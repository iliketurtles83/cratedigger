"""Featured credit parsing and canonical Tier 1 artist folder snapping."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import sqlite3
from typing import Any

import config
from cratedigger.core import (
    artist_comparison_key,
    artist_letter_bucket,
    read_tags,
    sanitise_component,
    sanitise_name,
)
from cratedigger.db import utc_now_iso

_FEAT_SPLIT_RE = re.compile(
    r"\s*(?:\(|\[)?\b(?:feat|ft|featuring)\.?\s+",
    re.IGNORECASE,
)
_MULTI_SEP_RE = re.compile(r"\s*(?:;|,|/|&amp;|&|\band\b|\bx\b|\bwith\b)\s*", re.IGNORECASE)
_SLASH_ONLY_RE = re.compile(r"\s*[;/]\s*")


def split_featured_artists(artist: str | None, *, title: str | None = None) -> tuple[str, list[str]]:
    """Return ``(primary_artist, all_artists)`` separating ``feat.`` credits.

    ``primary_artist`` is used for ``albumartist`` and physical Tier 1 folder routing.
    ``all_artists`` populates the multi-value ``artists`` tag without splitting
    established band names containing ``&`` when no ``feat.`` marker is present.
    """
    raw = (artist or "").strip()
    if not raw:
        return "", []

    guests_raw = ""
    parts = _FEAT_SPLIT_RE.split(raw, maxsplit=1)
    primary_raw = parts[0].strip().rstrip("([ ").strip()
    if len(parts) > 1:
        guests_raw = parts[1].strip().rstrip(")]").strip()

    # Check title for "(feat. X)" if artist didn't contain one.
    if not guests_raw and title:
        t_parts = _FEAT_SPLIT_RE.split(title, maxsplit=1)
        if len(t_parts) > 1:
            guests_raw = t_parts[1].strip().rstrip(")]").strip()

    if ";" in primary_raw or " / " in primary_raw:
        prim_tokens = [t.strip() for t in _SLASH_ONLY_RE.split(primary_raw) if t.strip()]
        primary = prim_tokens[0] if prim_tokens else primary_raw
        all_list = list(dict.fromkeys(prim_tokens))
    else:
        primary = primary_raw
        all_list = [primary] if primary else []

    if guests_raw:
        for token in _MULTI_SEP_RE.split(guests_raw):
            clean = token.strip().strip("()[]")
            if clean and clean not in all_list:
                all_list.append(clean)

    return primary, all_list


def format_artists_tag(artists: list[str]) -> str:
    """Format canonical list of individual artists into ``lib/tags.py`` format."""
    cleaned = [a.strip() for a in artists if a and a.strip()]
    return " / ".join(dict.fromkeys(cleaned))


def _bucket_dirs(music_root: Path) -> list[tuple[str, Path]]:
    buckets = sorted(getattr(config, "LETTER_BUCKETS", set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))) + [
        getattr(config, "SYMBOL_BUCKET", "#")
    ]
    out: list[tuple[str, Path]] = []
    for b in buckets:
        bdir = music_root / b
        if bdir.is_dir():
            out.append((b, bdir))
    return out


def build_artist_index(conn: sqlite3.Connection, music_root: Path) -> list[dict[str, Any]]:
    """Index existing Tier 1 artist folders into SQLite ``artists`` table."""
    ts = utc_now_iso()
    records: list[dict[str, Any]] = []
    seen_folders: set[str] = set()

    for bucket, bdir in _bucket_dirs(music_root):
        for artist_dir in sorted(bdir.iterdir()):
            if not artist_dir.is_dir():
                continue
            mbids: Counter[str] = Counter()
            sampled = 0
            for af in sorted(artist_dir.rglob("*")):
                if af.is_file() and af.suffix.lower() in config.AUDIO_EXTENSIONS:
                    tags = read_tags(af) or {}
                    mbid = tags.get("mb_albumartistid") or tags.get("mb_artistid")
                    if mbid:
                        mbids[mbid] += 1
                    sampled += 1
                    if sampled >= 5:
                        break
            top_mbid = mbids.most_common(1)[0][0] if mbids else None
            ckey = artist_comparison_key(artist_dir.name)
            fpath = str(artist_dir)
            seen_folders.add(fpath)
            rec = {
                "canonical_name": artist_dir.name,
                "comparison_key": ckey,
                "bucket": bucket,
                "mb_artistid": top_mbid,
                "folder_path": fpath,
            }
            records.append(rec)
            conn.execute(
                """
                INSERT INTO artists (canonical_name, comparison_key, bucket, mb_artistid, folder_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(folder_path) DO UPDATE SET
                    canonical_name = excluded.canonical_name,
                    comparison_key = excluded.comparison_key,
                    bucket = excluded.bucket,
                    mb_artistid = COALESCE(excluded.mb_artistid, artists.mb_artistid),
                    updated_at = excluded.updated_at
                """,
                (artist_dir.name, ckey, bucket, top_mbid, fpath, ts),
            )

    if seen_folders:
        existing_rows = conn.execute("SELECT folder_path FROM artists").fetchall()
        prefix = str(music_root.resolve())
        for row in existing_rows:
            fp = row["folder_path"]
            if fp.startswith(prefix) and fp not in seen_folders:
                conn.execute("DELETE FROM artists WHERE folder_path = ?", (fp,))
    conn.commit()
    return records


def snap_artist_name(
    conn: sqlite3.Connection,
    raw_artist: str,
    *,
    music_root: Path,
    mb_artistid: str | None = None,
    letter_bucketing: bool = True,
) -> tuple[str, str, Path]:
    """Snap *raw_artist* to an existing canonical Tier 1 artist folder if indexed.

    Priority:
    1. Exact ``mb_artistid`` match in SQLite ``artists`` table
    2. Exact case-insensitive folder name match
    3. Unambiguous ``artist_comparison_key`` match (e.g. ``ABBA`` vs ``Abba``, ``Arcade Fire`` vs ``The Arcade Fire``)
    4. Normalized fallback under ``artist_letter_bucket(raw_artist)``.
    """
    primary, _ = split_featured_artists(raw_artist)
    candidate = sanitise_component(sanitise_name(primary or raw_artist))
    if not candidate:
        candidate = "Unknown Artist"

    prefix = str(music_root.resolve())

    def _within_root(row_path: str) -> bool:
        try:
            return Path(row_path).resolve().is_relative_to(music_root.resolve())
        except OSError:
            return row_path.startswith(prefix)

    # 1. Match by MusicBrainz Artist ID
    if mb_artistid:
        rows = conn.execute(
            "SELECT canonical_name, bucket, folder_path FROM artists WHERE mb_artistid = ?",
            (mb_artistid,),
        ).fetchall()
        valid = [r for r in rows if _within_root(r["folder_path"])]
        if len(valid) == 1:
            r = valid[0]
            return r["canonical_name"], r["bucket"], Path(r["folder_path"])

    # 2. Match by case-insensitive canonical name or normalized comparison key
    ckey = artist_comparison_key(candidate)
    rows = conn.execute(
        "SELECT canonical_name, bucket, folder_path FROM artists WHERE comparison_key = ?",
        (ckey,),
    ).fetchall()
    valid = [r for r in rows if _within_root(r["folder_path"])]
    for r in valid:
        if r["canonical_name"].casefold() == candidate.casefold():
            return r["canonical_name"], r["bucket"], Path(r["folder_path"])
    if len(valid) == 1:
        r = valid[0]
        return r["canonical_name"], r["bucket"], Path(r["folder_path"])

    # 3. Direct disk fallback when DB wasn't pre-populated for this bucket
    bucket = artist_letter_bucket(candidate) if letter_bucketing else ""
    target_dir = (music_root / bucket / candidate) if bucket else (music_root / candidate)
    if bucket and (music_root / bucket).is_dir():
        for child in sorted((music_root / bucket).iterdir()):
            if child.is_dir() and (
                child.name.casefold() == candidate.casefold()
                or artist_comparison_key(child.name) == ckey
            ):
                return child.name, bucket, child
    return candidate, bucket, target_dir

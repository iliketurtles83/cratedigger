"""Unified Metadata Engine: transient no-write Beets, mb_cache, and 4-step genre fallback."""

from __future__ import annotations

from collections import Counter
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

from cratedigger.config_schema import Settings
from cratedigger.core import is_meaningful_genre, normalise_genre, read_tags
from cratedigger.db import utc_now_iso
from lib.embeddings import extract_embedding, get_style_classes
from lib.mb import (
    artist_release_groups,
    fingerprint_lookup,
    mb_genres,
    mb_recording_metadata,
    mb_recording_search,
)

log = logging.getLogger("cratedigger.engine.metadata")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_BEETS_CONFIG = _REPO_ROOT / "config" / "beets.yaml"
_MATCH_CONFIDENCE = re.compile(r"\bMatch \((\d+(?:\.\d+)?)%\):")


def _cache_get(conn: sqlite3.Connection, cache_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload_json FROM mb_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None


def _cache_set(conn: sqlite3.Connection, cache_key: str, kind: str, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO mb_cache (cache_key, kind, payload_json, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            kind = excluded.kind,
            payload_json = excluded.payload_json,
            created_at = excluded.created_at
        """,
        (cache_key, kind, json.dumps(payload, ensure_ascii=False), utc_now_iso()),
    )
    conn.commit()


def beets_match_album(
    folder: Path,
    conn: sqlite3.Connection | None = None,
    *,
    beets_config: Path | None = None,
    singleton: bool = False,
    online: bool = False,
    timeout: float = 90.0,
) -> dict[str, Any] | None:
    """Run an isolated transient Beets import with ``--nowrite --nomove --nocopy``.

    Returns extracted release/track metadata without modifying files or legacy DBs.
    Returns cached results when ``conn`` has a cached entry for ``folder``.
    """
    cache_key = f"beets:{'s' if singleton else 'a'}:{folder.resolve()}"
    if conn is not None:
        cached = _cache_get(conn, cache_key)
        if cached is not None:
            return cached if cached.get("matched") else None

    if not online or not folder.is_dir():
        return None

    cfg = beets_config or _DEFAULT_BEETS_CONFIG
    if not cfg.is_file():
        return None

    with tempfile.TemporaryDirectory(prefix="cratedigger_beets_") as tmp:
        tmp_path = Path(tmp)
        db_file = tmp_path / "transient.db"
        log_file = tmp_path / "beets.log"
        cmd = [
            sys.executable,
            "-m",
            "beets",
            "--library",
            str(db_file),
            "-c",
            str(cfg),
            "import",
            "--nocopy",
            "--nomove",
            "--nowrite",
            "--quiet",
            "--quiet-fallback",
            "skip",
            "--noincremental",
            "--log",
            str(log_file),
        ]
        if singleton:
            cmd.append("--singletons")
        cmd.append(str(folder))

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Transient Beets lookup failed for %s: %s", folder, exc)
            return None

        confidences: list[float] = []
        if log_file.is_file():
            for line in log_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = _MATCH_CONFIDENCE.search(line)
                if m:
                    confidences.append(float(m.group(1)))

        try:
            from beets.library import Library
        except ImportError:
            return None

        items: list[dict[str, Any]] = []
        if db_file.is_file():
            lib = Library(str(db_file), directory=str(folder))
            try:
                for item in lib.items():
                    artists = getattr(item, "artists", None)
                    items.append(
                        {
                            "path": str(Path(os.fsdecode(item.path))),
                            "artist": item.artist or "",
                            "artists": " / ".join(artists) if artists else "",
                            "title": item.title or "",
                            "albumartist": item.albumartist or "",
                            "album": item.album or "",
                            "year": str(item.year) if item.year else "",
                            "track": f"{item.track}/{item.tracktotal}" if item.track and item.tracktotal else (str(item.track) if item.track else ""),
                            "disc": f"{item.disc}/{item.disctotal}" if item.disc and item.disctotal else (str(item.disc) if item.disc else ""),
                            "tracktotal": int(item.tracktotal or 0),
                            "compilation": "1" if item.comp else "0",
                            "mb_trackid": item.mb_trackid or "",
                            "mb_albumid": item.mb_albumid or "",
                            "mb_artistid": item.mb_artistid or "",
                            "mb_albumartistid": item.mb_albumartistid or "",
                            "mb_releasegroupid": item.mb_releasegroupid or "",
                        }
                    )
            finally:
                lib._close()

        result: dict[str, Any] = {
            "matched": bool(items),
            "confidence": max(confidences) if confidences else (95.0 if items and proc.returncode == 0 else 0.0),
            "is_va": any(it["compilation"] == "1" for it in items),
            "album": items[0]["album"] if items else "",
            "albumartist": items[0]["albumartist"] if items else "",
            "year": items[0]["year"] if items else "",
            "mb_albumid": items[0]["mb_albumid"] if items else "",
            "mb_releasegroupid": items[0]["mb_releasegroupid"] if items else "",
            "tracktotal": max((it["tracktotal"] for it in items), default=0),
            "items": items,
        }
        if conn is not None:
            _cache_set(conn, cache_key, "beets_match", result)
        return result if result["matched"] else None


def lookup_track_metadata(
    path: Path,
    conn: sqlite3.Connection,
    *,
    audio_hash_val: str | None = None,
    tags: dict[str, str | None] | None = None,
    online: bool = False,
    allow_fingerprint: bool = True,
) -> dict[str, Any]:
    """Cached recording-level metadata lookup via AcoustID + MusicBrainz."""
    tags = tags if tags is not None else (read_tags(path) or {})
    rec_id = (tags.get("mb_trackid") or "").strip() or None
    cache_key = f"mb_rec:{audio_hash_val or path.resolve()}"
    cached = _cache_get(conn, cache_key)
    if cached is not None:
        return cached

    if not online:
        return {}

    result: dict[str, Any] = {}
    if not rec_id and allow_fingerprint:
        fp = fingerprint_lookup(path)
        if fp and fp.get("recording_id"):
            rec_id = str(fp["recording_id"])
            result.update({k: v for k, v in fp.items() if v})

    if not rec_id and tags.get("artist") and tags.get("title"):
        srch = mb_recording_search(
            tags["artist"] or "",
            tags["title"] or "",
            album=tags.get("album"),
        )
        if srch and srch.get("recording_id"):
            rec_id = str(srch["recording_id"])
            result.update({k: v for k, v in srch.items() if v})

    if rec_id:
        meta = mb_recording_metadata(rec_id)
        if meta:
            result.update({k: v for k, v in meta.items() if v})
        result.setdefault("mb_trackid", rec_id)

    _cache_set(conn, cache_key, "mb_track", result)
    return result


def apply_genre_remap(genre: str, remap_rules: dict[str, str] | None = None) -> str:
    """Normalize *genre* through 2-tier tree and user remap overrides."""
    norm = normalise_genre(genre)
    if not remap_rules:
        return norm
    for src, dst in remap_rules.items():
        if norm.casefold() == src.casefold() or norm.split(" / ")[-1].casefold() == src.casefold():
            return normalise_genre(dst)
    return norm


def _artist_library_consensus_genre(
    conn: sqlite3.Connection,
    artist: str,
    remap_rules: dict[str, str] | None = None,
) -> str | None:
    if not artist:
        return None
    rows = conn.execute(
        "SELECT genre FROM tracks WHERE LOWER(artist) = LOWER(?) AND genre IS NOT NULL AND genre != ''",
        (artist.strip(),),
    ).fetchall()
    counts: Counter[str] = Counter()
    for row in rows:
        g = apply_genre_remap(row["genre"], remap_rules)
        if is_meaningful_genre(g):
            counts[g] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def predict_effnet_genre(
    path: Path,
    *,
    remap_rules: dict[str, str] | None = None,
    embedding: Any = None,
) -> tuple[str | None, float]:
    """Predict best 2-tier genre via Discogs-EffNet ONNX model, returning ``(genre, score)``."""
    classes = get_style_classes()
    if not classes:
        return None, 0.0
    vec = embedding if embedding is not None else extract_embedding(path)
    if vec is None or len(vec) != len(classes):
        return None, 0.0

    best_idx = int(vec.argmax())
    best_score = float(vec[best_idx])
    raw_class = classes[best_idx]
    root_part, _, sub_part = raw_class.partition("---")
    root_norm = apply_genre_remap(root_part.strip(), remap_rules) if root_part.strip() else ""
    sub_norm = apply_genre_remap(sub_part.strip(), remap_rules) if sub_part.strip() else ""
    if root_norm and sub_norm and root_norm.casefold() != sub_norm.casefold() and " / " not in sub_norm:
        candidate = f"{root_norm} / {sub_norm}"
    else:
        candidate = sub_norm or root_norm
    mapped = apply_genre_remap(candidate, remap_rules) if candidate else ""
    if not is_meaningful_genre(mapped) and root_norm:
        mapped = root_norm
    return (mapped if is_meaningful_genre(mapped) else None), best_score


def resolve_genre_4step(
    path: Path,
    tags: dict[str, str | None],
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    mb_data: dict[str, Any] | None = None,
    online: bool = False,
    use_effnet: bool = False,
) -> dict[str, Any]:
    """Resolve genre via 4-step hierarchy.

    1. Existing valid file genre (reconciled via 2-tier tree + remap overrides).
    2. MusicBrainz recording & release-group tags.
    3. Library artist consensus (or MB artist release-group fallback).
    4. Discogs-EffNet ONNX prediction (auto-apply >= auto_threshold; review proposal >= review_threshold).
    """
    rules = settings.genre_remap_rules

    # Step 1: Existing valid tag
    existing = (tags.get("genre") or "").strip()
    if existing:
        reconciled = apply_genre_remap(existing, rules)
        if is_meaningful_genre(reconciled):
            return {"genre": reconciled, "source": "existing", "action": "keep_or_normalize", "confidence": 1.0}

    # Step 2: MusicBrainz recording / release-group tags
    if mb_data and mb_data.get("genres"):
        for g in mb_data["genres"]:
            mapped = apply_genre_remap(str(g), rules)
            if is_meaningful_genre(mapped):
                return {"genre": mapped, "source": "musicbrainz", "action": "apply", "confidence": 0.9}

    artist = (tags.get("albumartist") or tags.get("artist") or "").strip()
    title = (tags.get("title") or "").strip()
    if online and artist and title:
        mb_list = mb_genres(artist, title, album=tags.get("album"))
        for g in mb_list:
            mapped = apply_genre_remap(g, rules)
            if is_meaningful_genre(mapped):
                return {"genre": mapped, "source": "musicbrainz", "action": "apply", "confidence": 0.9}

    # Step 3: Artist consensus in cratedigger.db (or MB artist release-groups)
    consensus = _artist_library_consensus_genre(conn, artist, rules)
    if consensus:
        return {"genre": consensus, "source": "artist_consensus", "action": "apply", "confidence": 0.8}
    if online and artist:
        mb_artist_id = (tags.get("mb_artistid") or (mb_data or {}).get("mb_artistid") or "").strip()
        if mb_artist_id:
            rgs = artist_release_groups(mb_artist_id)
            for rg in rgs:
                for g in rg.get("tags", []) or []:
                    mapped = apply_genre_remap(str(g), rules)
                    if is_meaningful_genre(mapped):
                        return {"genre": mapped, "source": "artist_consensus", "action": "apply", "confidence": 0.75}

    # Step 4: Local Discogs-EffNet ONNX model
    if use_effnet:
        pred_genre, score = predict_effnet_genre(path, remap_rules=rules)
        if pred_genre and score >= settings.effnet_auto_threshold:
            return {"genre": pred_genre, "source": "effnet", "action": "apply", "confidence": round(score, 4)}
        if pred_genre and score >= settings.effnet_review_threshold:
            return {"genre": pred_genre, "source": "effnet", "action": "review", "confidence": round(score, 4)}

    return {"genre": None, "source": "unresolved", "action": "review", "confidence": 0.0}

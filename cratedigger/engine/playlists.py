"""Playlist synchronization, singles genre curation, and curated list conversion."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from cratedigger.config_schema import Settings
from cratedigger.core import read_tags
from lib.path_updates import update_downstream_paths
from scripts.generate_genre_playlists import resolve_root_genre

log = logging.getLogger("cratedigger.engine.playlists")


def format_relative_entry(track: Path, playlist_path: Path) -> str:
    """Return forward-slash relative path from *playlist_path*'s directory to *track*."""
    return os.path.relpath(track, playlist_path.parent).replace(os.sep, "/")


def read_m3u8_entries(playlist_path: Path) -> list[Path]:
    """Read resolved track paths from an existing .m3u8 playlist."""
    if not playlist_path.is_file():
        return []
    out: list[Path] = []
    for raw in playlist_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line)
        resolved = p if p.is_absolute() else (playlist_path.parent / p).resolve()
        out.append(resolved)
    return out


def write_m3u8(playlist_path: Path, track_paths: list[Path], *, dry_run: bool = False) -> Path:
    """Write a clean deduplicated .m3u8 playlist with paths relative to *playlist_path.parent*."""
    seen: set[str] = set()
    lines = ["#EXTM3U"]
    for track in track_paths:
        rel = format_relative_entry(track, playlist_path)
        if rel not in seen:
            seen.add(rel)
            lines.append(rel)
    if dry_run:
        return playlist_path
    playlist_path.parent.mkdir(parents=True, exist_ok=True)
    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return playlist_path


def append_to_playlist(
    playlist_path: Path,
    tracks: list[Path],
    *,
    dry_run: bool = False,
) -> int:
    """Append *tracks* not already in *playlist_path*, preserving existing entries."""
    existing = read_m3u8_entries(playlist_path)
    existing_norm = {str(p.resolve()) for p in existing}
    added: list[Path] = []
    for t in tracks:
        key = str(t.resolve())
        if key not in existing_norm:
            existing_norm.add(key)
            existing.append(t)
            added.append(t)
    if added and not dry_run:
        write_m3u8(playlist_path, existing, dry_run=False)
    return len(added)


def relink_playlists_for_move(
    path_map: dict[Path, Path],
    *,
    music_root: Path,
    features_path: Path | None = None,
) -> None:
    """Delegate downstream playlist and features.pkl synchronization after moves."""
    update_downstream_paths(
        path_map,
        music_root=music_root,
        logger=log,
        features_path=features_path,
    )


def update_singles_playlists(
    singles: list[Path],
    *,
    music_root: Path,
    settings: Settings,
    run_date: str | None = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Register newly placed singles in Intake, Singles-<RootGenre>, and All_Singles playlists."""
    if not singles:
        return []
    playlists_dir = settings.playlists_path()
    if playlists_dir == Path("Playlists"):
        playlists_dir = music_root / "Playlists"

    updates: list[dict[str, Any]] = []
    date_str = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if settings.auto_update_intake_playlist:
        intake_pl = playlists_dir / f"{settings.intake_playlist_prefix}{date_str}.m3u8"
        added = append_to_playlist(intake_pl, singles, dry_run=dry_run)
        if added or dry_run:
            updates.append({"playlist": str(intake_pl), "added": added or len(singles)})

    if settings.auto_update_singles_playlists:
        by_root: dict[str, list[Path]] = {}
        for s in singles:
            tags = read_tags(s) or {}
            root_g = resolve_root_genre(tags.get("genre"))
            by_root.setdefault(root_g, []).append(s)
        for root_g, grp_tracks in sorted(by_root.items()):
            pl = playlists_dir / f"Singles - {root_g}.m3u8"
            added = append_to_playlist(pl, grp_tracks, dry_run=dry_run)
            if added or dry_run:
                updates.append({"playlist": str(pl), "added": added or len(grp_tracks)})

        all_pl = playlists_dir / "All_Singles.m3u8"
        added_all = append_to_playlist(all_pl, singles, dry_run=dry_run)
        if added_all or dry_run:
            updates.append({"playlist": str(all_pl), "added": added_all or len(singles)})

    return updates


def resolve_playlist_tracks_by_hash(
    conn: sqlite3.Connection,
    track_hashes: list[tuple[Path, str]],
) -> tuple[list[Path], list[Path]]:
    """Resolve list of ``(path, audio_hash)`` against canonical paths in SQLite.

    Returns ``(resolved_playlist_entries, unlinked_files)``.
    """
    entries: list[Path] = []
    unlinked: list[Path] = []
    for src, ah in track_hashes:
        row = conn.execute(
            "SELECT canonical_path FROM tracks WHERE audio_hash = ?",
            (ah,),
        ).fetchone()
        canon = Path(row["canonical_path"]) if row and row["canonical_path"] else None
        if canon and canon.exists() and canon.resolve() != src.resolve():
            entries.append(canon)
        else:
            unlinked.append(src)
            entries.append(src)
    return entries, unlinked

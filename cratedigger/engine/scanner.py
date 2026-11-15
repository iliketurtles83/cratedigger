"""Read-only directory scanner and release/track classifier."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import logging
from pathlib import Path
import sqlite3
from typing import Any, Callable

from mutagen import File as MutagenFile

import config
from cratedigger.config_schema import Settings
from cratedigger.core import (
    audio_hash,
    is_disc_subfolder,
    is_suspicious_tag,
    normalise_genre,
    parse_filename,
    parse_folder_name,
    read_tags,
)
from cratedigger.core.artists import split_featured_artists
from cratedigger.engine.artwork import find_folder_cover
from cratedigger.engine.transcoder import needs_transcode
from scripts.consolidate_small_releases import is_complete_release

log = logging.getLogger("cratedigger.engine.scanner")

_ALWAYS_SKIP_DIRS = frozenset({"0quarantine", "0videos", "Playlists", ".git", "__pycache__"})
_VA_NAMES = frozenset({"various artists", "va", "v.a.", "v. a.", "various"})


@dataclass
class ScannedTrack:
    path: Path
    audio_hash: str
    size: int
    mtime_ns: int
    duration_ms: int | None
    tags: dict[str, str | None]
    parsed: dict[str, str | None]
    needs_transcode: bool
    library_duplicate_paths: list[str] = field(default_factory=list)


@dataclass
class ScanUnit:
    folder: Path
    kind: str
    tracks: list[ScannedTrack]
    cover_path: Path | None = None
    image_candidates: list[Path] = field(default_factory=list)
    ambiguous_covers: bool = False
    is_curated_list: bool = False
    is_soundtrack: bool = False
    is_exempt_mix: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    scope: Path
    units: list[ScanUnit]
    total_tracks: int
    skipped_dirs: list[str] = field(default_factory=list)


def _all_supported_audio_extensions(settings: Settings | None = None) -> set[str]:
    exts = {e.lower() for e in config.AUDIO_EXTENSIONS}
    if settings is not None:
        exts.update(e.lower() for e in settings.transcode_extensions)
    exts.update({".wma", ".mpc", ".ogg", ".opus"})
    return exts


def _cached_or_compute_hash(
    conn: sqlite3.Connection | None,
    path: Path,
    *,
    size: int,
    mtime_ns: int,
) -> str:
    if conn is not None:
        row = conn.execute(
            "SELECT audio_hash, size, mtime_ns FROM track_paths WHERE path = ?",
            (str(path),),
        ).fetchone()
        if row and row["size"] == size and row["mtime_ns"] == mtime_ns and row["audio_hash"]:
            return str(row["audio_hash"])
    return audio_hash(str(path))


def _duplicate_library_paths(
    conn: sqlite3.Connection | None,
    ah: str,
    current_path: Path,
) -> list[str]:
    if conn is None or not ah:
        return []
    rows = conn.execute(
        "SELECT path FROM track_paths WHERE audio_hash = ? AND path != ?",
        (ah, str(current_path)),
    ).fetchall()
    return [r["path"] for r in rows if Path(r["path"]).exists()]


def _probe_duration_ms(path: Path) -> int | None:
    try:
        audio = MutagenFile(path, easy=False)
        if audio is not None and getattr(audio, "info", None) and getattr(audio.info, "length", None):
            return int(float(audio.info.length) * 1000)
    except Exception:
        return None
    return None


def _is_skipped_dir(path: Path, music_root: Path, settings: Settings) -> bool:
    skip = set(_ALWAYS_SKIP_DIRS) | set(getattr(config, "SKIP_FOLDERS", set()))
    try:
        rel_parts = path.resolve().relative_to(music_root.resolve()).parts
    except ValueError:
        rel_parts = path.parts
    return any(part in skip for part in rel_parts)


def _is_exempt_mix_path(folder: Path, music_root: Path, settings: Settings) -> bool:
    exempt = {f.casefold() for f in settings.exempt_mix_folders}
    try:
        rel_parts = [p.casefold() for p in folder.resolve().relative_to(music_root.resolve()).parts]
    except ValueError:
        rel_parts = [p.casefold() for p in folder.parts]
    return any(p in exempt for p in rel_parts)


def _is_random_redistribution_path(folder: Path, music_root: Path) -> bool:
    targets = {"0random", "0random_good", "0bestof"}
    try:
        rel_parts = [p.casefold() for p in folder.resolve().relative_to(music_root.resolve()).parts]
    except ValueError:
        rel_parts = [p.casefold() for p in folder.parts]
    return any(p in targets for p in rel_parts)


def _is_soundtrack_path(folder: Path, music_root: Path) -> bool:
    try:
        rel_parts = [p.casefold() for p in folder.resolve().relative_to(music_root.resolve()).parts]
    except ValueError:
        rel_parts = [p.casefold() for p in folder.parts]
    return "soundtrack" in rel_parts


def _looks_like_va(tracks: list[ScannedTrack]) -> bool:
    aa_vals = {
        (t.tags.get("albumartist") or "").strip().casefold()
        for t in tracks
        if (t.tags.get("albumartist") or "").strip()
    }
    if aa_vals & _VA_NAMES:
        return True
    primaries = {
        split_featured_artists(t.tags.get("artist") or t.parsed.get("artist") or "")[0].casefold()
        for t in tracks
    }
    primaries.discard("")
    comp_flags = {(t.tags.get("compilation") or "").strip() for t in tracks}
    if "1" in comp_flags and len(primaries) > 1:
        return True
    threshold = int(getattr(config, "COMPILATION_ARTIST_THRESHOLD", 3))
    return len(primaries) > threshold


def _classify_unit(
    folder: Path,
    tracks: list[ScannedTrack],
    *,
    music_root: Path,
    settings: Settings,
) -> tuple[str, list[str]]:
    notes: list[str] = []
    curated_names = {c.casefold() for c in getattr(config, "CURATED_LIST_FOLDERS", set())}

    if folder.name.casefold() in curated_names:
        notes.append("configured_curated_list")
        return "curated_list", notes

    # Exempt personal mixes (0mixes / 0shacks)
    if _is_exempt_mix_path(folder, music_root, settings):
        parsed_folder = parse_folder_name(folder.name)
        albums = {(t.tags.get("album") or "").strip() for t in tracks if (t.tags.get("album") or "").strip()}
        if len(tracks) >= 6 and len(albums) == 1 and parsed_folder.get("year"):
            notes.append("full_album_inside_mix_folder")
        else:
            return "exempt_mix", notes

    # Soundtracks folder dissolution
    if _is_soundtrack_path(folder, music_root) and settings.dissolve_soundtracks:
        if _looks_like_va(tracks):
            return "soundtrack_va", ["soundtrack_multi_artist"]
        return "soundtrack_score", ["soundtrack_composer"]

    # Explicit singles folders
    if folder.name in {config.ARTIST_SINGLES_FOLDER, config.ROOT_SINGLES_FOLDER}:
        return "singles_folder", notes

    # Loose/random legacy buckets targeted for Tier 1 redistribution
    if settings.redistribute_random and _is_random_redistribution_path(folder, music_root):
        notes.append("redistribute_from_random_bucket")
        return "loose_singles", notes

    # VA / Multi-artist check
    is_va = _looks_like_va(tracks)
    if is_va:
        aa_vals = {(t.tags.get("albumartist") or "").strip().casefold() for t in tracks}
        if aa_vals & _VA_NAMES or folder.parent.name == "0compilations":
            return "compilation", notes
        # Multi-artist folder without VA albumartist -> user chooses Compilation vs Playlist vs Singles
        notes.append("multi_artist_ambiguous_intent")
        return "multi_artist_review", notes

    # Check if single file/loose drop inside root incoming/staging without album coherence
    if folder.resolve() in {music_root.resolve(), Path(settings.incoming_folder).resolve()}:
        return "loose_singles", ["root_loose_tracks"]

    # Small release consolidation (<= N tracks, incomplete, and not all tracks > 15 min)
    threshold = settings.small_release_threshold
    if threshold > 0 and len(tracks) <= threshold:
        long_ms = settings.long_track_minutes * 60 * 1000
        all_long = all((t.duration_ms or 0) >= long_ms for t in tracks)
        file_paths = [t.path for t in tracks]
        file_tags = [t.tags for t in tracks]
        complete, reason = is_complete_release(folder, file_paths, file_tags)
        if not all_long and not complete:
            notes.append(f"small_incomplete_release:{reason}")
            return "small_release_singles", notes
        if all_long:
            notes.append("kept_long_form_release")
        else:
            notes.append(f"kept_complete_release:{reason}")

    # Check if genre contains Soundtrack for album classification note
    genres = {normalise_genre(t.tags.get("genre") or "") for t in tracks if t.tags.get("genre")}
    if any("soundtrack" in g.casefold() for g in genres):
        notes.append("soundtrack_genre")

    return "album", notes


def scan_scope(
    scope: Path,
    *,
    settings: Settings,
    conn: sqlite3.Connection | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> ScanResult:
    """Scan *scope* strictly read-only, grouping files into classified ``ScanUnit`` objects."""
    music_root = settings.music_root_path()
    exts = _all_supported_audio_extensions(settings)
    if not scope.exists():
        raise FileNotFoundError(f"Scan scope does not exist: {scope}")

    grouped_files: dict[Path, list[Path]] = {}
    skipped: list[str] = []

    if scope.is_file():
        if scope.suffix.lower() in exts:
            grouped_files[scope.parent] = [scope]
    else:
        for root, dirs, files in os_walk_sorted(scope):
            root_path = Path(root)
            if _is_skipped_dir(root_path, music_root, settings):
                skipped.append(str(root_path))
                dirs[:] = []
                continue
            dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and not _is_skipped_dir(root_path / d, music_root, settings)]
            audio_here = [
                root_path / f
                for f in sorted(files)
                if not f.startswith(".") and (root_path / f).suffix.lower() in exts
            ]
            if not audio_here:
                continue
            unit_dir = root_path.parent if is_disc_subfolder(root_path.name) else root_path
            grouped_files.setdefault(unit_dir, []).extend(audio_here)

    total_files = sum(len(v) for v in grouped_files.values())
    done_files = 0
    units: list[ScanUnit] = []

    for unit_dir in sorted(grouped_files):
        raw_paths = sorted(grouped_files[unit_dir])
        scanned_tracks: list[ScannedTrack] = []
        for p in raw_paths:
            st = p.stat()
            ah = _cached_or_compute_hash(conn, p, size=st.st_size, mtime_ns=st.st_mtime_ns)
            tags = read_tags(p) or {}
            parsed = parse_filename(p)
            dups = _duplicate_library_paths(conn, ah, p)
            scanned_tracks.append(
                ScannedTrack(
                    path=p,
                    audio_hash=ah,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    duration_ms=_probe_duration_ms(p),
                    tags=tags,
                    parsed=parsed,
                    needs_transcode=needs_transcode(p, settings),
                    library_duplicate_paths=dups,
                )
            )
            done_files += 1
            if progress_cb:
                progress_cb(done_files, total_files, str(p))

        cover_path, img_cands = find_folder_cover(unit_dir)
        ambiguous_cov = cover_path is None and len(img_cands) > 1
        kind, notes = _classify_unit(unit_dir, scanned_tracks, music_root=music_root, settings=settings)

        # Detect tag conflicts if suspicious or <75% agreement on folder album/year
        if kind in {"album", "compilation", "soundtrack_score", "soundtrack_va"} and len(scanned_tracks) > 1:
            for fld in ("album", "year"):
                vals = [(t.tags.get(fld) or "").strip() for t in scanned_tracks if (t.tags.get(fld) or "").strip()]
                if len(set(vals)) > 1:
                    top_cnt = Counter(vals).most_common(1)[0][1]
                    if top_cnt / len(scanned_tracks) < settings.consensus_auto_threshold:
                        notes.append(f"tag_conflict:{fld}")

        if any(is_suspicious_tag(t.tags.get("artist"), "artist") for t in scanned_tracks):
            notes.append("placeholder_tags")

        units.append(
            ScanUnit(
                folder=unit_dir,
                kind=kind,
                tracks=scanned_tracks,
                cover_path=cover_path,
                image_candidates=img_cands,
                ambiguous_covers=ambiguous_cov,
                is_curated_list=(kind == "curated_list"),
                is_soundtrack=(kind in {"soundtrack_score", "soundtrack_va"}),
                is_exempt_mix=(kind == "exempt_mix"),
                notes=notes,
            )
        )

    return ScanResult(scope=scope, units=units, total_tracks=total_files, skipped_dirs=skipped)


def os_walk_sorted(root: Path):
    import os

    return os.walk(root)

#!/usr/bin/env python3
"""convert_lists.py — Convert curated list folders into .m3u8 playlists.

Scans folders in 0compilations/ (e.g. "The Pitchfork 500", "Rolling Stone 500",
"Classic Rock Music - Compilation Mix - Playlist") that represent someone's list
rather than commercial releases:
1. Generates an .m3u8 playlist in Playlists/ with relative paths in list order.
2. Identifies duplicates against existing album copies in the library via audio_hash.
3. Proposes retagging tracks with their genuine release (or leaving album empty for loose singles).

Usage:
    python scripts/convert_lists.py [--dry-run] --folder "100 Greatest Rock Songs of 90's"
    python scripts/convert_lists.py --no-dry-run --folder "Classic Rock Music - Compilation Mix - Playlist"
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.audiohash import audio_hash
from lib.logger import setup_logger
from lib.parsers import sanitise_name
from lib.tags import read_tags, write_tags

log = logging.getLogger("convert_lists")


CURATED_LIST_FOLDERS = [
    "100 Greatest Rock Guitar Solos",
    "100 Greatest Rock Songs of 90's",
    "100 hits -Rock of the 70s-80s-90s",
    "Classic Rock Music - Compilation Mix - Playlist",
    "More Classic Rock Music 2 - Compilation Mix - Playlist",
    "djcity",
    "Pitchfork Presents the 100 Best Tracks of 2007",
    "Pitchfork Presents the 100 Best Tracks of 2008",
    "Pitchfork Presents the 100 Best Tracks of 2009",
    "Pitchfork Presents the 100 Best Tracks of 2010",
    "Pitchfork Presents the 100 Best Tracks of 2011",
    "The Pitchfork 500",
    "The Rolling Stone Magazines 500 Greatest Songs Of All Time",
    "Top 100 Latin Hits Playlist (2018)",
    "Top 250 Rap Songs of All Time",
    "Various Artists - Indie Rock Playlist March",
    "Various Artists - Random Mix (from sean i think)",
    "Various Artists - Working Full Time Mix",
]

_FEATURES_CACHE: dict[str, Path] | None = None


def _get_features_hash_map() -> dict[str, Path]:
    global _FEATURES_CACHE
    if _FEATURES_CACHE is not None:
        return _FEATURES_CACHE

    hash_map: dict[str, Path] = {}
    features_path = REPO_ROOT / "features.pkl"
    if features_path.exists():
        try:
            import pickle
            log.info("Loading precomputed audio hashes from features.pkl...")
            with open(features_path, "rb") as f:
                df = pickle.load(f)
            if hasattr(df, "iterrows"):
                for _, row in df.iterrows():
                    h = row.get("audio_hash")
                    p_str = row.get("path")
                    if h and p_str and h not in hash_map:
                        hash_map[h] = Path(p_str)
                log.info("Loaded %d precomputed hashes from features.pkl", len(hash_map))
        except Exception as exc:
            log.warning("Could not load features.pkl: %s", exc)

    _FEATURES_CACHE = hash_map
    return _FEATURES_CACHE


def find_library_duplicates(
    target_hashes: set[str],
    library_root: Path,
    exclude_folder: Path,
    *,
    full_scan: bool = False,
) -> dict[str, Path]:
    """Scan library for matching audio hashes outside exclude_folder.

    Checks features.pkl first for fast precomputed hash lookup.
    """
    hash_to_path: dict[str, Path] = {}
    hash_map = _get_features_hash_map()
    if hash_map:
        for h in target_hashes:
            if h in hash_map:
                p = hash_map[h]
                if exclude_folder not in p.parents:
                    hash_to_path[h] = p
        log.info("Found %d duplicate matches in features cache", len(hash_to_path))
        if not full_scan:
            return hash_to_path

    if full_scan:
        log.info("Indexing existing audio hashes across %s...", library_root)
        for p in library_root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in config.AUDIO_EXTENSIONS:
                continue
            try:
                if exclude_folder in p.parents:
                    continue
            except Exception:
                pass

            try:
                h = audio_hash(str(p))
                if h and h in target_hashes and h not in hash_to_path:
                    hash_to_path[h] = p
            except Exception:
                continue

    return hash_to_path


def convert_list_folder(
    folder_path: Path,
    playlist_dir: Path,
    library_root: Path,
    *,
    playlist_name: str | None = None,
    dry_run: bool = True,
    retag: bool = False,
    remove_duplicates: bool = False,
) -> tuple[int, int, int] | None:
    """Convert list folder into an .m3u8 playlist and optionally retag unique tracks or prune duplicates."""
    if not folder_path.exists() or not folder_path.is_dir():
        log.error("Folder not found: %s", folder_path)
        return None

    audio_files = sorted(
        p for p in folder_path.rglob("*")
        if p.is_file() and p.suffix.lower() in config.AUDIO_EXTENSIONS
    )
    if not audio_files:
        log.warning("No audio files found in %s", folder_path)
        return None

    clean_name = playlist_name or sanitise_name(folder_path.name)
    playlist_path = playlist_dir / f"{clean_name}.m3u8"

    log.info("Processing list folder: %s (%d tracks)", folder_path.name, len(audio_files))
    log.info("Target playlist: %s", playlist_path)

    # Hash target tracks in the list
    track_hashes: dict[Path, str] = {}
    for track in audio_files:
        try:
            h = audio_hash(str(track))
            if h:
                track_hashes[track] = h
        except Exception as exc:
            log.warning("Could not hash %s: %s", track.name, exc)

    # Index library to check for copies already on their albums
    existing_hashes = find_library_duplicates(set(track_hashes.values()), library_root, folder_path)

    playlist_lines: list[str] = ["#EXTM3U"]
    duplicates_found: list[tuple[Path, Path]] = []
    unique_tracks: list[Path] = []

    for track in audio_files:
        h = track_hashes.get(track)
        album_copy = existing_hashes.get(h) if h else None

        if album_copy:
            duplicates_found.append((track, album_copy))
            # Use relative path from playlist_dir to album copy
            try:
                rel = os.path.relpath(album_copy, playlist_dir)
            except ValueError:
                rel = str(album_copy)
            playlist_lines.append(f"#EXTINF:-1,{track.stem}")
            playlist_lines.append(rel)

            if remove_duplicates:
                if dry_run:
                    log.info("[DRY-RUN] Would remove duplicate list copy: %s (album copy: %s)", track.name, album_copy)
                else:
                    try:
                        track.unlink()
                        log.info("Removed duplicate list copy: %s", track.name)
                    except OSError as exc:
                        log.warning("Failed to remove duplicate %s: %s", track.name, exc)
        else:
            unique_tracks.append(track)
            try:
                rel = os.path.relpath(track, playlist_dir)
            except ValueError:
                rel = str(track)
            playlist_lines.append(f"#EXTINF:-1,{track.stem}")
            playlist_lines.append(rel)

            if retag:
                tags = read_tags(track)
                if tags:
                    updates: dict[str, str | None] = {}
                    if tags.get("compilation") == "1":
                        updates["compilation"] = "0"
                    if (tags.get("albumartist") or "").lower() in {"various artists", "va", "v.a."}:
                        updates["albumartist"] = tags.get("artist") or None
                    curr_album = (tags.get("album") or "").strip()
                    is_list_album = (
                        curr_album.lower() == folder_path.name.lower()
                        or curr_album in CURATED_LIST_FOLDERS
                        or "pitchfork" in curr_album.lower()
                        or "rolling stone" in curr_album.lower()
                    )
                    if is_list_album:
                        updates["album"] = None

                    if updates:
                        if dry_run:
                            log.info("[DRY-RUN] Would retag list track %s: %s", track.name, updates)
                        else:
                            write_tags(track, updates)
                            log.info("Retagged list track %s: %s", track.name, updates)

    playlist_content = "\n".join(playlist_lines) + "\n"

    if dry_run:
        log.info("[DRY-RUN] Would write playlist to %s with %d entries", playlist_path, len(audio_files))
    else:
        playlist_dir.mkdir(parents=True, exist_ok=True)
        playlist_path.write_text(playlist_content, encoding="utf-8")
        log.info("Wrote playlist to %s with %d entries", playlist_path, len(audio_files))

    log.info("\n=== Summary for %s ===", folder_path.name)
    log.info("  Total tracks in list: %d", len(audio_files))
    log.info("  Existing album copies found (duplicates): %d", len(duplicates_found))
    log.info("  Unique list tracks: %d", len(unique_tracks))

    if duplicates_found:
        log.info("\nDuplicate tracks (already in library on their original albums):")
        for list_copy, album_copy in duplicates_found[:5]:
            log.info("  %s → %s", list_copy.name, album_copy)
        if len(duplicates_found) > 5:
            log.info("  ... and %d more duplicates", len(duplicates_found) - 5)

    return len(audio_files), len(duplicates_found), len(unique_tracks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert curated list folder to .m3u8 playlist.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview without writing")
    parser.add_argument("--no-dry-run", action="store_true", help="Write playlist and apply operations")
    parser.add_argument("--retag", action="store_true", help="Clear list album tag and compilation flag from unique tracks")
    parser.add_argument("--remove-duplicates", action="store_true", help="Delete duplicate files that already exist on their original albums")
    parser.add_argument("--folder", type=str, default=None, help="Folder name or path under 0compilations/")
    parser.add_argument("--all", action="store_true", help="Convert all known curated list folders")
    parser.add_argument("--library-root", type=str, default=str(config.MUSIC_ROOT), help="Music library root")
    parser.add_argument("--playlists-dir", type=str, default=None, help="Directory to store playlists")
    parser.add_argument("--playlist-name", type=str, default=None, help="Custom name for generated playlist (without extension)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    dry_run = not args.no_dry_run
    lib_root = Path(args.library_root)
    pl_dir = Path(args.playlists_dir) if args.playlists_dir else lib_root / "Playlists"

    folders_to_process: list[Path] = []
    if args.all:
        for name in CURATED_LIST_FOLDERS:
            p = lib_root / "0compilations" / name
            if p.exists() and p.is_dir():
                folders_to_process.append(p)
            else:
                log.warning("Curated list folder not found: %s", p)
    elif args.folder:
        folder_arg = Path(args.folder)
        if folder_arg.is_absolute():
            target_folder = folder_arg
        elif (lib_root / "0compilations" / folder_arg).exists():
            target_folder = lib_root / "0compilations" / folder_arg
        elif (lib_root / folder_arg).exists():
            target_folder = lib_root / folder_arg
        else:
            target_folder = folder_arg
        folders_to_process.append(target_folder)
    else:
        parser.error("Must specify either --folder <name> or --all")

    total_tracks = 0
    total_dupes = 0
    total_unique = 0

    for target_folder in folders_to_process:
        res = convert_list_folder(
            target_folder,
            pl_dir,
            lib_root,
            playlist_name=args.playlist_name,
            dry_run=dry_run,
            retag=args.retag,
            remove_duplicates=args.remove_duplicates,
        )
        if res:
            t, d, u = res
            total_tracks += t
            total_dupes += d
            total_unique += u

    log.info("\n================ ALL LISTS SUMMARY ================")
    log.info("  Processed lists: %d", len(folders_to_process))
    log.info("  Total tracks: %d", total_tracks)
    log.info("  Total existing album duplicates: %d", total_dupes)
    log.info("  Total unique tracks: %d", total_unique)
    if dry_run:
        log.info("  Mode: DRY-RUN (no playlists written)")
    else:
        log.info("  Mode: WROTE %d playlists to %s", len(folders_to_process), pl_dir)


if __name__ == "__main__":
    main()

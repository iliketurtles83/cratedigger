#!/usr/bin/env python3
"""retag_lists.py — Retag curated list folders to restore real album/artist metadata.

In accordance with Sections 1 & 2 of docs/cratedigger-feedback.md:
- Clears fake list album names (or replaces with genuine release from library if known).
- Replaces bogus "Various Artists" / "Pitchfork Media" albumartist tags with real artist.
- Enforces compilation = "0" on non-commercial list tracks so they don't pollute VA.
- Leaves album blank when no genuine release is known, turning the file into a clean single.

Usage:
    python scripts/retag_lists.py [--dry-run] --folder "100 Greatest Rock Songs of 90's"
    python scripts/retag_lists.py --no-dry-run --all
"""

import argparse
import logging
import re
import sqlite3
import sys
from pathlib import Path

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.parsers import parse_filename, sanitise_name, validate_albumartist
from lib.tags import read_tags, write_tags
from scripts.convert_lists import CURATED_LIST_FOLDERS

log = logging.getLogger("retag_lists")

_FEAT_PATTERN = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$", re.IGNORECASE)


def _extract_main_artist(artist: str | None) -> str | None:
    if not artist:
        return None
    cleaned = _FEAT_PATTERN.sub("", artist).strip()
    return cleaned or artist.strip()


def _is_list_album_name(album: str | None, folder_name: str) -> bool:
    if not album:
        return False
    alb_low = album.strip().lower()
    fn_low = folder_name.strip().lower()

    if alb_low in fn_low or fn_low in alb_low:
        return True

    # Generic list markers in album tags
    list_markers = [
        "pitchfork", "rolling stone", "100 greatest", "500 greatest",
        "best tracks of 20", "top 100", "top 250", "indie/rock playlist",
        "compilation mix - playlist", "random mix", "working full time mix",
    ]
    return any(marker in alb_low for marker in list_markers)


def load_library_album_index() -> dict[tuple[str, str], tuple[str, str, str | None]]:
    """Load (lower_artist, lower_title) -> (album, albumartist, year) from clean artist folders."""
    index: dict[tuple[str, str], tuple[str, str, str | None]] = {}
    db_path = Path("/home/jack/.config/beets/library.db")
    if not db_path.exists():
        return index

    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("""
            SELECT lower(artist), lower(title), album, albumartist, year
            FROM items
            WHERE path NOT LIKE '%0%'
              AND path NOT LIKE '%soundtrack%'
              AND album != ''
              AND lower(artist) = lower(albumartist)
        """)
        for a, t, alb, aa, yr in cur.fetchall():
            key = (a.strip(), t.strip())
            if key not in index and alb:
                index[key] = (alb, aa or a, str(yr) if yr else None)
        conn.close()
        log.info("Loaded %d clean canonical album tracks from library database", len(index))
    except Exception as exc:
        log.warning("Could not read library database: %s", exc)

    return index


def retag_list_folder(
    folder_path: Path,
    library_index: dict[tuple[str, str], tuple[str, str, str | None]] | None = None,
    *,
    dry_run: bool = True,
    as_compilation: bool = False,
    album_title: str | None = None,
) -> tuple[int, int]:
    """Inspect and clean tags for all tracks in a list folder.

    When as_compilation is True, unifies tracks into a single Various Artists
    compilation release (albumartist="Various Artists", compilation="1",
    album=album_title or folder_path.name) while preserving individual track artists.

    Returns (total_tracks, modified_tracks).
    """
    if not folder_path.exists() or not folder_path.is_dir():
        log.error("Folder not found: %s", folder_path)
        return 0, 0

    audio_files = sorted(
        p for p in folder_path.rglob("*")
        if p.is_file() and p.suffix.lower() in config.AUDIO_EXTENSIONS
    )
    if not audio_files:
        log.warning("No audio files in %s", folder_path)
        return 0, 0

    mode_label = "as Various Artists compilation" if as_compilation else "with original release tags"
    log.info("\n=== Processing list folder: %s (%d tracks) %s ===", folder_path.name, len(audio_files), mode_label)
    modified_count = 0

    target_album = (album_title or folder_path.name).strip()
    target_aa = "Various Artists"
    target_comp = "1"

    for path in audio_files:
        tags = read_tags(path)
        if not tags:
            continue

        raw_artist = tags.get("artist") or ""
        raw_album = tags.get("album") or ""
        raw_aa = tags.get("albumartist") or ""
        raw_comp = tags.get("compilation")

        updates: dict[str, str | None] = {}

        if as_compilation:
            if (raw_album or "").strip() != target_album:
                updates["album"] = target_album
            if (raw_aa or "").strip() != target_aa:
                updates["albumartist"] = target_aa
            if str(raw_comp or "").strip() != target_comp:
                updates["compilation"] = target_comp
            if not tags.get("track"):
                parsed = parse_filename(path)
                if parsed.get("track"):
                    updates["track"] = parsed["track"]
        else:
            # 1. Check if album tag is a fake list name
            is_list_album = _is_list_album_name(raw_album, folder_path.name)
            if is_list_album:
                match_key = (raw_artist.strip().lower(), (tags.get("title") or "").strip().lower())
                if library_index and match_key in library_index:
                    known_alb, known_aa, known_yr = library_index[match_key]
                    updates["album"] = known_alb
                    updates["albumartist"] = known_aa
                    if known_yr and not tags.get("year"):
                        updates["year"] = known_yr
                else:
                    # Leave album empty so it becomes a clean single
                    updates["album"] = ""

            # 2. Determine target albumartist
            main_artist = _extract_main_artist(raw_artist)
            valid_aa = validate_albumartist(raw_aa, raw_artist, tags.get("track"))

            # If album was matched, albumartist is already set in updates
            if "albumartist" not in updates:
                if not valid_aa or valid_aa.lower() in {"various artists", "va", "pitchfork media"}:
                    updates["albumartist"] = main_artist
                elif not raw_aa and main_artist:
                    updates["albumartist"] = main_artist

            # 3. Ensure compilation is '0'
            if raw_comp != "0":
                updates["compilation"] = "0"

        # 4. Filter no-op updates
        real_updates: dict[str, str | None] = {}
        for k, v in updates.items():
            current_v = tags.get(k)
            if v == "" and not current_v:
                continue
            if v != current_v:
                real_updates[k] = v

        if not real_updates:
            continue

        modified_count += 1
        if dry_run:
            log.info("  [DRY-RUN] %s: would update %s", path.name, real_updates)
        else:
            write_tags(path, real_updates, dry_run=False)
            log.info("  %s: updated %s", path.name, real_updates)

    log.info("Completed %s: %d / %d tracks modified (%s)",
             folder_path.name, modified_count, len(audio_files), "DRY-RUN" if dry_run else "WRITTEN")
    return len(audio_files), modified_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Retag curated list folders to clean release metadata.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview changes without writing")
    parser.add_argument("--no-dry-run", action="store_true", help="Apply tag updates")
    parser.add_argument("--as-compilation", action="store_true",
                        help="Retag as a unified Various Artists compilation in 0compilations/")
    parser.add_argument("--album-title", type=str, default=None,
                        help="Custom album title when using --as-compilation (defaults to folder name)")
    parser.add_argument("--folder", type=str, default=None, help="Target list folder under 0compilations/")
    parser.add_argument("--all", action="store_true", help="Retag all known curated list folders")
    parser.add_argument("--library-root", type=str, default=str(config.MUSIC_ROOT), help="Music library root")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    dry_run = not args.no_dry_run
    lib_root = Path(args.library_root)

    folders_to_process: list[Path] = []
    if args.all:
        for name in CURATED_LIST_FOLDERS:
            p = lib_root / "0compilations" / name
            if p.exists() and p.is_dir():
                folders_to_process.append(p)
            else:
                log.warning("Folder not found: %s", p)
    elif args.folder:
        folder_arg = Path(args.folder)
        if folder_arg.is_absolute():
            target = folder_arg
        elif (lib_root / "0compilations" / folder_arg).exists():
            target = lib_root / "0compilations" / folder_arg
        elif (lib_root / folder_arg).exists():
            target = lib_root / folder_arg
        else:
            target = folder_arg
        folders_to_process.append(target)
    else:
        parser.error("Must specify either --folder <name> or --all")

    library_index = {} if args.as_compilation else load_library_album_index()

    total_all = 0
    modified_all = 0
    for target in folders_to_process:
        tot, mod = retag_list_folder(
            target,
            library_index,
            dry_run=dry_run,
            as_compilation=args.as_compilation,
            album_title=args.album_title,
        )
        total_all += tot
        modified_all += mod

    log.info("\n================ ALL LISTS RETAG SUMMARY ================")
    log.info("  Processed lists: %d", len(folders_to_process))
    log.info("  Total tracks inspected: %d", total_all)
    log.info("  Total tracks modified: %d", modified_all)
    log.info("  Mode: %s", "DRY-RUN (no tags written)" if dry_run else "APPLIED TAG UPDATES")


if __name__ == "__main__":
    main()

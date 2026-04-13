#!/usr/bin/env python3
"""04_move.py — Three modes: intake, restructure, promote.

Modes are mutually exclusive:

- ``--intake``: route files from INCOMING_FOLDER into staging folders
- ``--restructure``: apply artist-folder threshold rule to existing collection
- ``--promote``: move staged albums from STAGED_ALBUMS_FOLDER to genre folders

Usage:
    python 04_move.py (--intake|--restructure|--promote)
                      [--dry-run] [--folder NAME] [--log FILE]
"""

import argparse
import json
import logging
import shutil
from pathlib import Path

import config
from lib.context import classify_folder
from lib.genres import normalise_genre, parent_genre
from lib.logger import setup_logger
from lib.tags import read_tags

log: logging.Logger = None  # type: ignore[assignment]

# Build reverse map: display genre → folder name
_GENRE_TO_FOLDER: dict[str, str] = {v.lower(): k for k, v in config.FOLDER_TO_GENRE.items()}


def resolve_target_folder(genre_tag: str) -> Path | None:
    """Return the target genre folder Path for the primary genre, or None."""
    primary = genre_tag.split("/")[0].strip()
    normed = normalise_genre(primary).lower()

    # Direct match
    folder_name = _GENRE_TO_FOLDER.get(normed)

    # Try parent genre
    if folder_name is None:
        par = parent_genre(primary)
        if par:
            folder_name = _GENRE_TO_FOLDER.get(par.lower())

    if folder_name is None:
        return None

    return config.MUSIC_ROOT / folder_name


def _first_audio_file(folder: Path) -> Path | None:
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in config.AUDIO_EXTENSIONS:
            return path
    return None


def _write_review_items(all_review: list[dict], *, dry_run: bool) -> None:
    if not all_review:
        return

    review_path = Path("review.json")
    existing: list[dict] = []
    if review_path.exists():
        try:
            existing = json.loads(review_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass

    if not dry_run:
        seen = {(e["path"], e["reason"]) for e in existing}
        for item in all_review:
            key = (item["path"], item["reason"])
            if key not in seen:
                existing.append(item)
                seen.add(key)
        review_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    log.info("Flagged %d items for review", len(all_review))


def _map_folder_name_from_genre(genre_tag: str) -> str | None:
    target_dir = resolve_target_folder(genre_tag)
    if target_dir is None:
        return None
    return target_dir.name


def _move_path(src: Path, dest: Path, *, dry_run: bool) -> None:
    if dry_run:
        log.info("[DRY-RUN] Would move: %s → %s", src, dest)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    log.info("Moved: %s → %s", src, dest)


def _remove_if_empty(folder: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            log.info("Removed empty directory: %s", folder)
    except OSError:
        pass


def _route_loose_file_from_intake(
    path: Path,
    *,
    dry_run: bool,
    review_items: list[dict],
) -> None:
    tags = read_tags(path)
    if tags is None:
        log.warning("Skipping unreadable file: %s", path)
        review_items.append({"path": str(path), "reason": "unreadable"})
        return

    genre_tag = tags.get("genre")
    if not genre_tag:
        log.warning("No genre tag for %s — flagging for review", path.name)
        review_items.append({"path": str(path), "reason": "no_genre"})
        return

    folder_name = _map_folder_name_from_genre(genre_tag)
    if folder_name is None:
        log.warning("No folder mapping for genre '%s' (%s) — flagging",
                    genre_tag, path.name)
        review_items.append({
            "path": str(path),
            "reason": "no_folder_mapping",
            "genre": genre_tag,
        })
        return

    dest = config.STAGED_TRACKS_FOLDER / folder_name / path.name
    if dest.exists():
        log.warning("Target exists, skipping: %s → %s", path, dest)
        review_items.append({
            "path": str(path),
            "reason": "move_conflict",
            "target": str(dest),
        })
        return

    _move_path(path, dest, dry_run=dry_run)


def _route_album_folder_from_intake(
    album_folder: Path,
    *,
    dry_run: bool,
    review_items: list[dict],
) -> None:
    sample = _first_audio_file(album_folder)
    if sample is None:
        log.warning("No audio files in intake album folder: %s", album_folder)
        review_items.append({
            "path": str(album_folder),
            "reason": "unknown_pattern",
        })
        return

    tags = read_tags(sample)
    if tags is None:
        log.warning("Skipping unreadable album folder: %s", album_folder)
        review_items.append({"path": str(album_folder), "reason": "unreadable"})
        return

    genre_tag = tags.get("genre")
    if not genre_tag:
        log.warning("No genre tag for album folder %s — flagging", album_folder)
        review_items.append({"path": str(album_folder), "reason": "no_genre"})
        return

    folder_name = _map_folder_name_from_genre(genre_tag)
    if folder_name is None:
        log.warning("No folder mapping for genre '%s' (%s) — flagging",
                    genre_tag, album_folder.name)
        review_items.append({
            "path": str(album_folder),
            "reason": "no_folder_mapping",
            "genre": genre_tag,
        })
        return

    dest = config.STAGED_ALBUMS_FOLDER / folder_name / album_folder.name
    if dest.exists():
        log.warning("Target exists, skipping: %s → %s", album_folder, dest)
        review_items.append({
            "path": str(album_folder),
            "reason": "move_conflict",
            "target": str(dest),
        })
        return

    _move_path(album_folder, dest, dry_run=dry_run)


def run_intake_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Route incoming loose files/albums into staging folders."""
    review_items: list[dict] = []

    if not config.INCOMING_FOLDER.exists():
        log.warning("INCOMING_FOLDER does not exist: %s", config.INCOMING_FOLDER)
        review_items.append({
            "path": str(config.INCOMING_FOLDER),
            "reason": "missing_folder",
        })
        return review_items

    if folder:
        candidates = [config.INCOMING_FOLDER / folder]
    else:
        candidates = sorted(config.INCOMING_FOLDER.iterdir())

    for item in candidates:
        if not item.exists():
            continue
        if item.is_dir():
            log.info("Intake album folder: %s", item)
            _route_album_folder_from_intake(item, dry_run=dry_run,
                                            review_items=review_items)
        elif item.is_file() and item.suffix.lower() in config.AUDIO_EXTENSIONS:
            log.info("Intake loose file: %s", item)
            _route_loose_file_from_intake(item, dry_run=dry_run,
                                          review_items=review_items)

    return review_items




# ── Artist folder threshold (N=3) ────────────────────────────────────────────

def _count_album_subdirs(folder: Path) -> int:
    """Count immediate child directories that classify as album."""
    count = 0
    for child in folder.iterdir():
        if child.is_dir() and classify_folder(child) == "album":
            count += 1
    return count


def _album_from_tags(folder: Path) -> dict[str, set[str]]:
    """Read album/year tags from audio files directly in *folder*."""
    albums: set[str] = set()
    years: set[str] = set()
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(f)
        if tags is None:
            continue
        album = tags.get("album") or ""
        if album:
            albums.add(album)
        year = (tags.get("year") or "")[:4]
        if year:
            years.add(year)
    return {"albums": albums, "years": years}


def _build_album_folder_name(artist: str, album: str, year: str | None) -> str:
    if year:
        return f"{artist} - {album} ({year})"
    return f"{artist} - {album}"


def restructure_artist_flat(
    folder: Path,
    genre_dir: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Apply N=3 threshold rule to an artist_flat folder."""
    artist = folder.name
    existing_albums = _count_album_subdirs(folder)
    tag_info = _album_from_tags(folder)
    albums = tag_info["albums"]
    years = tag_info["years"]

    audio_files = [
        f for f in sorted(folder.iterdir())
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    ]
    if not audio_files:
        return

    # Mixed album tags — cannot auto-decide
    if existing_albums == 0 and len(albums) > 1:
        log.warning("artist_flat with mixed album tags: %s — flagging for "
                     "review", folder)
        review_items.append({
            "path": str(folder),
            "reason": "artist_flat_mixed",
            "albums": sorted(albums),
        })
        return

    album_name = next(iter(albums)) if albums else "Unknown Album"
    year = next(iter(years)) if len(years) == 1 else None
    dest_folder_name = _build_album_folder_name(artist, album_name, year)

    if existing_albums == 0 and len(albums) <= 1:
        # Flatten to genre root: Artist - Album (Year)/
        dest_dir = genre_dir / dest_folder_name
    elif existing_albums < config.ARTIST_FOLDER_THRESHOLD:
        # Below threshold — create in genre root
        dest_dir = genre_dir / dest_folder_name
    else:
        # At or above threshold — create inside artist folder
        dest_dir = folder / dest_folder_name

    for f in audio_files:
        dest_path = dest_dir / f.name
        if dest_path.exists():
            log.warning("Target exists, skipping: %s → %s", f, dest_path)
            review_items.append({
                "path": str(f),
                "reason": "move_conflict",
                "target": str(dest_path),
            })
            continue
        if dry_run:
            log.info("[DRY-RUN] Would move: %s → %s", f.name, dest_path)
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest_path))
            log.info("Moved: %s → %s", f.name, dest_path)

    # Clean up empty artist folder (only if below threshold — files moved out)
    if not dry_run and existing_albums < config.ARTIST_FOLDER_THRESHOLD:
        try:
            if not any(folder.iterdir()):
                folder.rmdir()
                log.info("Removed empty artist folder: %s", folder)
        except OSError:
            pass


def walk_artist_folders(genre_dir: Path, **kwargs) -> list[dict]:
    """Find and restructure artist_flat folders in a genre directory."""
    review_items: list[dict] = []
    for child in sorted(genre_dir.iterdir()):
        if not child.is_dir():
            continue
        kind = classify_folder(child)
        if kind == "artist_flat":
            restructure_artist_flat(
                child, genre_dir, review_items=review_items, **kwargs)
        elif kind == "subgenre":
            # Also process artist_flat folders inside subgenre buckets
            items = walk_artist_folders(child, **kwargs)
            review_items.extend(items)
    return review_items


def run_restructure_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Apply N-threshold artist-folder restructuring to collection folders."""
    if folder:
        folders = [config.MUSIC_ROOT / folder]
    else:
        skip = config.SPECIAL_FOLDERS | config.SKIP_FOLDERS
        folders = sorted(
            p for p in config.MUSIC_ROOT.iterdir()
            if p.is_dir() and p.name.lower() not in skip
        )

    all_review: list[dict] = []
    for genre_dir in folders:
        log.info("Restructure folder: %s", genre_dir.name)
        items = walk_artist_folders(genre_dir, dry_run=dry_run)
        all_review.extend(items)

    return all_review


def run_promote_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Move staged albums from STAGED_ALBUMS_FOLDER into genre folders."""
    review_items: list[dict] = []

    if not config.STAGED_ALBUMS_FOLDER.exists():
        log.warning("STAGED_ALBUMS_FOLDER does not exist: %s",
                    config.STAGED_ALBUMS_FOLDER)
        review_items.append({
            "path": str(config.STAGED_ALBUMS_FOLDER),
            "reason": "missing_folder",
        })
        return review_items

    if folder:
        staged_genre_dirs = [config.STAGED_ALBUMS_FOLDER / folder]
    else:
        staged_genre_dirs = sorted(
            p for p in config.STAGED_ALBUMS_FOLDER.iterdir() if p.is_dir()
        )

    for staged_genre_dir in staged_genre_dirs:
        if not staged_genre_dir.exists() or not staged_genre_dir.is_dir():
            continue

        folder_name = staged_genre_dir.name
        if folder_name not in config.FOLDER_TO_GENRE:
            log.warning("Unknown staged genre folder: %s — flagging",
                        staged_genre_dir)
            review_items.append({
                "path": str(staged_genre_dir),
                "reason": "no_folder_mapping",
                "genre": folder_name,
            })
            continue

        target_genre_dir = config.MUSIC_ROOT / folder_name

        for album_folder in sorted(staged_genre_dir.iterdir()):
            if not album_folder.is_dir():
                continue

            dest = target_genre_dir / album_folder.name
            if dest.exists():
                log.warning("Target exists, skipping: %s → %s", album_folder, dest)
                review_items.append({
                    "path": str(album_folder),
                    "reason": "move_conflict",
                    "target": str(dest),
                })
                continue

            _move_path(album_folder, dest, dry_run=dry_run)

        _remove_if_empty(staged_genre_dir, dry_run=dry_run)

    return review_items


def main() -> None:
    global log

    parser = argparse.ArgumentParser(
        description="04_move modes: intake, restructure, promote")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--intake", action="store_true",
                      help="Route INCOMING_FOLDER files to staging folders")
    mode.add_argument("--restructure", action="store_true",
                      help="Apply artist folder threshold restructure")
    mode.add_argument("--promote", action="store_true",
                      help="Promote staged albums to main genre folders")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually move files")
    parser.add_argument("--folder", type=str, default=None,
                        help="Process a single top-level folder only")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log = setup_logger("04_move", Path(args.log))

    if args.intake:
        all_review = run_intake_mode(dry_run=dry_run, folder=args.folder)
    elif args.restructure:
        all_review = run_restructure_mode(dry_run=dry_run, folder=args.folder)
    else:
        all_review = run_promote_mode(dry_run=dry_run, folder=args.folder)

    _write_review_items(all_review, dry_run=dry_run)

    log.info("Done (%s)", "DRY-RUN" if dry_run else "LIVE")


if __name__ == "__main__":
    main()

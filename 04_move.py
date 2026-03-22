#!/usr/bin/env python3
"""04_move.py — Structural decisions: flatten/nest artist folders, route new files.

Routes files from ``0new/`` to the correct genre folder, and applies the N=3
artist-folder threshold rule to restructure artist folders.

Does NOT rename folders (that's ``03_folders.py``) or write tags (``01_tag.py``).

Usage:
    python 04_move.py [--dry-run] [--folder NAME] [--log FILE]
"""

import argparse
import importlib
import json
import logging
import shutil
from pathlib import Path

import config
from lib.genres import normalise_genre, parent_genre
from lib.logger import setup_logger
from lib.tags import read_tags

_folders_mod = importlib.import_module("03_folders")
classify_folder = _folders_mod.classify_folder

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


def move_file(
    path: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Move a single audio file to its genre folder."""
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

    target_dir = resolve_target_folder(genre_tag)
    if target_dir is None:
        log.warning("No folder mapping for genre '%s' (%s) — flagging",
                     genre_tag, path.name)
        review_items.append({
            "path": str(path),
            "reason": "no_folder_mapping",
            "genre": genre_tag,
        })
        return

    # Preserve album subfolder structure
    # e.g. Artist - Album (2002)/01 - Track.mp3
    album_folder = path.parent
    # Only keep the immediate album folder, not the entire tree
    dest_dir = target_dir / album_folder.name
    dest_path = dest_dir / path.name

    if dest_path == path:
        log.debug("Already in correct folder: %s", path)
        return

    if dest_path.exists():
        log.warning("Target exists, skipping: %s → %s", path, dest_path)
        review_items.append({
            "path": str(path),
            "reason": "move_conflict",
            "target": str(dest_path),
        })
        return

    if dry_run:
        log.info("[DRY-RUN] Would move: %s → %s", path, dest_path)
    else:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_path))
        log.info("Moved: %s → %s", path, dest_path)

        # Clean up empty source directory
        try:
            if album_folder != config.MUSIC_ROOT and not any(album_folder.iterdir()):
                album_folder.rmdir()
                log.info("Removed empty directory: %s", album_folder)
        except OSError:
            pass


def walk_folder(folder: Path, **kwargs) -> list[dict]:
    """Process all audio files under *folder*."""
    review_items: list[dict] = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and path.is_file():
            move_file(path, review_items=review_items, **kwargs)
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


def main() -> None:
    global log

    parser = argparse.ArgumentParser(
        description="Structural decisions: flatten/nest artist folders, "
                    "route new files.")
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

    if args.folder:
        folders = [config.MUSIC_ROOT / args.folder]
    else:
        # Never move files from SPECIAL_FOLDERS or SKIP_FOLDERS
        skip = config.SPECIAL_FOLDERS | config.SKIP_FOLDERS
        folders = sorted(
            p for p in config.MUSIC_ROOT.iterdir()
            if p.is_dir() and p.name.lower() not in skip
        )

    all_review: list[dict] = []
    for folder in folders:
        log.info("=== Folder: %s ===", folder.name)
        items = walk_folder(folder, dry_run=dry_run)
        all_review.extend(items)
        # Also apply N=3 threshold logic for artist_flat folders
        items = walk_artist_folders(folder, dry_run=dry_run)
        all_review.extend(items)

    if all_review:
        review_path = Path("review.json")
        existing: list[dict] = []
        if review_path.exists():
            try:
                existing = json.loads(review_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                pass

        if not dry_run:
            existing.extend(all_review)
            review_path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("Flagged %d files for review", len(all_review))

    log.info("Done (%s)", "DRY-RUN" if dry_run else "LIVE")


if __name__ == "__main__":
    main()

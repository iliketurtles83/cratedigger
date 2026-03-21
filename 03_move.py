#!/usr/bin/env python3
"""03_move.py — Move audio files to the genre folder based on their genre tag.

Reads the genre tag (first value before ``/``), maps it to a folder via
config.FOLDER_TO_GENRE (reversed), and moves the file.

Files in SPECIAL_FOLDERS are never moved.

Usage:
    python 03_move.py [--dry-run] [--folder NAME] [--log FILE]
"""

import argparse
import json
import logging
import shutil
from pathlib import Path

import config
from lib.genres import normalise_genre, parent_genre, CHILD_TO_PARENT
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


def move_file(
    path: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Move a single audio file to its genre folder."""
    tags = read_tags(path)
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


def main() -> None:
    global log

    parser = argparse.ArgumentParser(description="Move audio files to genre folders.")
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
    log = setup_logger("03_move", Path(args.log))

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

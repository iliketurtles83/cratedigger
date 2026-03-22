#!/usr/bin/env python3
"""02_rename.py — Normalise filenames using tags as source of truth.

Reads existing tags and renames audio files to the canonical format:
    ``01 - Artist Name - Song Title.ext``

Files in SPECIAL_FOLDERS are never renamed.

Usage:
    python 02_rename.py [--dry-run] [--folder NAME] [--log FILE]
"""

import argparse
import json
import logging
from pathlib import Path

import config
from lib.logger import setup_logger
from lib.parsers import build_filename, parse_filename
from lib.tags import read_tags

log: logging.Logger = None  # type: ignore[assignment]


def rename_file(
    path: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Rename a single audio file to canonical format using its tags."""
    tags = read_tags(path)
    if tags is None:
        log.warning("Skipping unreadable file: %s", path)
        review_items.append({"path": str(path), "reason": "unreadable"})
        return
    parsed = parse_filename(path)

    # Use tags as source of truth; fall back to filename parse
    artist = tags.get("artist") or parsed.get("artist")
    title = tags.get("title") or parsed.get("title")
    track = tags.get("track") or parsed.get("track")

    if not title:
        log.warning("No title for %s — flagging for review", path)
        review_items.append({"path": str(path), "reason": "no_title"})
        return

    new_name = build_filename(track, artist, title, ext=path.suffix)
    new_path = path.parent / new_name

    if new_path == path:
        log.debug("Already canonical: %s", path.name)
        return

    if new_path.exists():
        log.warning("Target already exists, skipping: %s → %s", path.name, new_name)
        review_items.append({
            "path": str(path),
            "reason": "rename_conflict",
            "target": str(new_path),
        })
        return

    if dry_run:
        log.info("[DRY-RUN] Would rename: %s → %s", path.name, new_name)
    else:
        path.rename(new_path)
        log.info("Renamed: %s → %s", path.name, new_name)


def walk_folder(folder: Path, **kwargs) -> list[dict]:
    """Process all audio files under *folder*."""
    review_items: list[dict] = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and path.is_file():
            rename_file(path, review_items=review_items, **kwargs)
    return review_items


def main() -> None:
    global log

    parser = argparse.ArgumentParser(description="Normalise audio filenames.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually rename files")
    parser.add_argument("--folder", type=str, default=None,
                        help="Process a single top-level folder only")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log = setup_logger("02_rename", Path(args.log))

    if args.folder:
        folders = [config.MUSIC_ROOT / args.folder]
    else:
        # Never rename files in SPECIAL_FOLDERS or SKIP_FOLDERS
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

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
import re
from collections import defaultdict
from pathlib import Path

import config
from lib.context import is_disc_subfolder
from lib.logger import setup_logger
from lib.parsers import build_filename, parse_filename
from lib.tags import read_tags

log: logging.Logger = None  # type: ignore[assignment]

# Folder name pattern indicating disc number, e.g. "Album (disc 1)" or "[disc 2]"
_DISC_IN_FOLDER_NAME = re.compile(r"[\(\[]\s*disc\s*\d+\s*[\)\]]", re.IGNORECASE)


def _parse_disc_value(disc_str: str | None) -> tuple[int | None, int | None]:
    """Parse disc tag value like '1', '1/2' into (disc_number, disc_total)."""
    if not disc_str:
        return (None, None)
    parts = disc_str.split("/")
    try:
        num = int(parts[0].strip())
    except (ValueError, IndexError):
        return (None, None)
    total = None
    if len(parts) > 1:
        try:
            total = int(parts[1].strip())
        except ValueError:
            pass
    return (num, total)


def _build_disc_context(
    file_tags: dict[Path, dict[str, str | None]],
) -> set[Path]:
    """Determine which files in a single parent folder should use disc prefix.

    Multi-disc is indicated when:
    1. The parent folder is a disc subfolder (CD1, Disc 2, etc.)
    2. The folder name contains a disc indicator like (disc 1)
    3. The disc tag has total > 1 (e.g., disc 1/2)
    4. Sibling files with the same album tag have different disc numbers
    """
    if not file_tags:
        return set()

    parent = next(iter(file_tags)).parent

    # Rule 1: parent is a disc subfolder → all files get disc prefix
    if is_disc_subfolder(parent.name):
        return set(file_tags)

    # Rule 2: folder name contains disc indicator like (disc 1)
    if _DISC_IN_FOLDER_NAME.search(parent.name):
        return set(file_tags)

    # Group by album tag, check disc diversity per album
    use_disc: set[Path] = set()
    albums: dict[str, list[tuple[Path, int | None, int | None]]] = defaultdict(list)
    for path, tags in file_tags.items():
        album_key = (tags.get("album") or "").strip().lower()
        disc_raw = tags.get("disc") or parse_filename(path).get("disc")
        num, total = _parse_disc_value(disc_raw)
        albums[album_key].append((path, num, total))

    for album_key, entries in albums.items():
        disc_numbers = {num for _, num, _ in entries if num is not None}
        has_total_gt_1 = any(
            total is not None and total > 1 for _, _, total in entries
        )

        # Rule 3: disc total > 1, or Rule 4: multiple disc numbers
        if has_total_gt_1 or len(disc_numbers) > 1:
            for path, _, _ in entries:
                use_disc.add(path)

    return use_disc


def rename_file(
    path: Path,
    *,
    tags: dict[str, str | None],
    use_disc: bool,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Rename a single audio file to canonical format using its tags."""
    parsed = parse_filename(path)

    # Use tags as source of truth; fall back to filename parse
    artist = tags.get("artist") or parsed.get("artist")
    title = tags.get("title") or parsed.get("title")
    track = tags.get("track") or parsed.get("track")

    # Only include disc in filename when multi-disc context is confirmed
    disc = (tags.get("disc") or parsed.get("disc")) if use_disc else None

    if not title:
        log.warning("No title for %s — flagging for review", path)
        review_items.append({"path": str(path), "reason": "no_title"})
        return

    new_name = build_filename(track, artist, title, ext=path.suffix, disc=disc)
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

    # Collect all audio files and group by parent directory
    audio_files = sorted(
        p for p in folder.rglob("*")
        if p.suffix.lower() in config.AUDIO_EXTENSIONS and p.is_file()
    )

    by_parent: dict[Path, list[Path]] = defaultdict(list)
    for path in audio_files:
        by_parent[path.parent].append(path)

    # Process each parent group with shared disc context
    for parent in sorted(by_parent):
        paths = by_parent[parent]

        # Read tags for all files in this parent
        file_tags: dict[Path, dict[str, str | None]] = {}
        for path in paths:
            tags = read_tags(path)
            if tags is None:
                log.warning("Skipping unreadable file: %s", path)
                review_items.append({"path": str(path), "reason": "unreadable"})
            else:
                file_tags[path] = tags

        # Determine which files need disc prefix
        use_disc_paths = _build_disc_context(file_tags)

        for path in paths:
            if path in file_tags:
                rename_file(
                    path,
                    tags=file_tags[path],
                    use_disc=(path in use_disc_paths),
                    review_items=review_items,
                    **kwargs,
                )

    return review_items


def _review_item_key(item: dict) -> str:
    """Return stable identity key for review item dedupe."""
    payload = {
        key: item.get(key)
        for key in sorted(item)
        if key not in {"path", "reason"}
    }
    return json.dumps(
        {
            "path": item.get("path"),
            "reason": item.get("reason"),
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


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
            seen = {
                _review_item_key(e)
                for e in existing
                if isinstance(e, dict)
            }
            for item in all_review:
                key = _review_item_key(item)
                if key not in seen:
                    existing.append(item)
                    seen.add(key)
            review_path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("Flagged %d files for review", len(all_review))

    log.info("Done (%s)", "DRY-RUN" if dry_run else "LIVE")


if __name__ == "__main__":
    main()

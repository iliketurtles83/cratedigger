#!/usr/bin/env python3
"""05_review.py — Interactively resolve files flagged in review.json.

Reads review.json and presents each flagged item for manual resolution.

Usage:
    python 05_review.py [--dry-run] [--log FILE]
"""

import argparse
import json
import logging
from pathlib import Path

import config
from lib.genres import merge_genres, normalise_genre
from lib.logger import setup_logger
from lib.tags import read_tags, write_tags

log: logging.Logger = None  # type: ignore[assignment]

REVIEW_FILE = Path("review.json")


def load_review() -> list[dict]:
    """Load the current review queue."""
    if not REVIEW_FILE.exists():
        return []
    try:
        return json.loads(REVIEW_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return []


def save_review(items: list[dict], *, dry_run: bool = True) -> None:
    """Persist the review queue."""
    if dry_run:
        log.info("[DRY-RUN] Would save %d remaining review items", len(items))
        return
    REVIEW_FILE.write_text(
        json.dumps(items, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def handle_no_genre(item: dict, *, dry_run: bool) -> bool:
    """Handle a file flagged with no genre. Returns True if resolved."""
    path = Path(item["path"])
    if not path.exists():
        log.warning("File no longer exists: %s", path)
        return True

    tags = read_tags(path)
    if tags is None:
        print(f"  [Cannot read tags from file]")
        tags = {}
    print(f"\n--- No genre: {path} ---")
    print(f"  Current tags: artist={tags.get('artist')}, title={tags.get('title')}")
    print(f"  Current genre: {tags.get('genre')}")
    print()
    print("  Options:")
    print("    [g] Set genre manually")
    print("    [s] Skip (keep in review)")
    print("    [d] Dismiss (remove from review)")

    choice = input("  Choice: ").strip().lower()

    if choice == "g":
        genre = input("  Enter genre: ").strip()
        if genre:
            normed = normalise_genre(genre)
            if dry_run:
                log.info("[DRY-RUN] Would set genre = %s for %s", normed, path.name)
            else:
                write_tags(path, {"genre": normed}, dry_run=False)
                log.info("Set genre = %s for %s", normed, path.name)
            return True
    elif choice == "d":
        return True

    return False


def handle_rename_conflict(item: dict, *, dry_run: bool) -> bool:
    """Handle a rename conflict. Returns True if resolved."""
    path = Path(item["path"])
    target = item.get("target", "?")
    print(f"\n--- Rename conflict: {path} ---")
    print(f"  Target already exists: {target}")
    print()
    print("  Options:")
    print("    [r] Rename with suffix")
    print("    [s] Skip")
    print("    [d] Dismiss")

    choice = input("  Choice: ").strip().lower()

    if choice == "r":
        p = Path(target)
        new_name = p.stem + "_2" + p.suffix
        new_path = p.parent / new_name
        if dry_run:
            log.info("[DRY-RUN] Would rename %s → %s", path.name, new_name)
        else:
            if path.exists():
                path.rename(new_path)
                log.info("Renamed: %s → %s", path.name, new_name)
        return True
    elif choice == "d":
        return True

    return False


def handle_generic(item: dict, *, dry_run: bool) -> bool:
    """Handle any other flagged item."""
    path = item.get("path", "?")
    reason = item.get("reason", "unknown")
    print(f"\n--- Review item: {path} ---")
    print(f"  Reason: {reason}")
    for k, v in item.items():
        if k not in ("path", "reason"):
            print(f"  {k}: {v}")
    print()
    print("  Options:")
    print("    [d] Dismiss (remove from review)")
    print("    [s] Skip (keep in review)")

    choice = input("  Choice: ").strip().lower()
    return choice == "d"


def handle_artist_flat_proposal(item: dict, *, dry_run: bool) -> bool:
    """Handle an artist_flat rename proposal. Returns True if resolved."""
    path = Path(item["path"])
    target = item.get("target", "?")
    print(f"\n--- artist_flat rename proposal: {path} ---")
    print(f"  Proposed: {target}")
    print()
    print("  Options:")
    print("    [a] Apply rename")
    print("    [s] Skip (keep in review)")
    print("    [d] Dismiss (remove from review)")

    choice = input("  Choice: ").strip().lower()

    if choice == "a":
        target_path = Path(target)
        if target_path.exists():
            log.warning("Target already exists: %s", target_path)
            return False
        if dry_run:
            log.info("[DRY-RUN] Would rename %s → %s", path, target_path)
        else:
            if path.exists():
                path.rename(target_path)
                log.info("Renamed: %s → %s", path, target_path)
        return True
    elif choice == "d":
        return True

    return False


HANDLERS = {
    "no_genre": handle_no_genre,
    "no_title": handle_generic,
    "rename_conflict": handle_rename_conflict,
    "move_conflict": handle_generic,
    "no_folder_mapping": handle_no_genre,
    "mixed_tags": handle_generic,
    "incomplete_metadata": handle_generic,
    "artist_flat_mixed": handle_generic,
    "artist_flat_incomplete": handle_generic,
    "artist_flat_rename_proposal": handle_artist_flat_proposal,
    "artist_flat_rename_conflict": handle_rename_conflict,
}


def main() -> None:
    global log

    parser = argparse.ArgumentParser(description="Resolve flagged review items.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually apply changes")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log = setup_logger("05_review", Path(args.log))

    items = load_review()
    if not items:
        log.info("No items to review.")
        return

    log.info("Loaded %d review items", len(items))
    remaining: list[dict] = []

    for item in items:
        reason = item.get("reason", "unknown")
        handler = HANDLERS.get(reason, handle_generic)
        resolved = handler(item, dry_run=dry_run)
        if not resolved:
            remaining.append(item)

    save_review(remaining, dry_run=dry_run)
    log.info("Done — %d items remaining (%s)", len(remaining),
             "DRY-RUN" if dry_run else "LIVE")


if __name__ == "__main__":
    main()

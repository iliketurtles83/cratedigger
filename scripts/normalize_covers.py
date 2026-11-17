#!/usr/bin/env python3
"""normalize_covers.py — Normalize album cover art filenames for music player compatibility.

The music player only looks for embedded art, cover.jpg, and folder.jpg.
This script scans album directories for non-standard cover filenames (such as
ripper-annotated images, e.g. "0413. Talking Heads - More Songs About Buildings And Food.jpg",
or "front.jpg", "cover.jpeg") and normalises them to "cover.jpg".

Rules:
1. If cover.jpg already exists, no renaming is necessary.
2. If cover.jpeg exists, rename it to cover.jpg.
3. If exactly one image is found in an album directory, rename it to cover.jpg.
4. If multiple images are found, prioritize front-cover candidates (front.jpg, albumart.jpg)
   and rename the best candidate to cover.jpg. If ambiguous, flag for review.

Usage:
    python scripts/normalize_covers.py [--dry-run] [--folder NAME]
    python scripts/normalize_covers.py --no-dry-run [--folder NAME]
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("normalize_covers")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_FRONT_PATTERNS = [
    re.compile(r"^front\b", re.IGNORECASE),
    re.compile(r"^cover\b", re.IGNORECASE),
    re.compile(r"^folder\b", re.IGNORECASE),
    re.compile(r"^albumart\b", re.IGNORECASE),
]


def is_album_directory(folder: Path) -> bool:
    """Check if folder contains audio files directly."""
    try:
        return any(
            f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
            for f in folder.iterdir()
        )
    except (OSError, PermissionError):
        return False


def find_image_files(folder: Path) -> list[Path]:
    """Find image files directly under folder."""
    try:
        return sorted([
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
        ])
    except (OSError, PermissionError):
        return []


def select_cover_candidate(images: list[Path]) -> tuple[Path | None, str]:
    """Select the best cover candidate from a list of images.

    Returns (chosen_image, reason).
    """
    if not images:
        return None, "no_images"

    # Already canonical
    for img in images:
        if img.name.lower() in {"cover.jpg", "folder.jpg"}:
            return None, "already_canonical"

    # If cover.jpeg exists
    for img in images:
        if img.name.lower() == "cover.jpeg":
            return img, "extension_fix"

    # Filter out known tiny WMP thumbnails if larger images exist
    non_thumbnails = [
        img for img in images
        if not re.search(r"albumartsmall\.jpg$|albumart_.*_small\.jpg$", img.name, re.IGNORECASE)
    ]
    candidate_pool = non_thumbnails if non_thumbnails else images

    # Exactly one image in the album directory
    if len(candidate_pool) == 1:
        return candidate_pool[0], "single_image"

    # Check for front/cover pattern matches (e.g. "Front.jpg", "Zelda_Front.jpg", "AlbumArt_Large.jpg")
    _WORD_FRONT_PATTERNS = [
        re.compile(r"(?:^|[\s._-])front(?:[\s._-]|$)", re.IGNORECASE),
        re.compile(r"(?:^|[\s._-])cover(?:[\s._-]|$)", re.IGNORECASE),
        re.compile(r"(?:^|[\s._-])folder(?:[\s._-]|$)", re.IGNORECASE),
        re.compile(r"albumart.*large", re.IGNORECASE),
    ]

    for pat in _WORD_FRONT_PATTERNS:
        matches = [img for img in candidate_pool if pat.search(img.stem)]
        if len(matches) == 1:
            return matches[0], "front_pattern_match"

    return None, "ambiguous_multiple_images"


def normalize_cover_in_folder(
    folder: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict] | None = None,
) -> bool:
    """Normalize cover art in a single album folder. Returns True if a rename occurred/would occur."""
    images = find_image_files(folder)
    candidate, reason = select_cover_candidate(images)

    if reason == "ambiguous_multiple_images":
        log.warning("Multiple ambiguous images in %s: %s", folder, [i.name for i in images])
        if review_items is not None:
            review_items.append({
                "path": str(folder),
                "reason": "ambiguous_cover_art",
                "images": [i.name for i in images],
            })
        return False

    if not candidate:
        return False

    target_ext = ".jpg" if candidate.suffix.lower() in {".jpg", ".jpeg"} else candidate.suffix.lower()
    target_name = f"cover{target_ext}"
    target_path = folder / target_name

    if candidate == target_path:
        return False

    if target_path.exists():
        log.warning("Target %s already exists, cannot rename %s", target_path.name, candidate.name)
        return False

    if dry_run:
        log.info("[DRY-RUN] Would rename: %s → %s", candidate.name, target_name)
    else:
        candidate.rename(target_path)
        log.info("Renamed: %s → %s in %s", candidate.name, target_name, folder.name)

    return True


def scan_and_normalize_covers(
    root: Path,
    *,
    dry_run: bool = True,
) -> tuple[int, int, list[dict]]:
    """Recursively scan root for album directories and normalize cover art."""
    albums_checked = 0
    covers_renamed = 0
    review_items: list[dict] = []

    for path in sorted(root.rglob("*")):
        if not path.is_dir():
            continue
        if is_album_directory(path):
            albums_checked += 1
            if normalize_cover_in_folder(path, dry_run=dry_run, review_items=review_items):
                covers_renamed += 1

    return albums_checked, covers_renamed, review_items


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize album cover art filenames to cover.jpg.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview renames without executing")
    parser.add_argument("--no-dry-run", action="store_true", help="Actually rename cover art files")
    parser.add_argument("--folder", type=str, default=None, help="Process a single folder only")
    parser.add_argument("--library-root", type=str, default=str(config.MUSIC_ROOT), help="Library root path")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    lib_root = Path(args.library_root)

    if args.folder:
        target = lib_root / args.folder if not Path(args.folder).is_absolute() else Path(args.folder)
        if not target.exists():
            log.error("Target path does not exist: %s", target)
            sys.exit(1)
        scan_root = target
    else:
        scan_root = lib_root

    log.info("Scanning for cover art in %s (dry_run=%s)...", scan_root, dry_run)
    checked, renamed, review_items = scan_and_normalize_covers(scan_root, dry_run=dry_run)

    log.info("\n=== Cover Art Normalization Summary ===")
    log.info("  Album directories checked: %d", checked)
    log.info("  Covers renamed / proposed: %d", renamed)
    log.info("  Ambiguous folders flagged: %d", len(review_items))

    if review_items and dry_run:
        log.info("[DRY-RUN] Would append %d review item(s) to review.json", len(review_items))
    elif review_items:
        review_file = Path("review.json")
        try:
            existing = json.loads(review_file.read_text(encoding="utf-8")) if review_file.exists() else []
            existing.extend(review_items)
            review_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            log.info("Appended %d review items to %s", len(review_items), review_file)
        except Exception as exc:
            log.warning("Could not write review.json: %s", exc)


if __name__ == "__main__":
    main()

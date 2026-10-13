#!/usr/bin/env python3
"""sync_beets_db.py — Synchronize Beets library.db with on-disk audio tags.

Reads tags directly from audio files on disk and commits the refreshed metadata
into Beets' SQLite database (library.db). Resolves album artist leaks, phantom
curator artists, and collapsed albums.

Usage:
    python scripts/sync_beets_db.py [--all-lists] [--faves] [--query QUERY]
"""

import argparse
import logging
import sys
from pathlib import Path

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from beets import config as b_config
from scripts.convert_lists import CURATED_LIST_FOLDERS

log = logging.getLogger("sync_beets_db")


def sync_items(items, lib) -> int:
    """Read on-disk tags for *items* and commit updates to beets library."""
    updated = 0
    with lib.transaction():
        for item in items:
            try:
                if not Path(item.path.decode("utf-8", errors="ignore")).exists():
                    continue
                old_aa = item.albumartist
                old_alb = item.album
                item.read()
                if item.albumartist != old_aa or item.album != old_alb:
                    item.store()
                    updated += 1
                else:
                    item.store()
            except Exception as exc:
                log.warning("Could not sync item %s: %s", getattr(item, "title", "unknown"), exc)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync beets library.db with on-disk tags.")
    parser.add_argument("--config", type=str, default="config/beets.yaml", help="Path to beets config")
    parser.add_argument("--all-lists", action="store_true", help="Sync all curated list folders in 0compilations")
    parser.add_argument("--faves", action="store_true", help="Sync 0faves folder")
    parser.add_argument("--query", type=str, default=None, help="Custom beets query")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Load beets configuration
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        log.error("Beets config not found: %s", config_path)
        sys.exit(1)

    b_config.set_file(str(config_path))
    from beets.library import Library

    lib = Library(b_config["library"].as_filename(), b_config["directory"].as_filename())
    log.info("Opened beets library: %s (directory: %s)", lib.path, lib.directory)

    # Gather items to sync
    matched_items = []
    if args.query:
        matched_items.extend(lib.items(args.query))
    else:
        target_folders = set()
        if args.all_lists:
            target_folders.update(f.lower() for f in CURATED_LIST_FOLDERS)
        if args.faves:
            target_folders.add("0faves")

        if not target_folders:
            target_folders.update(f.lower() for f in CURATED_LIST_FOLDERS)
            target_folders.add("0faves")

        log.info("Filtering beets library for %d target folders...", len(target_folders))
        for item in lib.items():
            p_str = item.path.decode("utf-8", errors="ignore").lower()
            if any(fn in p_str for fn in target_folders):
                matched_items.append(item)

    # Deduplicate items by ID
    unique_items = {item.id: item for item in matched_items}.values()
    log.info("Found %d items in beets DB to sync", len(unique_items))

    if not unique_items:
        log.info("No items to sync.")
        return

    updated_count = sync_items(unique_items, lib)
    log.info("Successfully updated %d / %d items in beets library.db", updated_count, len(unique_items))


if __name__ == "__main__":
    main()

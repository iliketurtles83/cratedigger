#!/usr/bin/env python3
"""Reconcile split artist spellings across the library and Beets database.

Finds artist names that differ only by case, leading articles ('The'),
punctuation, or '&' vs 'and' (e.g. 'R.E.M.' vs 'REM', 'The Prodigy' vs 'Prodigy',
'Simon & Garfunkel' vs 'Simon and Garfunkel'), selects the canonical spelling,
and normalises them.

Usage:
    python scripts/reconcile_artists.py [--dry-run] [--fix] [--min-tracks N]
"""

import argparse
from collections import Counter, defaultdict
import logging
from pathlib import Path
import sqlite3
import sys

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.parsers import artist_comparison_key, title_case_clean
from lib.tags import read_tags, write_tags

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("reconcile_artists")


def get_beets_db_path() -> Path:
    return Path(getattr(config, "BEETS_DB_PATH", Path.home() / ".config" / "beets" / "library.db"))


def find_split_artists(db_path: Path) -> dict[str, list[tuple[str, int, int]]]:
    """Return map of comparison_key -> list of (spelling, track_count, mb_count)."""
    if not db_path.exists():
        log.error("Beets database not found at %s", db_path)
        return {}

    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    query = """
    SELECT artist, COUNT(*), SUM(CASE WHEN mb_artistid IS NOT NULL AND mb_artistid != '' THEN 1 ELSE 0 END)
    FROM items
    WHERE artist IS NOT NULL AND TRIM(artist) != ''
    GROUP BY artist
    """
    rows = c.execute(query).fetchall()
    conn.close()

    groups = defaultdict(list)
    for artist, count, mb_count in rows:
        key = artist_comparison_key(artist)
        if key:
            groups[key].append((artist, count, mb_count or 0))

    # Keep only groups with more than one spelling
    return {k: v for k, v in groups.items() if len(v) > 1}


def choose_canonical_spelling(variants: list[tuple[str, int, int]]) -> str:
    """Choose the best canonical spelling among variants.

    Priority:
    1. Variant with the most MusicBrainz matched tracks (mb_count).
    2. Majority track count across the library.
    3. Proper title-casing.
    """
    # Sort by: (mb_count, track_count, not all-lowercase, length)
    def score(item):
        spelling, count, mb_count = item
        has_upper = any(c.isupper() for c in spelling)
        return (mb_count, count, has_upper, len(spelling))

    best = max(variants, key=score)
    canonical = " ".join(best[0].split()).strip()

    # If the chosen variant is all lowercase, clean it up
    if canonical.islower():
        canonical = title_case_clean(canonical) or canonical
    elif best[2] == 0:
        # No MusicBrainz match in group: normalize title-casing of short words
        cleaned = title_case_clean(canonical)
        if cleaned:
            canonical = cleaned

    return canonical


def reconcile_artists(*, dry_run: bool = True, fix: bool = False, min_tracks: int = 1) -> None:
    db_path = get_beets_db_path()
    splits = find_split_artists(db_path)

    log.info("Found %d artist groups with multiple spellings in %s", len(splits), db_path)
    total_reconciled_tracks = 0
    total_reconciled_groups = 0

    conn = sqlite3.connect(db_path) if fix and not dry_run else None

    for key, variants in sorted(splits.items()):
        total_group_tracks = sum(v[1] for v in variants)
        if total_group_tracks < min_tracks:
            continue

        canonical = choose_canonical_spelling(variants)
        non_canonical = [v for v in variants if v[0] != canonical]
        if not non_canonical:
            continue

        affected_tracks = sum(v[1] for v in non_canonical)
        total_reconciled_tracks += affected_tracks
        total_reconciled_groups += 1

        variant_summary = ", ".join(f"{v[0]} ({v[1]} trk)" for v in variants)
        prefix = "[DRY-RUN] Would reconcile" if dry_run else "Reconciling"
        log.info("%s: %s -> %r (%d tracks to update)", prefix, variant_summary, canonical, affected_tracks)

        if fix and not dry_run and conn:
            c = conn.cursor()
            for old_spelling, count, _ in non_canonical:
                # Update files on disk for both artist and albumartist
                item_rows = c.execute(
                    "SELECT path, artist, albumartist FROM items WHERE artist = ? OR albumartist = ?",
                    (old_spelling, old_spelling),
                ).fetchall()
                for p_bytes, item_art, item_alb_art in item_rows:
                    path_str = p_bytes.decode("utf-8", "replace") if isinstance(p_bytes, bytes) else str(p_bytes)
                    p = Path(path_str)
                    if not p.is_absolute():
                        p = config.MUSIC_ROOT / p
                    if p.exists():
                        tags_to_update = {}
                        if item_art == old_spelling:
                            tags_to_update["artist"] = canonical
                        if item_alb_art == old_spelling:
                            tags_to_update["albumartist"] = canonical
                        if tags_to_update:
                            try:
                                write_tags(p, tags_to_update, dry_run=False)
                            except Exception as exc:
                                log.warning("Could not write tag to %s: %s", p, exc)

                # Update in beets DB (items and albums)
                c.execute("UPDATE items SET artist = ? WHERE artist = ?", (canonical, old_spelling))
                c.execute("UPDATE items SET albumartist = ? WHERE albumartist = ?", (canonical, old_spelling))
                c.execute("UPDATE albums SET albumartist = ? WHERE albumartist = ?", (canonical, old_spelling))
                log.info("  Updated beets DB: %r -> %r", old_spelling, canonical)
            conn.commit()

    if conn:
        conn.close()

    log.info("Summary: %d split groups, %d tracks affected. (dry_run=%s, fix=%s)",
             total_reconciled_groups, total_reconciled_tracks, dry_run, fix)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate reconciliation (default)")
    parser.add_argument("--no-dry-run", action="store_false", dest="dry_run", help="Execute reconciliation")
    parser.add_argument("--fix", action="store_true", help="Apply updates to disk tags and beets DB")
    parser.add_argument("--min-tracks", type=int, default=1, help="Minimum total tracks in group to consider")
    args = parser.parse_args()

    reconcile_artists(dry_run=args.dry_run and not args.fix, fix=args.fix, min_tracks=args.min_tracks)


if __name__ == "__main__":
    main()

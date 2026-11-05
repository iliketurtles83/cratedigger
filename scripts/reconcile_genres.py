#!/usr/bin/env python3
"""Audit and reconcile genre tags across the audio library.

Normalises casing, fixes known aliases, prunes blacklisted junk tokens
(e.g., 'Other', 'Unknown', 'Audio'), and deduplicates multi-value tags.

Usage:
    python scripts/reconcile_genres.py
    python scripts/reconcile_genres.py --dry-run
    python scripts/reconcile_genres.py --folder "A" --dry-run
    python scripts/reconcile_genres.py --fix --no-dry-run
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import logging
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.genres import (
    CHILD_TO_PARENT,
    GENRE_BLACKLIST,
    ROOT_GENRES,
    is_meaningful_genre,
    normalise_genre,
    parent_genre,
)
from lib.logger import setup_logger
from lib.tags import read_tags, write_tags

log = logging.getLogger("reconcile_genres")


def reconcile_genre_string(genre_str: str | None) -> tuple[str | None, list[str]]:
    """Clean, normalise, and format as 2-Tier Root / Subgenre.

    Returns:
        (canonical_genre_string, removed_tokens)
    """
    if not genre_str:
        return None, []

    tokens = [t.strip() for t in re.split(r"[/\\|;]", genre_str) if t.strip()]
    cleaned_tokens: list[str] = []
    removed_tokens: list[str] = []
    seen_lower: set[str] = set()

    for tok in tokens:
        if not is_meaningful_genre(tok):
            removed_tokens.append(tok)
            continue

        norm = normalise_genre(tok)
        if not norm or not is_meaningful_genre(norm):
            removed_tokens.append(tok)
            continue

        subparts = [p.strip() for p in norm.split("/") if p.strip()]
        for sp in subparts:
            low = sp.lower()
            if is_meaningful_genre(sp) and low not in seen_lower:
                seen_lower.add(low)
                cleaned_tokens.append(sp)

    if not cleaned_tokens:
        return None, removed_tokens

    # Group into roots and subgenres
    roots: list[str] = []
    subgenres: list[str] = []

    for t in cleaned_tokens:
        if t in ROOT_GENRES:
            if t not in roots:
                roots.append(t)
        else:
            p = parent_genre(t)
            if p and p not in roots:
                roots.append(p)
            if t not in subgenres:
                subgenres.append(t)

    # Assemble 2-Tier: Roots first (max 2), followed by Subgenres (total max 3)
    final_tokens: list[str] = []
    for r in roots:
        if len(final_tokens) < 2 and r not in final_tokens:
            final_tokens.append(r)
    for s in subgenres:
        if len(final_tokens) < 3 and s not in final_tokens:
            final_tokens.append(s)

    if not final_tokens:
        final_tokens = cleaned_tokens[:3]

    new_genre = " / ".join(final_tokens) if final_tokens else None
    return new_genre, removed_tokens


def run_genre_reconciliation(
    *,
    dry_run: bool = True,
    folder: str | None = None,
    report_path: Path | None = None,
) -> dict:
    """Scan library and reconcile genre tags."""
    if folder:
        scan_root = config.MUSIC_ROOT / folder
    else:
        scan_root = config.MUSIC_ROOT

    if not scan_root.exists():
        log.error("Scan target does not exist: %s", scan_root)
        return {"error": f"Path not found: {scan_root}"}

    log.info("Starting genre reconciliation scan under: %s", scan_root)
    log.info("Mode: %s", "DRY-RUN (no files will be modified)" if dry_run else "LIVE (writing tags)")

    total_scanned = 0
    total_with_genre = 0
    total_unchanged = 0
    total_updated = 0
    total_cleared = 0

    transformations = Counter()
    removed_token_counter = Counter()
    all_old_tokens = Counter()
    all_new_tokens = Counter()

    changed_files: list[dict] = []

    # Iteration over audio files
    for path in sorted(scan_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue

        total_scanned += 1
        tags = read_tags(path)
        if tags is None:
            continue

        old_genre = tags.get("genre")
        if not old_genre:
            continue

        total_with_genre += 1
        for part in old_genre.split("/"):
            p = part.strip()
            if p:
                all_old_tokens[p] += 1

        new_genre, removed = reconcile_genre_string(old_genre)

        if removed:
            for r in removed:
                removed_token_counter[r] += 1

        if new_genre:
            for part in new_genre.split("/"):
                p = part.strip()
                if p:
                    all_new_tokens[p] += 1

        if old_genre == new_genre:
            total_unchanged += 1
            continue

        total_updated += 1
        transformations[(old_genre, new_genre or "<CLEARED>")] += 1
        if new_genre is None:
            total_cleared += 1

        rel_path = str(path.relative_to(config.MUSIC_ROOT))
        changed_files.append({
            "path": rel_path,
            "old_genre": old_genre,
            "new_genre": new_genre,
            "removed": removed,
        })

        if not dry_run:
            try:
                write_tags(path, {"genre": new_genre or ""}, dry_run=False)
            except Exception as e:
                log.warning("Failed writing genre tags to %s: %s", path, e)
                continue

    log.info("Finished scan of %d audio files.", total_scanned)
    log.info("  Files with existing genre: %d", total_with_genre)
    log.info("  Genres already clean:      %d", total_unchanged)
    log.info("  Genres needing update:     %d", total_updated)
    if total_cleared:
        log.info("  Genres cleared (all junk): %d", total_cleared)

    print("\n" + "=" * 60)
    print(f"GENRE RECONCILIATION SUMMARY ({'DRY-RUN' if dry_run else 'LIVE'})")
    print("=" * 60)
    print(f"Total audio files scanned:      {total_scanned:6d}")
    print(f"Files with genre tags:          {total_with_genre:6d}")
    print(f"Files already canonical:        {total_unchanged:6d}")
    print(f"Files to update:                {total_updated:6d}")
    print(f"Unique genre tokens BEFORE:     {len(all_old_tokens):6d}")
    print(f"Unique genre tokens AFTER:      {len(all_new_tokens):6d}")

    if removed_token_counter:
        print("\n--- Top Junk Tokens Removed ---")
        for tok, cnt in removed_token_counter.most_common(15):
            print(f"  {cnt:5d}  '{tok}'")

    print("\n--- Top 25 Genre Transformations ---")
    for (old, new), cnt in transformations.most_common(25):
        print(f"  {cnt:5d}  '{old}'  -->  '{new}'")

    summary = {
        "dry_run": dry_run,
        "scan_root": str(scan_root),
        "total_scanned": total_scanned,
        "total_with_genre": total_with_genre,
        "total_unchanged": total_unchanged,
        "total_updated": total_updated,
        "total_cleared": total_cleared,
        "unique_tokens_before": len(all_old_tokens),
        "unique_tokens_after": len(all_new_tokens),
        "top_transformations": [
            {"old": old, "new": new, "count": cnt}
            for (old, new), cnt in transformations.most_common(50)
        ],
        "top_removed_tokens": [
            {"token": tok, "count": cnt}
            for tok, cnt in removed_token_counter.most_common(30)
        ],
    }

    if report_path:
        report_data = {
            "summary": summary,
            "sample_changed_files": changed_files[:200],
        }
        report_path.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Saved report to %s", report_path)
        print(f"\nWrote full report to: {report_path}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile genre tags library-wide.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview changes without modifying files (default)")
    parser.add_argument("--no-dry-run", action="store_false", dest="dry_run", help="Execute tag writes")
    parser.add_argument("--fix", action="store_true", help="Required with --no-dry-run to write tags")
    parser.add_argument("--folder", type=str, default=None, help="Specific folder under MUSIC_ROOT to process")
    parser.add_argument("--report", type=str, default="genre_reconciliation_report.json", help="Path to write JSON report")

    args = parser.parse_args()
    setup_logger("reconcile_genres")

    if not args.dry_run and not args.fix:
        parser.error("Refusing to modify files without --fix. Use --fix --no-dry-run to execute.")

    report_file = Path(args.report) if args.report else None
    run_genre_reconciliation(
        dry_run=args.dry_run,
        folder=args.folder,
        report_path=report_file,
    )


if __name__ == "__main__":
    main()

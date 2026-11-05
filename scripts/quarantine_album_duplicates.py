#!/usr/bin/env python3
"""scripts/quarantine_album_duplicates.py — Quarantine identical audio duplicates from album folders.

Finds files in the same album folder sharing the exact same audio_hash (e.g. unnumbered
leftovers, curly apostrophe variants, CD-ripper placeholders) and moves the redundant
copy to <MUSIC_ROOT>/0review/duplicates/<relpath>, recording a manifest.

Usage:
    python scripts/quarantine_album_duplicates.py [--dry-run]
    python scripts/quarantine_album_duplicates.py --fix --no-dry-run [--folder FOLDER]
"""

import argparse
import json
import logging
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pandas as pd

import config
from lib.audiohash import audio_hash
from lib.logger import setup_logger
from lib.parsers import parse_filename
from lib.path_updates import update_downstream_paths
from lib.tags import read_tags

log = logging.getLogger("quarantine_album_dups")


def score_track_candidate(path: Path) -> int:
    """Score track candidate: higher score means higher preference to keep."""
    name = path.name
    score = 0

    # 1. Has track number prefix like "02 - " or "1-01 - "
    if re.match(r"^(?:\d+-)?\d{1,2}\s*-\s*", name):
        score += 20
        # Penalize crazy 3-digit track numbers like "101 - " from CD-rippers
        if re.match(r"^10\d\s*-\s*", name):
            score -= 15

    # 2. Check for placeholder names
    lower = name.lower()
    if any(p in lower for p in ("audiotrack", "track 0", "track 1", "unknown")):
        score -= 50

    # 3. Prefer standard straight apostrophe over curly apostrophe
    if "'" in name and "’" not in name:
        score += 5
    elif "’" in name:
        score -= 5

    # 4. Prefer standard Picard "feat." over "ft."
    if "feat." in lower or "featuring" in lower:
        score += 3
    elif "ft." in lower:
        score += 1

    # 5. Penalize truncated titles ending with unfinished words or parenthesis
    if re.search(r"\([^\)]*$", name) or name.endswith("..."):
        score -= 20

    # 6. File size tie-breaker (longer tags / embedded art)
    try:
        score += min(path.stat().st_size // 100000, 5)
    except OSError:
        pass

    return score


def find_album_folders(music_root: Path, target_folder: str | None = None) -> list[Path]:
    """Discover album folders across letter buckets."""
    buckets = list(getattr(config, "LETTER_BUCKETS", set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))) + [
        getattr(config, "SYMBOL_BUCKET", "#")
    ]
    album_folders = []

    for b in sorted(buckets):
        b_dir = music_root / b
        if not b_dir.exists():
            continue
        for artist_dir in b_dir.iterdir():
            if not artist_dir.is_dir():
                continue
            if target_folder and target_folder.lower() not in (b.lower(), artist_dir.name.lower()):
                continue
            for folder in artist_dir.iterdir():
                if not folder.is_dir() or folder.name == "0singles":
                    continue
                if target_folder and target_folder.lower() not in (
                    b.lower(),
                    artist_dir.name.lower(),
                    folder.name.lower(),
                ):
                    continue
                album_folders.append(folder)

    return sorted(album_folders)


def quarantine_album_duplicates(
    music_root: Path,
    quarantine_root: Path,
    *,
    dry_run: bool = True,
    fix: bool = False,
    target_folder: str | None = None,
) -> dict:
    """Find and quarantine identical audio duplicates within album folders."""
    features_path = getattr(config, "FEATURES_STORE_PATH", Path("features.pkl"))
    path_to_hash: dict[str, str] = {}
    if features_path.exists():
        try:
            df = pd.read_pickle(features_path)
            path_to_hash = dict(zip(df["path"], df["audio_hash"]))
            log.info("Loaded %d precomputed hashes from %s", len(path_to_hash), features_path)
        except Exception as exc:
            log.warning("Could not read features.pkl: %s", exc)

    album_folders = find_album_folders(music_root, target_folder)
    log.info("Scanning %d album folders for duplicate audio files...", len(album_folders))

    quarantined_records = []
    path_updates: dict[Path, Path] = {}
    total_quarantined = 0

    for folder in album_folders:
        audio_files = [
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
        ]
        if len(audio_files) <= 1:
            continue

        hash_groups = defaultdict(list)
        for f in audio_files:
            h = path_to_hash.get(str(f))
            if not h:
                try:
                    h = audio_hash(f)
                    path_to_hash[str(f)] = h
                except Exception as exc:
                    log.warning("Could not compute hash for %s: %s", f, exc)
                    continue
            if h:
                hash_groups[h].append(f)

        for h, files in hash_groups.items():
            if len(files) <= 1:
                continue

            # Sort by candidate score descending
            scored = sorted(files, key=score_track_candidate, reverse=True)
            canonical = scored[0]
            to_quarantine = scored[1:]

            log.info("\nDuplicate group in %s (hash: %s):", folder.relative_to(music_root), h)
            log.info("  [KEEP]        %s (score: %d)", canonical.name, score_track_candidate(canonical))

            for dup in to_quarantine:
                total_quarantined += 1
                rel = dup.relative_to(music_root)
                dest = quarantine_root / rel

                log.info("  [QUARANTINE]  %s (score: %d) -> %s", dup.name, score_track_candidate(dup), dest)

                record = {
                    "quarantined_file": str(rel),
                    "canonical_file": str(canonical.relative_to(music_root)),
                    "audio_hash": h,
                    "quarantine_path": str(dest),
                    "quarantined_at": datetime.now(timezone.utc).isoformat(),
                }
                quarantined_records.append(record)

                if fix and not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dup), str(dest))
                    path_updates[dup] = canonical

    # Apply downstream updates to features.pkl and playlists
    if fix and not dry_run and path_updates:
        update_downstream_paths(path_updates, music_root=music_root, logger=log)

    # Save manifest
    manifest_path = quarantine_root / "manifest.json"
    if fix and not dry_run and quarantined_records:
        quarantine_root.mkdir(parents=True, exist_ok=True)
        existing_manifest = []
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    existing_manifest = json.load(f)
            except Exception:
                existing_manifest = []
        combined_manifest = existing_manifest + quarantined_records
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(combined_manifest, f, indent=2)
        log.info("Wrote quarantine manifest: %s (%d total entries)", manifest_path, len(combined_manifest))

    mode_label = "LIVE" if (fix and not dry_run) else "DRY-RUN"
    log.info("\n=== Quarantine Summary (%s) ===", mode_label)
    log.info("Album folders scanned:    %d", len(album_folders))
    log.info("Duplicate sets detected:  %d", len(quarantined_records))
    log.info("Tracks to quarantine:     %d", total_quarantined)

    return {
        "mode": mode_label,
        "scanned_folders": len(album_folders),
        "quarantined_count": total_quarantined,
        "records": quarantined_records,
    }


def main() -> None:
    global log
    parser = argparse.ArgumentParser(
        description="Quarantine identical audio duplicates from album folders into 0review/duplicates."
    )
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate quarantine (default)")
    parser.add_argument("--no-dry-run", action="store_true", help="Execute file quarantine")
    parser.add_argument("--fix", action="store_true", help="Move duplicate files to quarantine directory")
    parser.add_argument("--folder", type=str, default=None, help="Process a single artist, album, or bucket")
    parser.add_argument("--quarantine-dir", type=str, default=None, help="Custom quarantine directory")
    args = parser.parse_args()

    log = setup_logger("quarantine_album_dups", Path("review.log"))

    music_root = Path(getattr(config, "MUSIC_ROOT", Path("/media/jack/buffer/audio")))
    quarantine_root = Path(args.quarantine_dir) if args.quarantine_dir else music_root / "0review" / "duplicates"

    dry_run = not args.no_dry_run
    quarantine_album_duplicates(
        music_root,
        quarantine_root,
        dry_run=dry_run,
        fix=args.fix,
        target_folder=args.folder,
    )


if __name__ == "__main__":
    main()

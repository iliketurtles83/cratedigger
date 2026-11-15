#!/usr/bin/env python3
"""repair_bpm.py — Audit and repair corrupt, beatless, or mis-estimated BPM tags.

Scans audio files (or a target folder), re-estimates tempo using the shared
Essentia RhythmExtractor2013 estimator in ``lib/tempo.py``, and reports or
repairs:
1. Corrupt BPM tags outside [35, 220] BPM (e.g. 1 or 12704).
2. False BPM tags on beatless / spoken / ambient tracks (low beat confidence).
3. Mis-estimated BPM tags that diverge by more than --tolerance (default 10%)
   from a high-confidence Essentia estimate (confidence >= --min-confidence).

Respects the ``lib/audiohash.py`` contract: skips ``.ogg``, ``.opus``, and
``.wma`` files by default because retagging them alters ``audio_hash``.

Usage:
    python scripts/repair_bpm.py --dry-run --folder M
    python scripts/repair_bpm.py --dry-run --folder H/Headspace --report reports/bpm/headspace.json
    python scripts/repair_bpm.py --fix --no-dry-run --folder H/Headspace
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.context import get_folder_context
from lib.logger import setup_logger
from lib.tags import read_tags, write_tags
from lib.tempo import estimate_tempo, sanitize_bpm_tag

log: logging.Logger = logging.getLogger("repair_bpm")

WHOLE_FILE_HASH_EXTENSIONS = {".ogg", ".opus", ".wma"}


def evaluate_bpm_repair(
    path: Path,
    *,
    tolerance: float = 0.10,
    min_repair_confidence: float = 1.2,
    clear_beatless: bool = True,
    fill_missing: bool = False,
) -> dict:
    """Inspect *path* and return a repair decision record (no writes performed)."""
    tags = read_tags(path)
    if tags is None:
        return {"path": str(path), "action": "skip_unreadable"}

    raw_bpm = tags.get("bpm")
    sane_old = sanitize_bpm_tag(raw_bpm)
    genre = tags.get("genre")

    if raw_bpm is None and not fill_missing:
        return {"path": str(path), "action": "unchanged_no_tag"}

    res = estimate_tempo(path, genre=genre, octave_fold=True)
    if res.method == "error":
        return {"path": str(path), "action": "skip_error", "old_bpm": raw_bpm}

    new_bpm_int = round(res.bpm) if res.bpm is not None else None

    if raw_bpm is not None and sane_old is None:
        # Existing BPM tag is corrupt (e.g. 1 or 12704)
        if new_bpm_int is not None:
            return {
                "path": str(path),
                "action": "replace_corrupt",
                "old_bpm": raw_bpm,
                "new_bpm": str(new_bpm_int),
                "confidence": res.confidence,
                "genre": genre,
            }
        return {
            "path": str(path),
            "action": "clear_corrupt",
            "old_bpm": raw_bpm,
            "new_bpm": "",
            "confidence": res.confidence,
            "genre": genre,
        }

    if raw_bpm is None and fill_missing:
        if new_bpm_int is not None:
            return {
                "path": str(path),
                "action": "fill_missing",
                "old_bpm": None,
                "new_bpm": str(new_bpm_int),
                "confidence": res.confidence,
                "genre": genre,
            }
        return {"path": str(path), "action": "unchanged_beatless", "confidence": res.confidence}

    # Existing BPM tag is within [35, 220]
    if new_bpm_int is None:
        if clear_beatless and res.confidence is not None:
            return {
                "path": str(path),
                "action": "clear_beatless",
                "old_bpm": raw_bpm,
                "new_bpm": "",
                "confidence": res.confidence,
                "genre": genre,
            }
        return {
            "path": str(path),
            "action": "keep_low_conf",
            "old_bpm": raw_bpm,
            "confidence": res.confidence,
        }

    if res.confidence is not None and res.confidence < min_repair_confidence:
        return {
            "path": str(path),
            "action": "keep_uncertain",
            "old_bpm": raw_bpm,
            "candidate_bpm": str(new_bpm_int),
            "confidence": res.confidence,
        }

    assert sane_old is not None
    rel_diff = abs(new_bpm_int - sane_old) / max(float(new_bpm_int), 1.0)
    if rel_diff > tolerance:
        return {
            "path": str(path),
            "action": "update_mismatch",
            "old_bpm": raw_bpm,
            "new_bpm": str(new_bpm_int),
            "confidence": res.confidence,
            "rel_diff": round(rel_diff, 3),
            "genre": genre,
        }

    return {
        "path": str(path),
        "action": "unchanged_ok",
        "old_bpm": raw_bpm,
        "confidence": res.confidence,
    }


def _iter_target_files(root: Path, *, include_whole_hash: bool = False) -> tuple[list[Path], int]:
    """Yield eligible audio files under *root*, skipping SKIP_FOLDERS and whole-hash formats."""
    files: list[Path] = []
    skipped_whole_hash = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in config.AUDIO_EXTENSIONS:
            continue
        ctx = get_folder_context(path)
        if ctx.skip or not ctx.needs_bpm:
            continue
        if not include_whole_hash and suffix in WHOLE_FILE_HASH_EXTENSIONS:
            skipped_whole_hash += 1
            continue
        files.append(path)
    return files, skipped_whole_hash


def _iter_suspect_files_from_features(
    features_path: Path,
    *,
    include_whole_hash: bool = False,
) -> tuple[list[Path], int]:
    """Select files from *features_path* whose existing bpm_tag looks corrupt or suspect."""
    df = pd.read_pickle(features_path)
    tagged = df[df["error"].isna() & df["path"].notna() & df["bpm_tag"].notna()].copy()
    g = tagged["genre_tag"].fillna("")
    bpm = tagged["bpm_tag"]

    suspect_mask = (
        (bpm < 35.0)
        | (bpm > 220.0)
        | g.str.contains("Meditation|Spoken|Speech|Audiobook", case=False, regex=True)
        | (g.str.contains("Ambient", case=False, regex=False) & (bpm > 150.0))
        | (g.str.contains("Hip-Hop", case=False, regex=False) & (bpm > 160.0))
        | (g.str.contains("Drum & Bass|Jungle", case=False, regex=True) & (bpm < 120.0))
        | (g.str.contains("Techno|Trance", case=False, regex=True) & (bpm < 105.0))
    )
    suspects = tagged[suspect_mask]

    files: list[Path] = []
    skipped_whole_hash = 0
    seen: set[str] = set()
    for p_str in suspects["path"]:
        p = Path(str(p_str))
        if str(p) in seen or not p.is_file():
            continue
        seen.add(str(p))
        suffix = p.suffix.lower()
        if suffix not in config.AUDIO_EXTENSIONS:
            continue
        ctx = get_folder_context(p)
        if ctx.skip or not ctx.needs_bpm:
            continue
        if not include_whole_hash and suffix in WHOLE_FILE_HASH_EXTENSIONS:
            skipped_whole_hash += 1
            continue
        files.append(p)
    return files, skipped_whole_hash


def _eval_worker(args_tuple: tuple[Path, float, float, bool, bool]) -> dict:
    path, tolerance, min_repair_confidence, clear_beatless, fill_missing = args_tuple
    return evaluate_bpm_repair(
        path,
        tolerance=tolerance,
        min_repair_confidence=min_repair_confidence,
        clear_beatless=clear_beatless,
        fill_missing=fill_missing,
    )


def apply_report(
    report_path: Path,
    *,
    dry_run: bool = True,
    actions: Optional[set[str]] = None,
    include_whole_hash: bool = False,
) -> dict:
    """Apply (or preview) BPM tag edits from a previously generated JSON report."""
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    changes = payload.get("changes", [])
    applied = 0
    skipped = 0
    for rec in changes:
        action = rec.get("action")
        if actions and action not in actions:
            skipped += 1
            continue
        path = Path(rec["path"])
        if not path.is_file():
            skipped += 1
            continue
        if not include_whole_hash and path.suffix.lower() in WHOLE_FILE_HASH_EXTENSIONS:
            skipped += 1
            continue
        new_bpm = rec.get("new_bpm")
        if new_bpm is None:
            skipped += 1
            continue
        prefix = "[DRY-RUN] " if dry_run else ""
        log.info(
            "%s%s: %s (%r -> %r, conf=%s)",
            prefix, action, path.name, rec.get("old_bpm"), new_bpm, rec.get("confidence"),
        )
        if not dry_run:
            write_tags(path, {"bpm": str(new_bpm)}, dry_run=False)
        applied += 1
    log.info("Report apply complete (dry_run=%s): applied=%d, skipped=%d", dry_run, applied, skipped)
    return {"dry_run": dry_run, "applied": applied, "skipped": skipped}


def run_repair(
    target_root: Optional[Path],
    *,
    dry_run: bool = True,
    tolerance: float = 0.10,
    min_repair_confidence: float = 1.2,
    clear_beatless: bool = True,
    fill_missing: bool = False,
    include_whole_hash: bool = False,
    limit: Optional[int] = None,
    report_path: Optional[Path] = None,
    suspect_only: bool = False,
    features_path: Path = config.FEATURES_STORE_PATH,
    workers: int = 1,
) -> dict:
    """Audit and optionally repair BPM tags under *target_root* or suspect features."""
    if suspect_only:
        files, skipped_whole_hash = _iter_suspect_files_from_features(
            features_path, include_whole_hash=include_whole_hash
        )
        source_label = f"suspect:{features_path}"
    else:
        assert target_root is not None
        files, skipped_whole_hash = _iter_target_files(
            target_root, include_whole_hash=include_whole_hash
        )
        source_label = str(target_root)

    if limit is not None and limit > 0:
        files = files[:limit]

    log.info(
        "Scanning %d audio file(s) from %s (skipped whole-hash: %d, dry_run=%s, workers=%d)",
        len(files), source_label, skipped_whole_hash, dry_run, workers,
    )

    counts: dict[str, int] = {}
    changes: list[dict] = []
    repair_actions = {
        "replace_corrupt",
        "clear_corrupt",
        "clear_beatless",
        "update_mismatch",
        "fill_missing",
    }

    if workers > 1 and len(files) > 1:
        tasks = [
            (p, tolerance, min_repair_confidence, clear_beatless, fill_missing)
            for p in files
        ]
        with ProcessPoolExecutor(max_workers=workers) as executor:
            records = list(executor.map(_eval_worker, tasks, chunksize=4))
    else:
        records = [
            evaluate_bpm_repair(
                p,
                tolerance=tolerance,
                min_repair_confidence=min_repair_confidence,
                clear_beatless=clear_beatless,
                fill_missing=fill_missing,
            )
            for p in files
        ]

    for idx, rec in enumerate(records, start=1):
        path = Path(rec["path"])
        action = rec["action"]
        counts[action] = counts.get(action, 0) + 1

        if action in repair_actions:
            changes.append(rec)
            prefix = "[DRY-RUN] " if dry_run else ""
            log.info(
                "%s[%d/%d] %s: %s (%r -> %r, conf=%s)",
                prefix, idx, len(files), action, path.name,
                rec.get("old_bpm"), rec.get("new_bpm"), rec.get("confidence"),
            )
            if not dry_run:
                write_tags(path, {"bpm": rec["new_bpm"]}, dry_run=False)

    summary = {
        "target_root": source_label,
        "dry_run": dry_run,
        "scanned": len(files),
        "skipped_whole_hash": skipped_whole_hash,
        "counts": counts,
        "changes": changes,
    }

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log.info("Wrote report to %s", report_path)

    log.info("Summary: scanned=%d, proposed_or_applied=%d, counts=%s", len(files), len(changes), counts)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and repair BPM tags using Essentia")
    parser.add_argument("--folder", type=str, default=None,
                        help="Folder relative to MUSIC_ROOT (or absolute path)")
    parser.add_argument("--suspect-only", action="store_true", default=False,
                        help="Target only corrupt/suspect BPM-tagged tracks from features.pkl")
    parser.add_argument("--features", type=Path, default=config.FEATURES_STORE_PATH,
                        help="Feature store pickle for --suspect-only (default: config.FEATURES_STORE_PATH)")
    parser.add_argument("--apply-report", type=Path, default=None,
                        help="Apply changes from a previously saved JSON report")
    parser.add_argument("--actions", nargs="*", default=None,
                        help="Optional action filter when using --apply-report")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview repairs without modifying tags (default)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="Enable tag writes (must be paired with --fix)")
    parser.add_argument("--fix", action="store_true", default=False,
                        help="Explicitly authorize tag repairs when combined with --no-dry-run")
    parser.add_argument("--tolerance", type=float, default=0.10,
                        help="Minimum relative BPM difference to trigger repair (default: 0.10)")
    parser.add_argument("--min-confidence", type=float, default=1.2,
                        help="Minimum Essentia confidence to overwrite an existing valid BPM (default: 1.2)")
    parser.add_argument("--no-clear-beatless", dest="clear_beatless", action="store_false", default=True,
                        help="Keep existing BPM tags on low-confidence beatless tracks")
    parser.add_argument("--fill-missing", action="store_true", default=False,
                        help="Also write BPM tags on files currently missing a BPM tag")
    parser.add_argument("--include-whole-hash", action="store_true", default=False,
                        help="Include .ogg/.opus/.wma files (WARNING: changes audio_hash)")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                        help="Worker processes for read-only evaluation (default: half CPU count)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of eligible files to inspect")
    parser.add_argument("--report", type=Path, default=None,
                        help="Optional JSON report path (e.g. reports/bpm/audit.json)")
    parser.add_argument("--log", type=Path, default=None,
                        help="Log file path")
    args = parser.parse_args()

    setup_logger("repair_bpm", log_file=args.log)

    if not args.dry_run and not args.fix:
        parser.error("--no-dry-run requires --fix to authorize writing BPM tags")

    if args.apply_report is not None:
        if not args.apply_report.exists():
            log.error("Report file does not exist: %s", args.apply_report)
            sys.exit(1)
        apply_report(
            args.apply_report,
            dry_run=args.dry_run,
            actions=set(args.actions) if args.actions else None,
            include_whole_hash=args.include_whole_hash,
        )
        return

    target_root: Optional[Path] = None
    if not args.suspect_only:
        if args.folder:
            candidate = Path(args.folder)
            target_root = candidate if candidate.is_absolute() else config.MUSIC_ROOT / candidate
        else:
            target_root = config.MUSIC_ROOT

        if not target_root.exists():
            log.error("Target folder does not exist: %s", target_root)
            sys.exit(1)

    run_repair(
        target_root,
        dry_run=args.dry_run,
        tolerance=args.tolerance,
        min_repair_confidence=args.min_confidence,
        clear_beatless=args.clear_beatless,
        fill_missing=args.fill_missing,
        include_whole_hash=args.include_whole_hash,
        limit=args.limit,
        report_path=args.report,
        suspect_only=args.suspect_only,
        features_path=args.features,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()

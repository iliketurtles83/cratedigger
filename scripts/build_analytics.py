#!/usr/bin/env python3
"""Preview or build the UI analytics snapshot from a completed feature store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from cratedigger.analytics import DEFAULT_SNAPSHOT_PATH, build_snapshot
from cratedigger.jobs import external_analysis_running, external_backfill_running


def _parse_source_state(value: str) -> tuple[int, int, int, int]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError("Invalid feature-store identity") from exc
    if not isinstance(parsed, list) or len(parsed) != 4 or any(type(item) is not int or item < 0 for item in parsed):
        raise argparse.ArgumentTypeError("Invalid feature-store identity")
    return parsed[0], parsed[1], parsed[2], parsed[3]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=config.FEATURES_STORE_PATH, help="Feature store to snapshot")
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT_PATH, help="SQLite snapshot destination")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview without writing a snapshot (default)")
    mode.add_argument("--no-dry-run", action="store_true", help="Atomically publish the snapshot")
    parser.add_argument("--expected-source-state", type=_parse_source_state, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if external_analysis_running(REPO_ROOT):
        parser.error("Feature extraction is running; wait for it to finish before reading the feature store")
    if external_backfill_running(REPO_ROOT):
        parser.error("A feature backfill is running; wait for it to finish before reading the feature store")
    summary = build_snapshot(
        args.features, args.output, dry_run=not args.no_dry_run, expected_source_state=args.expected_source_state
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

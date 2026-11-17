#!/usr/bin/env python3
"""Preview or apply a reviewed recommender feature-store backfill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from cratedigger.feature_backfill import run_guarded_backfill
from cratedigger.jobs import external_analysis_running, external_backfill_running
from scripts.build_analytics import _parse_source_state


def _terminate(_signum: int, _frame: object) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise SystemExit("Backfill cancelled; check the feature store for a possible late publication")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    kind = parser.add_mutually_exclusive_group(required=True)
    kind.add_argument("--hashes", action="store_true", help="Backfill missing audio hashes")
    kind.add_argument("--embeddings", action="store_true", help="Backfill missing or stale embeddings")
    parser.add_argument("--features", type=Path, default=config.FEATURES_STORE_PATH, help="Feature store to backfill")
    parser.add_argument("--max-embeddings", type=int, help="Maximum number of embeddings to compute")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Inspect candidates without changing the store (default)")
    mode.add_argument("--no-dry-run", action="store_true", help="Apply a matching reviewed preview")
    parser.add_argument("--expected-plan", help="SHA-256 from the reviewed preview; required with --no-dry-run")
    parser.add_argument("--expected-source-state", type=_parse_source_state, help=argparse.SUPPRESS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _terminate)
    summary = run_guarded_backfill(
        args.features,
        "hashes" if args.hashes else "embeddings",
        max_embeddings=args.max_embeddings,
        dry_run=not args.no_dry_run,
        expected_plan=args.expected_plan,
        expected_source_state=args.expected_source_state,
        extraction_running=lambda: external_analysis_running(REPO_ROOT) or external_backfill_running(REPO_ROOT),
    )
    if not summary["dry_run"]:
        print("@@backfill-result " + json.dumps(summary, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

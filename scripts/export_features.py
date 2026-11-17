#!/usr/bin/env python3
"""Preview or publish a validated player export without changing the feature store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from cratedigger.feature_export import DEFAULT_EXPORT_PATH, build_player_export
from cratedigger.jobs import external_analysis_running
from scripts.build_analytics import _parse_source_state


def _parse_output_state(value: str) -> tuple[int, int, int, int] | None:
    return None if value == "null" else _parse_source_state(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=config.FEATURES_STORE_PATH, help="Feature store to export")
    parser.add_argument("--output", type=Path, default=DEFAULT_EXPORT_PATH, help="JSON or gzip JSON destination")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate a temporary export without publishing (default)")
    mode.add_argument("--no-dry-run", action="store_true", help="Atomically publish the validated export")
    parser.add_argument("--expected-source-state", type=_parse_source_state, help=argparse.SUPPRESS)
    parser.add_argument("--expected-output-state", type=_parse_output_state, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    args = parser.parse_args()
    summary = build_player_export(
        args.features,
        args.output,
        dry_run=not args.no_dry_run,
        expected_source_state=args.expected_source_state,
        expected_output_state=getattr(args, "expected_output_state", None),
        check_output_state=hasattr(args, "expected_output_state"),
        extraction_running=lambda: external_analysis_running(REPO_ROOT),
    )
    print("@@export-result " + json.dumps(summary, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()

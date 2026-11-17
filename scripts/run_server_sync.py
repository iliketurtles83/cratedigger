#!/usr/bin/env python3
"""Preview or apply a reviewed complete-library mirror sync."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cratedigger.server_sync import run_guarded_sync
from scripts.build_analytics import _parse_source_state


def _destination_state(value: str) -> tuple[int, int, int, int] | None:
    return None if value == "null" else _parse_source_state(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Complete library mirror source")
    parser.add_argument("--dest", required=True, help="Existing local directory or user@host:/absolute/path")
    parser.add_argument("--complete-source", action="store_true", help="Confirm the source is a complete mirror")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview the mirror without changing the destination (default)")
    mode.add_argument("--no-dry-run", action="store_true", help="Apply a matching reviewed preview")
    parser.add_argument("--expected-plan", help="SHA-256 from a reviewed preview; required with --no-dry-run")
    parser.add_argument("--expected-source-state", type=_parse_source_state, help=argparse.SUPPRESS)
    parser.add_argument("--expected-destination-state", type=_destination_state, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--protected-library-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    run_guarded_sync(
        args.source,
        args.dest,
        complete_source=args.complete_source,
        dry_run=not args.no_dry_run,
        expected_plan=args.expected_plan,
        expected_source_state=args.expected_source_state,
        expected_destination_state=getattr(args, "expected_destination_state", None),
        check_destination_state=hasattr(args, "expected_destination_state"),
        protected_library_root=args.protected_library_root,
    )


if __name__ == "__main__":
    main()

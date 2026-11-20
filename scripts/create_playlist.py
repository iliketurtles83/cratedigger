#!/usr/bin/env python3
"""Validate or create a new M3U8 from a reviewed seed-playlist report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from cratedigger.playlist_builder import create_playlist


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="Reviewed playlist JSON report")
    parser.add_argument("--library-root", type=Path, default=config.MUSIC_ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate without writing a playlist (default)")
    mode.add_argument("--no-dry-run", action="store_true", help="Create a new playlist; never overwrite")
    args = parser.parse_args()
    result = create_playlist(args.report, args.library_root, dry_run=not args.no_dry_run)
    print("@@playlist-result " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

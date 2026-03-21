#!/usr/bin/env python3
"""intake.py — Orchestrate 01_tag → 02_rename → 03_move for files in 0new/.

Processes new files arriving in ``0new/albums/`` and ``0new/singles/``,
running the full pipeline automatically.

Usage:
    python intake.py [--dry-run] [--overwrite] [--log FILE]
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import config
from lib.logger import setup_logger

log: logging.Logger = None  # type: ignore[assignment]

INTAKE_FOLDER = config.MUSIC_ROOT / "0new"


def run_step(script: str, extra_args: list[str]) -> int:
    """Run a pipeline script as a subprocess, returning its exit code."""
    cmd = [sys.executable, script] + extra_args
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error("%s exited with code %d", script, result.returncode)
    return result.returncode


def main() -> None:
    global log

    parser = argparse.ArgumentParser(description="Intake pipeline for 0new/.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually apply all changes")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing artist/title tags from MB")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log = setup_logger("intake", Path(args.log))

    if not INTAKE_FOLDER.exists():
        log.info("Intake folder does not exist: %s", INTAKE_FOLDER)
        return

    # Check for audio files in 0new/
    audio_files = [
        p for p in INTAKE_FOLDER.rglob("*")
        if p.suffix.lower() in config.AUDIO_EXTENSIONS and p.is_file()
    ]

    if not audio_files:
        log.info("No audio files found in %s", INTAKE_FOLDER)
        return

    log.info("Found %d audio files in %s", len(audio_files), INTAKE_FOLDER)

    # Build common args
    common_args = ["--folder", "0new", "--log", args.log]
    if not dry_run:
        common_args.append("--no-dry-run")

    # Step 1: Tag
    tag_args = common_args[:]
    if args.overwrite:
        tag_args.append("--overwrite")
    rc = run_step("01_tag.py", tag_args)
    if rc != 0:
        log.error("Tagging failed — stopping pipeline")
        return

    # Step 2: Rename (only in non-special context — 0new is special,
    # but we rename before moving out)
    # We pass --folder 0new so it processes that folder specifically
    rc = run_step("02_rename.py", common_args)
    if rc != 0:
        log.error("Renaming failed — stopping pipeline")
        return

    # Step 3: Move to genre folders
    rc = run_step("03_move.py", common_args)
    if rc != 0:
        log.error("Moving failed — stopping pipeline")
        return

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info("Intake complete (%s) — processed %d files", mode, len(audio_files))


if __name__ == "__main__":
    main()

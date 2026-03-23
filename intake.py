#!/usr/bin/env python3
"""intake.py — Orchestrate 01_tag → 02_rename → 04_move --intake for incoming.

Processes new files arriving in ``INCOMING_FOLDER``, running the intake pipeline
automatically.

Usage:
    python intake.py [--dry-run] [--overwrite] [--log FILE]
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

import config
from lib.logger import setup_logger

log: logging.Logger = None  # type: ignore[assignment]


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

    parser = argparse.ArgumentParser(
        description="Intake pipeline for INCOMING_FOLDER.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually apply all changes")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing artist/title tags from MB")
    parser.add_argument("--fix-suspicious", action="store_true",
                        help="Replace suspicious placeholder values only")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log = setup_logger("intake", Path(args.log))

    intake_folder = config.INCOMING_FOLDER

    if not intake_folder.exists():
        log.info("Intake folder does not exist: %s", intake_folder)
        return

    # Check for audio files in incoming folder
    audio_files = [
        p for p in intake_folder.rglob("*")
        if p.suffix.lower() in config.AUDIO_EXTENSIONS and p.is_file()
    ]

    if not audio_files:
        log.info("No audio files found in %s", intake_folder)
        return

    log.info("Found %d audio files in %s", len(audio_files), intake_folder)

    # Build common args
    incoming_folder_name = intake_folder.name
    common_args = ["--folder", incoming_folder_name, "--log", args.log]
    if not dry_run:
        common_args.append("--no-dry-run")

    # Step 1: Tag
    tag_args = common_args[:]
    if args.overwrite:
        tag_args.append("--overwrite")
    if args.fix_suspicious:
        tag_args.append("--fix-suspicious")
    rc = run_step("01_tag.py", tag_args)
    if rc != 0:
        log.error("Tagging failed — stopping pipeline")
        return

    # Step 2: Rename files in incoming folder
    rc = run_step("02_rename.py", common_args)
    if rc != 0:
        log.error("Renaming failed — stopping pipeline")
        return

    # Step 3: Intake routing to staging folders
    move_args = ["--intake", "--log", args.log]
    if not dry_run:
        move_args.append("--no-dry-run")
    rc = run_step("04_move.py", move_args)
    if rc != 0:
        log.error("Moving failed — stopping pipeline")
        return

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info("Intake complete (%s) — processed %d files", mode, len(audio_files))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""intake.py — Tag and organize files arriving in INCOMING_FOLDER.

The default flow uses beets only for release matching and metadata, then lets
cratedigger own all filename and path changes:

    beets metadata → enrich_covers → 02_rename → 03_folders → 04_move --to-artist-root

The beets import uses a transient catalog. The historical persistent beets
database is not part of this pipeline and is not updated by subsequent moves.

Usage:
    python intake.py [--dry-run] [--overwrite] [--log FILE]
"""

import argparse
import logging
import subprocess
import sys
import tempfile
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
    parser.add_argument("--legacy-move", action="store_true",
                        help="Use legacy 04_move.py --intake staging routing instead of beets")
    parser.add_argument("--pre-tag", action="store_true",
                        help="Run 01_tag.py on incoming folder before beets import")
    parser.add_argument("--no-enrich", action="store_true",
                        help="Skip cover art enrichment")
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

    if args.legacy_move:
        # Legacy pipeline flow: 01_tag -> 02_rename -> 04_move --intake
        tag_args = common_args[:]
        if args.overwrite:
            tag_args.append("--overwrite")
        if args.fix_suspicious:
            tag_args.append("--fix-suspicious")
        rc = run_step("01_tag.py", tag_args)
        if rc != 0:
            log.error("Tagging failed — stopping pipeline")
            return

        if not args.no_enrich:
            rc = run_step("scripts/enrich_covers.py", common_args)
            if rc != 0:
                log.error("Artwork enrichment failed — stopping pipeline")
                return

        rc = run_step("02_rename.py", common_args)
        if rc != 0:
            log.error("Renaming failed — stopping pipeline")
            return

        move_args = ["--intake", "--log", args.log]
        if not dry_run:
            move_args.append("--no-dry-run")
        rc = run_step("04_move.py", move_args)
        if rc != 0:
            log.error("Moving failed — stopping pipeline")
            return
    else:
        # Beets supplies metadata; cratedigger exclusively owns layout.
        if args.pre_tag:
            tag_args = common_args[:]
            if args.overwrite:
                tag_args.append("--overwrite")
            if args.fix_suspicious:
                tag_args.append("--fix-suspicious")
            rc = run_step("01_tag.py", tag_args)
            if rc != 0:
                log.error("Pre-tagging failed — stopping pipeline")
                return

        beets_cfg = Path("config/beets.yaml")
        if not beets_cfg.exists():
            log.error("Beets config not found at %s. Run with --legacy-move or create config/beets.yaml", beets_cfg)
            return

        with tempfile.TemporaryDirectory(prefix="cratedigger-beets-") as tmp:
            transient_db = Path(tmp) / "library.db"
            beet_cmd = [
                sys.executable,
                "-m",
                "beets",
                "--library",
                str(transient_db),
                "-c",
                str(beets_cfg),
                "import",
                "--nomove",
                "--nocopy",
            ]
            if dry_run:
                beet_cmd.append("--pretend")
            beet_cmd.append(str(intake_folder))
            log.info("Running beets metadata import: %s", " ".join(beet_cmd))
            result = subprocess.run(beet_cmd)
            if result.returncode != 0:
                log.error("Beets import failed with code %d", result.returncode)
                return

        pipeline_steps = []
        if not args.no_enrich:
            pipeline_steps.append(("scripts/enrich_covers.py", common_args))
        pipeline_steps.extend([
            ("02_rename.py", common_args),
            ("03_folders.py", common_args),
            (
                "04_move.py",
                [
                    "--to-artist-root",
                    "--folder",
                    incoming_folder_name,
                    "--log",
                    args.log,
                    *(["--no-dry-run"] if not dry_run else []),
                ],
            ),
        ])

        for script, step_args in pipeline_steps:
            rc = run_step(script, step_args)
            if rc != 0:
                log.error("%s failed — stopping pipeline", script)
                return

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info("Intake complete (%s) — processed %d files", mode, len(audio_files))


if __name__ == "__main__":
    main()

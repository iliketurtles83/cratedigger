#!/usr/bin/env python3
"""runscripts.py — Orchestrate pipeline scripts across music collection folders.

Usage:
    python runscripts.py --dry-run --folder rock
    python runscripts.py --no-dry-run --folder blues
    python runscripts.py --no-dry-run --step 06_analyze.py --folder blues
    python runscripts.py --dry-run --all
"""

import argparse
import subprocess
import sys
from pathlib import Path

import config

PIPELINE_SCRIPTS = [
    {
        "name": "01_tag.py",
        "dry_args": ["--fix-track-mismatch"],
        "live_args": ["--fix-track-mismatch"],
    },
    {
        "name": "02_rename.py",
        "dry_args": [],
        "live_args": [],
    },
    {
        "name": "03_folders.py",
        "dry_args": [],
        "live_args": [],
    },
    {
        "name": "04_move.py",
        "dry_args": ["--restructure"],
        "live_args": ["--restructure"],
    },
]


def run_step(script_name: str, args: list[str]) -> int:
    """Run a pipeline script with the given arguments."""
    cmd = [sys.executable, script_name] + args
    print(f"\n>> Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def get_available_folders() -> list[str]:
    """Return all valid top-level genre folders in MUSIC_ROOT."""
    folders = []
    for p in sorted(config.MUSIC_ROOT.iterdir()):
        if p.is_dir() and p.name in config.GENRE_FOLDERS:
            folders.append(p.name)
    return folders


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pipeline steps across collection folders.")
    parser.add_argument("--folders", nargs="+", help="Specific folders to process")
    parser.add_argument("--folder", type=str, help="Single folder to process")
    parser.add_argument("--all", action="store_true", help="Process all available genre folders")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run steps in dry-run mode (default)")
    parser.add_argument("--no-dry-run", action="store_true", help="Run steps live and apply changes")
    parser.add_argument("--step", type=str, help="Run only a specific script (e.g. 02_rename.py, 06_analyze.py)")
    parser.add_argument("--workers", type=int, default=4, help="Workers for 06_analyze.py (default: 4)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run

    if args.folder:
        target_folders = [args.folder]
    elif args.folders:
        target_folders = args.folders
    elif args.all:
        target_folders = get_available_folders()
    else:
        # Default active list of standard genre folders
        target_folders = [
            "soundtrack", "funk", "folk", "reggae", "brazil",
            "chill", "hip-hop", "indie", "classical", "estonian",
            "meditation", "rock", "pop", "electronic", "metal",
        ]

    mode_label = "DRY-RUN" if dry_run else "LIVE"
    print(f"=== Crategigger Pipeline Orchestrator ({mode_label}) ===")
    print(f"Target Folders ({len(target_folders)}): {target_folders}")

    for folder in target_folders:
        folder_path = config.MUSIC_ROOT / folder
        if not folder_path.exists():
            print(f"Skipping non-existent folder: {folder_path}")
            continue

        print(f"\n{'='*60}")
        print(f"  PROCESSING: {folder}")
        print(f"{'='*60}")

        if args.step:
            step_args = ["--folder", folder]
            if args.step == "06_analyze.py":
                step_args.extend(["--workers", str(args.workers)])
            if dry_run:
                step_args.append("--dry-run")
            else:
                if args.step != "06_analyze.py":
                    step_args.append("--no-dry-run")
            run_step(args.step, step_args)
            continue

        for script in PIPELINE_SCRIPTS:
            script_name = script["name"]
            extra = script["dry_args"] if dry_run else script["live_args"]
            cmd_args = ["--folder", folder] + extra
            if dry_run:
                cmd_args.append("--dry-run")
            else:
                cmd_args.append("--no-dry-run")

            rc = run_step(script_name, cmd_args)
            if rc != 0:
                print(f"Warning: {script_name} returned code {rc} on {folder}")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""Audit unmatched library units with isolated, no-write Beets matching.

The command is inventory-only unless ``--online`` is supplied. Online matching
uses a separate temporary Beets catalog for every album or singles folder and
forces ``--nowrite --nomove --nocopy --noincremental``. It never updates the
legacy persistent Beets database or audio tags.

Examples:
    python scripts/audit_beets_matches.py --bucket Q
    python scripts/audit_beets_matches.py --bucket Q --online
    python scripts/audit_beets_matches.py --folder Q/Quantic --online
"""

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.context import is_disc_subfolder
from lib.tags import read_tags

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("audit_beets_matches")

_MATCH_CONFIDENCE = re.compile(r"\bMatch \((\d+(?:\.\d+)?)%\):")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class MatchUnit:
    path: Path
    singleton: bool
    tracks: int
    missing_track_ids: int


def _audio_files(folder: Path) -> list[Path]:
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in config.AUDIO_EXTENSIONS
    )


def _unit_root(path: Path) -> Path:
    parent = path.parent
    return parent.parent if is_disc_subfolder(parent.name) else parent


def collect_unmatched_units(scope: Path) -> tuple[list[MatchUnit], int]:
    """Return release/singles units containing at least one track without an MBID."""
    grouped: dict[Path, list[Path]] = {}
    unreadable = 0
    for path in _audio_files(scope):
        tags = read_tags(path)
        if tags is None:
            unreadable += 1
            continue
        if tags.get("mb_trackid"):
            continue
        grouped.setdefault(_unit_root(path), []).append(path)

    units: list[MatchUnit] = []
    for folder, missing_files in sorted(grouped.items()):
        all_files = _audio_files(folder)
        units.append(
            MatchUnit(
                path=folder,
                singleton=folder.name == config.ARTIST_SINGLES_FOLDER
                or folder.name == config.ROOT_SINGLES_FOLDER,
                tracks=len(all_files),
                missing_track_ids=len(missing_files),
            )
        )
    return units, unreadable


def _source_state(folder: Path) -> dict[Path, tuple[int, int]]:
    return {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in _audio_files(folder)
    }


def _catalog_items(database: Path) -> list[dict[str, object]]:
    from beets.library import Library

    library = Library(str(database), directory=str(config.MUSIC_ROOT))
    try:
        items = []
        for item in library.items():
            path = Path(os.fsdecode(item.path))
            track = str(item.track) if item.track else ""
            if item.tracktotal:
                track = f"{track}/{item.tracktotal}"
            disc = str(item.disc) if item.disc else ""
            if item.disctotal:
                disc = f"{disc}/{item.disctotal}"
            artists = getattr(item, "artists", None)
            items.append({
                "path": str(path),
                "source_size": path.stat().st_size,
                "source_mtime_ns": path.stat().st_mtime_ns,
                "artist": item.artist,
                "artists": " / ".join(artists) if artists else "",
                "title": item.title,
                "albumartist": item.albumartist,
                "album": item.album,
                "year": item.year,
                "track": track,
                "disc": disc,
                "isrc": item.isrc,
                "compilation": "1" if item.comp else "0",
                "mb_trackid": item.mb_trackid,
                "mb_albumid": item.mb_albumid,
                "mb_artistid": item.mb_artistid,
                "mb_albumartistid": item.mb_albumartistid,
                "mb_releasegroupid": item.mb_releasegroupid,
            })
        return items
    finally:
        library._close()


def _beets_command(unit: MatchUnit, database: Path, log_path: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "beets",
        "--library",
        str(database),
        "-c",
        str(REPO_ROOT / "config" / "beets.yaml"),
        "import",
        "--nocopy",
        "--nomove",
        "--nowrite",
        "--quiet",
        "--quiet-fallback",
        "skip",
        "--noincremental",
        "--log",
        str(log_path),
    ]
    if unit.singleton:
        command.append("--singletons")
    command.append(str(unit.path))
    return command


def match_unit(unit: MatchUnit, temp_root: Path, number: int) -> dict[str, object]:
    """Run one isolated no-write match and return structured evidence."""
    database = temp_root / f"unit-{number}.db"
    log_path = temp_root / f"unit-{number}.log"
    before = _source_state(unit.path)
    result = subprocess.run(
        _beets_command(unit, database, log_path),
        capture_output=True,
        text=True,
        check=False,
    )
    after = _source_state(unit.path)
    if before != after:
        raise RuntimeError(f"Beets changed source files despite --nowrite: {unit.path}")

    items = _catalog_items(database) if database.exists() else []
    clean_stdout = _ANSI_ESCAPE.sub("", result.stdout)
    clean_stderr = _ANSI_ESCAPE.sub("", result.stderr)
    confidences = [
        float(value) for value in _MATCH_CONFIDENCE.findall(clean_stdout)
    ]
    status = "matched" if items else "skipped"
    if result.returncode:
        status = "error"

    return {
        "path": str(unit.path.relative_to(config.MUSIC_ROOT)),
        "mode": "singletons" if unit.singleton else "album",
        "tracks": unit.tracks,
        "missing_track_ids": unit.missing_track_ids,
        "status": status,
        "confidence_percent": min(confidences) if confidences else None,
        "matched_tracks": len(items),
        "items": items,
        "beets_output": clean_stdout.strip(),
        "beets_errors": clean_stderr.strip(),
        "returncode": result.returncode,
    }


def _resolve_scope(bucket: str | None, folder: str | None) -> Path:
    root = Path(config.MUSIC_ROOT).resolve()
    if bucket:
        normalized = bucket.upper()
        if normalized not in config.LETTER_BUCKETS and normalized != config.SYMBOL_BUCKET:
            raise ValueError(f"Invalid artist bucket: {bucket}")
        scope = root / normalized
    else:
        scope = (root / str(folder)).resolve()
    if not scope.is_relative_to(root):
        raise ValueError("Folder must be inside MUSIC_ROOT")
    if not scope.is_dir():
        raise ValueError(f"Folder does not exist: {scope}")
    return scope


def _default_report(scope: Path) -> Path:
    label = "-".join(scope.relative_to(config.MUSIC_ROOT).parts).lower() or "root"
    return (
        Path.home()
        / ".cache"
        / "cratedigger"
        / f"beets-match-audit-{label}.json"
    )


def audit(
    scope: Path,
    *,
    online: bool = False,
    report_path: Path | None = None,
) -> dict[str, object]:
    units, unreadable = collect_unmatched_units(scope)
    log.info(
        "Found %d unmatched unit(s), %d unmatched track(s), %d unreadable file(s)",
        len(units),
        sum(unit.missing_track_ids for unit in units),
        unreadable,
    )

    results: list[dict[str, object]]
    if online:
        results = []
        with tempfile.TemporaryDirectory(prefix="cratedigger-beets-audit-") as temp:
            temp_root = Path(temp)
            for number, unit in enumerate(units, start=1):
                log.info("[%d/%d] Matching %s", number, len(units), unit.path)
                results.append(match_unit(unit, temp_root, number))
    else:
        results = [
            {
                **asdict(unit),
                "path": str(unit.path.relative_to(config.MUSIC_ROOT)),
                "mode": "singletons" if unit.singleton else "album",
                "status": "inventory",
            }
            for unit in units
        ]

    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": str(scope.relative_to(config.MUSIC_ROOT)),
        "online": online,
        "summary": {
            "units": len(units),
            "unmatched_tracks": sum(unit.missing_track_ids for unit in units),
            "unreadable_files": unreadable,
            "matched_units": sum(result["status"] == "matched" for result in results),
            "skipped_units": sum(result["status"] == "skipped" for result in results),
            "error_units": sum(result["status"] == "error" for result in results),
            "matched_tracks": sum(
                int(result.get("matched_tracks", 0)) for result in results
            ),
        },
        "units": results,
    }
    destination = report_path or _default_report(scope)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Wrote report: %s", destination)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--bucket", help="One Tier 1 letter bucket, such as Q")
    scope.add_argument(
        "--folder",
        help="Folder relative to MUSIC_ROOT, such as Q/Quantic",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Contact Beets metadata sources using isolated no-write catalogs",
    )
    parser.add_argument("--report", type=Path, help="JSON report destination")
    args = parser.parse_args()

    try:
        selected = _resolve_scope(args.bucket, args.folder)
    except ValueError as exc:
        parser.error(str(exc))
    payload = audit(selected, online=args.online, report_path=args.report)
    if payload["summary"]["error_units"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

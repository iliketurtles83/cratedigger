#!/usr/bin/env python3
"""Apply explicitly approved releases from a Beets match audit report.

Dry-run is the default. Live writes require both ``--fix`` and
``--no-dry-run``. Every applied release ID must be supplied explicitly, and
each source file must still match the size and mtime recorded by the audit.

Example:
    python scripts/apply_beets_matches.py \
        --report ~/.cache/cratedigger/beets-match-audit-q.json \
        --release-id 96eb9326-2a69-42b5-b9df-8b84911dbd51
"""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.tags import read_tags, write_tags

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("apply_beets_matches")

WRITABLE_FIELDS = (
    "title",
    "artist",
    "artists",
    "albumartist",
    "album",
    "year",
    "track",
    "disc",
    "isrc",
    "compilation",
    "mb_trackid",
    "mb_albumid",
    "mb_artistid",
    "mb_albumartistid",
    "mb_releasegroupid",
)
REQUIRED_FIELDS = (
    "title",
    "artist",
    "albumartist",
    "album",
    "mb_trackid",
    "mb_albumid",
    "mb_artistid",
    "mb_albumartistid",
    "mb_releasegroupid",
)


def load_report(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read audit report {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
        raise ValueError(f"Invalid audit report structure: {path}")
    if payload.get("online") is not True:
        raise ValueError("Apply requires an online Beets audit report")
    return payload


def _verified_path(item: dict[str, object]) -> Path:
    root = Path(config.MUSIC_ROOT).resolve()
    path = Path(str(item.get("path", ""))).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Report path is outside MUSIC_ROOT: {path}")
    if not path.is_file():
        raise ValueError(f"Report path no longer exists: {path}")
    if path.suffix.lower() not in config.AUDIO_EXTENSIONS:
        raise ValueError(f"Report path is not supported audio: {path}")
    try:
        expected_size = int(item["source_size"])
        expected_mtime = int(item["source_mtime_ns"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Report lacks source evidence for {path}") from exc
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (expected_size, expected_mtime):
        raise ValueError(f"Source changed since audit: {path}")
    return path


def _proposed_tags(item: dict[str, object], path: Path) -> dict[str, str]:
    missing = [field for field in REQUIRED_FIELDS if not item.get(field)]
    if missing:
        raise ValueError(
            f"Matched item lacks required fields {', '.join(missing)}: "
            f"{item.get('path')}"
        )
    proposed = {
        field: str(item[field])
        for field in WRITABLE_FIELDS
        if item.get(field) not in (None, "")
    }
    if path.suffix.lower() in {".m4a", ".aac"}:
        proposed.pop("isrc", None)
    return proposed


def plan_writes(
    report: dict[str, object],
    release_ids: set[str],
) -> list[dict[str, object]]:
    """Validate an allowlist and return exact per-file tag writes."""
    if not release_ids:
        raise ValueError("At least one --release-id is required")

    plans: list[dict[str, object]] = []
    found: set[str] = set()
    for unit in report["units"]:
        if not isinstance(unit, dict) or unit.get("status") != "matched":
            continue
        items = unit.get("items")
        if not isinstance(items, list):
            raise ValueError(f"Matched unit has no items: {unit.get('path')}")
        unit_ids = {
            str(item.get("mb_albumid"))
            for item in items
            if isinstance(item, dict) and item.get("mb_albumid")
        }
        selected = unit_ids & release_ids
        if not selected:
            continue
        if len(unit_ids) != 1 or len(selected) != 1:
            raise ValueError(f"Unit has ambiguous release IDs: {unit.get('path')}")
        if len(items) != int(unit.get("tracks", 0)):
            raise ValueError(f"Matched unit is incomplete: {unit.get('path')}")

        release_id = next(iter(selected))
        found.add(release_id)
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"Invalid matched item in {unit.get('path')}")
            path = _verified_path(item)
            existing = read_tags(path)
            if existing is None:
                raise ValueError(f"Cannot read current tags: {path}")
            if existing.get("mb_albumid") not in (None, "", release_id):
                raise ValueError(f"Conflicting existing release ID: {path}")
            proposed = _proposed_tags(item, path)
            updates = {
                field: value
                for field, value in proposed.items()
                if str(existing.get(field) or "") != value
            }
            plans.append(
                {
                    "path": path,
                    "release_id": release_id,
                    "before": existing,
                    "updates": updates,
                    "expected": proposed,
                }
            )

    missing_ids = release_ids - found
    if missing_ids:
        raise ValueError(
            "Approved release IDs are absent from matched report units: "
            + ", ".join(sorted(missing_ids))
        )
    return plans


def apply_report(
    report_path: Path,
    release_ids: set[str],
    *,
    dry_run: bool = True,
    result_path: Path | None = None,
) -> dict[str, object]:
    report = load_report(report_path)
    plans = plan_writes(report, release_ids)
    changed = 0
    results = []
    for plan in plans:
        path = plan["path"]
        updates = plan["updates"]
        if updates:
            written = write_tags(path, updates, dry_run=dry_run)
            if written != updates:
                raise RuntimeError(f"Tag writer did not accept all fields for {path}")
            changed += 1
        results.append(
            {
                "path": str(path),
                "release_id": plan["release_id"],
                "before": plan["before"],
                "updates": updates,
            }
        )
        log.info(
            "%s %s (%d field(s))",
            "Would update" if dry_run else "Updated",
            path,
            len(updates),
        )

    if not dry_run:
        for plan in plans:
            current = read_tags(plan["path"])
            if current is None:
                raise RuntimeError(f"Cannot verify written tags: {plan['path']}")
            mismatches = {
                field: (current.get(field), expected)
                for field, expected in plan["expected"].items()
                if str(current.get(field) or "") != expected
            }
            if mismatches:
                raise RuntimeError(
                    f"Post-write verification failed for {plan['path']}: {mismatches}"
                )

    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_report": str(report_path.resolve()),
        "dry_run": dry_run,
        "approved_release_ids": sorted(release_ids),
        "tracks": len(plans),
        "changed_tracks": changed,
        "results": results,
    }
    destination = result_path or report_path.with_name(
        f"{report_path.stem}-apply.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Wrote application record: %s", destination)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--release-id",
        action="append",
        required=True,
        dest="release_ids",
        help="Explicitly approved MusicBrainz release ID; repeat as needed",
    )
    parser.add_argument("--fix", action="store_true", help="Select tag repairs")
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Write tags; requires --fix",
    )
    parser.add_argument("--result", type=Path, help="Application record path")
    args = parser.parse_args()
    if args.no_dry_run and not args.fix:
        parser.error("--no-dry-run requires --fix")
    apply_report(
        args.report,
        set(args.release_ids),
        dry_run=not args.no_dry_run,
        result_path=args.result,
    )


if __name__ == "__main__":
    main()

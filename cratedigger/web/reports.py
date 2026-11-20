"""Safe report discovery, inspection, and reviewed-evidence helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

MAX_PREVIEW_BYTES = 5 * 1024 * 1024
PAGE_SIZE = 50
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
TEXT_TYPES = {".json", ".txt", ".log", ".csv", ".md"}
_OUTPUT_FLAGS = {
    "audit-beets": ("--report",),
    "apply-beets": ("--result",),
    "consolidate": ("--report",),
    "reconcile-genres": ("--report",),
    "repair-bpm": ("--report",),
}


def report_path(root: Path, relative: str) -> Path:
    """Resolve an existing regular report without permitting path escape."""
    candidate = Path(relative)
    if not relative or "\x00" in relative or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Report path must be relative to the reports directory")
    resolved_root = root.resolve()
    path = (resolved_root / candidate).resolve()
    if not path.is_relative_to(resolved_root):
        raise ValueError("Report path is outside the reports directory")
    if not path.is_file():
        raise ValueError("Report file does not exist")
    return path


def _walk_error(error: OSError) -> None:
    raise error


def list_reports(root: Path, *, query: str = "") -> list[dict[str, Any]]:
    root = root.resolve()
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("Reports location is not a directory")
    needle = query.casefold()
    reports = []
    for folder, directories, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
        parent = Path(folder)
        directories[:] = [name for name in directories if not (parent / name).is_symlink()]
        for name in filenames:
            path = parent / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if needle not in relative.casefold():
                continue
            stat = path.stat()
            reports.append({
                "path": relative,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "mtime_ns": stat.st_mtime_ns,
            })
    reports.sort(key=lambda item: (-item["mtime_ns"], item["path"].casefold(), item["path"]))
    return reports


def read_report(path: Path) -> tuple[str, str]:
    """Read bounded UTF-8 text and return it with its content fingerprint."""
    with path.open("rb") as stream:
        data = stream.read(MAX_PREVIEW_BYTES + 1)
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError(f"Report exceeds the {MAX_PREVIEW_BYTES // (1024 * 1024)} MiB preview limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Report is not valid UTF-8; download it to inspect the bytes") from exc
    return text, hashlib.sha256(data).hexdigest()


def report_sections(payload: Any) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    summary: dict[str, Any] = {}
    sections: dict[str, list[dict[str, Any]]] = {}
    if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
        sections["Records"] = payload
    elif isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                sections[key] = value
            else:
                summary[key] = value
    return summary, sections


def _sort_value(value: Any) -> tuple[int, int, int | float | str]:
    if value is None:
        return (1, 0, "")
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return (0, 0, value)
    if isinstance(value, (list, dict)):
        return (0, 1, json.dumps(value, ensure_ascii=False, sort_keys=True).casefold())
    return (0, 1, str(value).casefold())


def report_table(
    sections: dict[str, list[dict[str, Any]]],
    *,
    section: str = "",
    sort: str = "",
    descending: bool = False,
    page: int = 1,
) -> dict[str, Any] | None:
    if not sections:
        if section or sort:
            raise ValueError("This report has no table sections")
        return None
    if not section:
        section = next((name for name, records in sections.items() if records), next(iter(sections)))
    if section not in sections:
        raise ValueError("Unknown report section")
    records = sections[section]
    columns = list(dict.fromkeys(key for record in records for key in record))
    if sort and sort not in columns:
        raise ValueError("Unknown report sort column")
    if sort:
        records = sorted(records, key=lambda record: _sort_value(record.get(sort)), reverse=descending)
    total = len(records)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    selected = records[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    rows = []
    for record in selected:
        cells = []
        for column in columns:
            value = record.get(column)
            nested = isinstance(value, (dict, list))
            cells.append({
                "nested": nested,
                "value": json.dumps(value, ensure_ascii=False, indent=2) if nested else value,
            })
        rows.append(cells)
    return {
        "section": section, "columns": columns, "rows": rows, "total": total,
        "page": page, "pages": pages, "sort": sort, "descending": descending,
    }


def evidence_actions(payload: Any) -> dict[str, Any]:
    """Describe only report types that the existing scripts actually consume."""
    result: dict[str, Any] = {"beets": [], "bpm": False, "bpm_needs_audit": False}
    if not isinstance(payload, dict):
        return result
    units = payload.get("units")
    if payload.get("online") is True and isinstance(units, list):
        releases: dict[str, dict[str, Any]] = {}
        for unit in units:
            if not isinstance(unit, dict) or unit.get("status") != "matched":
                continue
            items = unit.get("items")
            if not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items):
                continue
            values = [item.get("mb_albumid") for item in items]
            if not all(isinstance(value, str) and value.strip() for value in values):
                continue
            release_ids = set(values)
            if len(release_ids) != 1:
                continue
            release_id = next(iter(release_ids))
            if not isinstance(release_id, str) or not release_id.strip() or unit.get("tracks") != len(items):
                continue
            entry = releases.setdefault(release_id, {"release_id": release_id, "units": [], "tracks": 0})
            entry["units"].append(str(unit.get("path", "Matched unit")))
            entry["tracks"] += len(items)
        result["beets"] = list(releases.values())
    changes = payload.get("changes")
    bpm_report = (
        isinstance(changes, list)
        and "target_root" in payload
        and "scanned" in payload
        and all(isinstance(change, dict) and "path" in change and "action" in change for change in changes)
    )
    if bpm_report and changes:
        required = {"source_dev", "source_ino", "source_size", "source_mtime_ns", "old_bpm", "new_bpm", "genre"}
        result["bpm"] = payload.get("dry_run") is True and all(required.issubset(change) for change in changes)
        result["bpm_needs_audit"] = not result["bpm"]
    return result


def report_url(relative: str, **params: object) -> str:
    return "/reports/view?" + urlencode({"path": relative, **params})


def job_report_artifacts(job: dict[str, object], root: Path) -> list[dict[str, str]]:
    argv = job.get("argv")
    params = job.get("params")
    operation_id = str(job.get("operation_id", ""))
    candidates: list[tuple[str, str]] = []
    if isinstance(params, dict):
        for key in ("apply_report", "report"):
            if key == "report" and operation_id not in {"apply-beets", "create-playlist"}:
                continue
            if isinstance(params.get(key), str) and params[key]:
                candidates.append(("Reviewed input", params[key]))
    if isinstance(argv, list):
        for flag in _OUTPUT_FLAGS.get(operation_id, ()):
            for index, value in enumerate(argv[:-1]):
                if value == flag and isinstance(argv[index + 1], str):
                    candidates.append(("Report output", argv[index + 1]))
    result = []
    seen = set()
    resolved_root = root.resolve()
    for label, raw in candidates:
        path = Path(raw).resolve()
        if path in seen or not path.is_relative_to(resolved_root) or not path.is_file():
            continue
        seen.add(path)
        relative = path.relative_to(resolved_root).as_posix()
        result.append({"label": label, "path": relative, "url": report_url(relative)})
    return result

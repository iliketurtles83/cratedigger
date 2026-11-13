"""Migrate and auto-triage review.json items into cratedigger.db."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import config
from cratedigger.db import DEFAULT_DB_PATH, open_db, upsert_review_item, utc_now_iso
from lib.tags import read_tags, write_tags

log = logging.getLogger("cratedigger.review_migration")

PASSIVE_HEALTH_REASONS: frozenset[str] = frozenset(
    {
        "caa_no_cover",
        "single_no_mbid",
        "single_caa_no_cover",
        "no_mbid_found",
        "genre_fallback_applied",
    }
)

_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_CANONICAL_COVER_NAMES: frozenset[str] = frozenset({"cover.jpg", "folder.jpg", "cover.png", "folder.png"})


def classify_queue_type(reason: str) -> str:
    """Return 'health' for passive API notices or 'actionable' for blocking review items."""
    return "health" if reason in PASSIVE_HEALTH_REASONS else "actionable"


def make_review_item_key(item: dict[str, Any]) -> str:
    """Return a stable SHA-256 identity key for a legacy review.json entry."""
    payload = {k: item[k] for k in sorted(item) if k not in {"path", "reason"}}
    raw = json.dumps(
        {"path": item.get("path", ""), "reason": item.get("reason", ""), "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _check_consensus_folder(
    folder: Path,
    item: dict[str, Any],
    *,
    fix_consensus: bool,
    dry_run: bool,
) -> tuple[str, dict[str, Any] | None]:
    """Evaluate mixed_tags / inconsistent_tags folders for >=75% consensus."""
    audio_files = [
        f
        for f in folder.rglob("*")
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    ]
    if not audio_files:
        return "auto_resolved", {"method": "empty_folder"}

    field = item.get("field")
    if not field:
        fields = item.get("fields")
        if isinstance(fields, list) and len(fields) == 1:
            field = fields[0]

    if field not in {"year", "album", "albumartist"}:
        return "pending", None

    values: Counter[str] = Counter()
    per_file: list[tuple[Path, str]] = []
    for af in audio_files:
        tags = read_tags(af) or {}
        val = (tags.get(field) or "").strip()
        if field == "year" and val:
            val = val[:4]
        per_file.append((af, val))
        if val:
            values[val] += 1

    if not values:
        return "pending", None

    if len(values) == 1 and all(v for _, v in per_file):
        only_val = next(iter(values))
        return "auto_resolved", {"method": "already_consistent", "field": field, "value": only_val}

    top_val, count = values.most_common(1)[0]
    coverage = count / len(audio_files)
    if coverage < 0.75:
        return "pending", None

    if fix_consensus and not dry_run:
        updated = 0
        for af, curr in per_file:
            if curr != top_val:
                write_tags(af, {field: top_val}, dry_run=False)
                updated += 1
        return "auto_resolved", {
            "method": "consensus_applied",
            "field": field,
            "value": top_val,
            "coverage": round(coverage, 3),
            "updated_files": updated,
        }

    return "pending", {
        "suggested_consensus": {
            "field": field,
            "value": top_val,
            "coverage": round(coverage, 3),
        }
    }


def evaluate_review_item(
    item: dict[str, Any],
    *,
    fix_consensus: bool = False,
    dry_run: bool = True,
) -> tuple[str, str, dict[str, Any] | None]:
    """Return ``(queue_type, status, resolution)`` for one legacy review item."""
    reason = str(item.get("reason") or "unknown")
    queue_type = classify_queue_type(reason)
    raw_path = str(item.get("path") or "")
    if not raw_path:
        return queue_type, "pruned", {"method": "empty_path"}

    p = Path(raw_path)
    if not p.exists():
        return queue_type, "pruned", {"method": "missing_path"}

    if reason == "no_genre" and p.is_file():
        tags = read_tags(p)
        if tags and tags.get("genre"):
            return queue_type, "auto_resolved", {"method": "already_tagged", "genre": tags["genre"]}

    if reason == "ambiguous_cover_art" and p.is_dir():
        children = list(p.iterdir())
        has_canonical = any(c.is_file() and c.name.lower() in _CANONICAL_COVER_NAMES for c in children)
        image_count = sum(1 for c in children if c.is_file() and c.suffix.lower() in _IMAGE_EXTS)
        if has_canonical or image_count <= 1:
            return queue_type, "auto_resolved", {"method": "cover_already_resolved"}

    if reason in {"mixed_tags", "inconsistent_tags"} and p.is_dir():
        status, resolution = _check_consensus_folder(
            p, item, fix_consensus=fix_consensus, dry_run=dry_run
        )
        return queue_type, status, resolution

    return queue_type, "pending", None


def migrate_review_json(
    review_path: Path = Path("review.json"),
    db_path: Path = DEFAULT_DB_PATH,
    *,
    fix_consensus: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Import *review_path* into *db_path* with automatic pruning and consensus triage.

    Does not modify or delete *review_path*. In dry-run mode (default), evaluates
    every item and returns summary counts without writing to *db_path* or audio tags.
    """
    if not review_path.exists():
        return {"total": 0, "dry_run": dry_run, "counts": {}}

    raw = json.loads(review_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected list in {review_path}")

    summary_counts: dict[str, Counter[str]] = {
        "actionable": Counter(),
        "health": Counter(),
    }
    consensus_suggestions = 0
    now = utc_now_iso()

    path_hashes: dict[str, str] = {}
    if db_path.exists():
        with open_db(db_path) as conn:
            for row in conn.execute("SELECT path, audio_hash FROM track_paths").fetchall():
                path_hashes[row["path"]] = row["audio_hash"]

    prepared: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "unknown")
        raw_path = str(item.get("path") or "")
        payload = {k: item[k] for k in sorted(item) if k not in {"path", "reason"}}
        queue_type, status, resolution = evaluate_review_item(
            item, fix_consensus=fix_consensus, dry_run=dry_run
        )
        summary_counts[queue_type][status] += 1
        if resolution and "suggested_consensus" in resolution:
            consensus_suggestions += 1
        prepared.append(
            {
                "item_key": make_review_item_key(item),
                "queue_type": queue_type,
                "reason": reason,
                "path": raw_path,
                "audio_hash": path_hashes.get(raw_path),
                "payload": payload,
                "status": status,
                "resolution": resolution,
            }
        )

    if not dry_run:
        with open_db(db_path) as conn:
            for entry in prepared:
                upsert_review_item(conn, now=now, **entry)
            conn.commit()

    return {
        "total": len(prepared),
        "dry_run": dry_run,
        "consensus_suggestions": consensus_suggestions,
        "counts": {k: dict(v) for k, v in summary_counts.items()},
    }

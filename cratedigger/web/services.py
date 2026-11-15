"""Service layer for Review Queue, Library Health, Quarantine, and Undo actions."""

from __future__ import annotations

from collections import Counter
import json
import logging
from pathlib import Path
import shutil
import sqlite3
from typing import Any
import uuid

import config
from cratedigger.db import utc_now_iso
from lib.audiohash import audio_hash
from lib.path_updates import update_downstream_paths
from lib.tags import read_tags, write_tags

log = logging.getLogger("cratedigger.web.services")

_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp"})


def resolve_safe_path(raw_path: str | Path, music_root: Path) -> Path:
    """Resolve *raw_path* and ensure it lives inside *music_root*."""
    root = music_root.resolve()
    candidate = Path(raw_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"Path outside music_root is forbidden: {raw_path}")
    return candidate


def quarantine_root(music_root: Path) -> Path:
    """Return the quarantine directory inside *music_root*."""
    return music_root / "0quarantine"


def _record_undo(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    seq: int,
    op_type: str,
    src_path: str | None = None,
    dst_path: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO undo_journal (
            plan_id, run_id, seq, op_type, src_path, dst_path,
            before_json, after_json, reverted, created_at
        ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            run_id,
            seq,
            op_type,
            src_path,
            dst_path,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            utc_now_iso(),
        ),
    )


def _move_to_quarantine(
    conn: sqlite3.Connection,
    file_path: Path,
    music_root: Path,
    *,
    run_id: str,
    seq: int,
) -> Path:
    """Move *file_path* into 0quarantine/<relpath> without overwriting existing files."""
    qroot = quarantine_root(music_root)
    rel = file_path.resolve().relative_to(music_root.resolve())
    dest = qroot / rel
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        idx = 2
        while dest.exists():
            dest = dest.with_name(f"{stem} ({idx}){suf}")
            idx += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file_path), str(dest))
    _record_undo(
        conn,
        run_id=run_id,
        seq=seq,
        op_type="quarantine_move",
        src_path=str(file_path),
        dst_path=str(dest),
    )
    conn.execute("DELETE FROM track_paths WHERE path = ?", (str(file_path),))
    return dest


def get_review_reason_counts(
    conn: sqlite3.Connection,
    *,
    queue_type: str,
    status: str = "pending",
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT reason, COUNT(*) AS cnt
        FROM review_items
        WHERE queue_type = ? AND status = ?
        GROUP BY reason
        ORDER BY cnt DESC, reason ASC
        """,
        (queue_type, status),
    ).fetchall()
    return [{"reason": r["reason"], "count": int(r["cnt"])} for r in rows]


def get_review_status_counts(conn: sqlite3.Connection, *, queue_type: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM review_items WHERE queue_type = ? GROUP BY status",
        (queue_type,),
    ).fetchall()
    return {r["status"]: int(r["cnt"]) for r in rows}


def list_review_items(
    conn: sqlite3.Connection,
    *,
    queue_type: str = "actionable",
    status: str = "pending",
    reason: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return paginated review items plus total count matching filters."""
    where = ["queue_type = ?", "status = ?"]
    params: list[Any] = [queue_type, status]
    if reason:
        where.append("reason = ?")
        params.append(reason)
    if query:
        where.append("path LIKE ?")
        params.append(f"%{query}%")

    where_sql = " AND ".join(where)
    total = int(
        conn.execute(f"SELECT COUNT(*) FROM review_items WHERE {where_sql}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT id, item_key, queue_type, reason, path, audio_hash,
               payload_json, status, resolution_json, created_at, resolved_at
        FROM review_items
        WHERE {where_sql}
        ORDER BY id ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload_json"])
        item["resolution"] = json.loads(item["resolution_json"]) if item["resolution_json"] else None
        items.append(item)
    return items, total


def list_grouped_no_genre(
    conn: sqlite3.Connection,
    *,
    status: str = "pending",
    query: str | None = None,
    limit: int = 40,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Group no_genre review items by parent directory for batch genre assignment."""
    where = ["queue_type = 'actionable'", "status = ?", "reason = 'no_genre'"]
    params: list[Any] = [status]
    if query:
        where.append("path LIKE ?")
        params.append(f"%{query}%")
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"SELECT id, path FROM review_items WHERE {where_sql} ORDER BY path ASC",
        params,
    ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        parent = str(Path(r["path"]).parent)
        grouped.setdefault(parent, []).append({"id": int(r["id"]), "path": r["path"]})

    all_groups = [
        {"folder": folder, "folder_name": Path(folder).name, "count": len(files), "items": files}
        for folder, files in grouped.items()
    ]
    total_groups = len(all_groups)
    return all_groups[offset : offset + limit], total_groups


def enrich_review_item_context(item: dict[str, Any], music_root: Path) -> dict[str, Any]:
    """Attach live disk inspection details for rendering a single review item."""
    reason = item["reason"]
    raw_path = item["path"]
    p = Path(raw_path)
    ctx: dict[str, Any] = {
        "exists": p.exists(),
        "is_dir": p.is_dir(),
        "is_file": p.is_file(),
        "display_name": p.name or raw_path,
    }
    try:
        ctx["rel_path"] = str(p.resolve().relative_to(music_root.resolve()))
    except (OSError, ValueError):
        ctx["rel_path"] = raw_path

    if not p.exists():
        return ctx

    if reason == "ambiguous_cover_art" and p.is_dir():
        candidates = []
        for child in sorted(p.iterdir()):
            if child.is_file() and child.suffix.lower() in _IMAGE_EXTS:
                dims = None
                try:
                    from PIL import Image

                    with Image.open(child) as img:
                        dims = f"{img.width}×{img.height}"
                except Exception:
                    pass
                candidates.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "size_kb": round(child.stat().st_size / 1024, 1),
                        "dims": dims,
                    }
                )
        ctx["image_candidates"] = candidates

    elif reason == "rename_conflict":
        target_raw = item["payload"].get("target")
        target = Path(target_raw) if target_raw else None
        src_hash = None
        dst_hash = None
        try:
            src_hash = audio_hash(str(p)) if p.is_file() else None
        except Exception:
            pass
        if target and target.is_file():
            try:
                dst_hash = audio_hash(str(target))
            except Exception:
                pass
        ctx["source_tags"] = read_tags(p) if p.is_file() else {}
        ctx["source_size_kb"] = round(p.stat().st_size / 1024, 1) if p.is_file() else 0
        ctx["source_hash"] = src_hash
        ctx["target_path"] = str(target) if target else None
        ctx["target_exists"] = bool(target and target.exists())
        ctx["target_tags"] = read_tags(target) if target and target.is_file() else {}
        ctx["target_size_kb"] = round(target.stat().st_size / 1024, 1) if target and target.is_file() else 0
        ctx["target_hash"] = dst_hash
        ctx["same_audio"] = bool(src_hash and dst_hash and src_hash == dst_hash)

    elif reason in {"mixed_tags", "inconsistent_tags", "artist_flat_mixed", "incomplete_metadata", "artist_flat_incomplete"} and p.is_dir():
        field = item["payload"].get("field")
        if not field:
            fields = item["payload"].get("fields") or item["payload"].get("missing")
            if isinstance(fields, list) and fields:
                field = fields[0]
        ctx["inspect_field"] = field or "album"
        value_counts: Counter[str] = Counter()
        track_count = 0
        for child in sorted(p.rglob("*")):
            if child.is_file() and child.suffix.lower() in config.AUDIO_EXTENSIONS:
                track_count += 1
                tags = read_tags(child) or {}
                val = (tags.get(ctx["inspect_field"]) or "").strip()
                value_counts[val or "<empty>"] += 1
        ctx["track_count"] = track_count
        ctx["value_distribution"] = value_counts.most_common()

    elif p.is_file():
        ctx["tags"] = read_tags(p) or {}

    return ctx


def mark_review_status(
    conn: sqlite3.Connection,
    item_ids: list[int],
    *,
    status: str,
    resolution: dict[str, Any] | None = None,
) -> int:
    """Update status on selected review items."""
    if not item_ids:
        return 0
    ts = utc_now_iso()
    res_json = json.dumps(resolution, ensure_ascii=False) if resolution is not None else None
    placeholders = ",".join("?" for _ in item_ids)
    cur = conn.execute(
        f"""
        UPDATE review_items
        SET status = ?, resolution_json = COALESCE(?, resolution_json), resolved_at = ?
        WHERE id IN ({placeholders})
        """,
        (status, res_json, ts, *item_ids),
    )
    conn.commit()
    return cur.rowcount


def pick_cover_image(
    conn: sqlite3.Connection,
    item_id: int,
    selected_path: str,
    music_root: Path,
) -> dict[str, Any]:
    """Promote selected candidate image to canonical cover.jpg and quarantine others."""
    row = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"Review item {item_id} not found")

    folder = resolve_safe_path(row["path"], music_root)
    chosen = resolve_safe_path(selected_path, music_root)
    if not folder.is_dir() or not chosen.is_file() or chosen.parent != folder:
        raise ValueError("Selected cover must be an image inside the review folder")

    run_id = f"ui-cover-{uuid.uuid4().hex[:8]}"
    cover_dest = folder / "cover.jpg"

    other_images = [
        c
        for c in sorted(folder.iterdir())
        if c.is_file() and c.suffix.lower() in _IMAGE_EXTS and c != chosen
    ]
    seq = 1
    for img in other_images:
        _move_to_quarantine(conn, img, music_root, run_id=run_id, seq=seq)
        seq += 1

    if chosen != cover_dest:
        try:
            from PIL import Image

            with Image.open(chosen) as im:
                rgb = im.convert("RGB")
                rgb.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
                rgb.save(cover_dest, format="JPEG", quality=92)
            _move_to_quarantine(conn, chosen, music_root, run_id=run_id, seq=seq)
        except Exception:
            shutil.move(str(chosen), str(cover_dest))
            _record_undo(
                conn,
                run_id=run_id,
                seq=seq,
                op_type="rename",
                src_path=str(chosen),
                dst_path=str(cover_dest),
            )

    mark_review_status(
        conn,
        [item_id],
        status="resolved",
        resolution={"method": "pick_cover", "cover": str(cover_dest), "run_id": run_id},
    )
    return {"cover": str(cover_dest), "quarantined": len(other_images), "run_id": run_id}


def resolve_rename_conflict(
    conn: sqlite3.Connection,
    item_id: int,
    decision: str,
    music_root: Path,
    *,
    features_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve a rename_conflict item: 'quarantine_source' | 'replace_target' | 'keep_both'."""
    row = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"Review item {item_id} not found")
    payload = json.loads(row["payload_json"])
    source = resolve_safe_path(row["path"], music_root)
    target_raw = payload.get("target")
    if not target_raw:
        raise ValueError("Missing target path in rename_conflict payload")
    target = resolve_safe_path(target_raw, music_root)
    run_id = f"ui-conflict-{uuid.uuid4().hex[:8]}"

    if decision == "quarantine_source":
        q_dest = _move_to_quarantine(conn, source, music_root, run_id=run_id, seq=1)
        if target.exists():
            update_downstream_paths({source: target}, music_root=music_root, logger=log, features_path=features_path)
        res = {"method": "quarantine_source", "quarantined": str(q_dest), "run_id": run_id}
    elif decision == "replace_target":
        seq = 1
        if target.exists():
            _move_to_quarantine(conn, target, music_root, run_id=run_id, seq=seq)
            seq += 1
        shutil.move(str(source), str(target))
        _record_undo(conn, run_id=run_id, seq=seq, op_type="rename", src_path=str(source), dst_path=str(target))
        update_downstream_paths({source: target}, music_root=music_root, logger=log, features_path=features_path)
        res = {"method": "replace_target", "target": str(target), "run_id": run_id}
    elif decision == "keep_both":
        digest = audio_hash(str(source)).split(":", 1)[-1][:8]
        alt = target.with_name(f"{target.stem} [alternate-{digest}]{target.suffix}")
        idx = 2
        while alt.exists():
            alt = target.with_name(f"{target.stem} [alternate-{digest}-{idx}]{target.suffix}")
            idx += 1
        shutil.move(str(source), str(alt))
        _record_undo(conn, run_id=run_id, seq=1, op_type="rename", src_path=str(source), dst_path=str(alt))
        update_downstream_paths({source: alt}, music_root=music_root, logger=log, features_path=features_path)
        res = {"method": "keep_both", "target": str(alt), "run_id": run_id}
    else:
        raise ValueError(f"Unsupported conflict decision: {decision}")

    mark_review_status(conn, [item_id], status="resolved", resolution=res)
    return res


def apply_folder_tag_value(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    field: str,
    value: str,
    music_root: Path,
) -> dict[str, Any]:
    """Write a consistent tag *field=value* across all audio files in a folder."""
    row = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KeyError(f"Review item {item_id} not found")
    folder = resolve_safe_path(row["path"], music_root)
    if not folder.is_dir():
        raise ValueError(f"Not a directory: {folder}")

    allowed = {"album", "albumartist", "artist", "year", "genre"}
    if field not in allowed:
        raise ValueError(f"Unsupported folder tag field: {field}")

    updated = 0
    for af in sorted(folder.rglob("*")):
        if af.is_file() and af.suffix.lower() in config.AUDIO_EXTENSIONS:
            write_tags(af, {field: value.strip()}, dry_run=False)
            updated += 1

    res = {"method": "apply_folder_tag", "field": field, "value": value.strip(), "updated_files": updated}
    mark_review_status(conn, [item_id], status="resolved", resolution=res)
    return res


def apply_tags_to_items(
    conn: sqlite3.Connection,
    item_ids: list[int],
    *,
    updates: dict[str, str],
    music_root: Path,
) -> int:
    """Write tag updates to file-level review items and mark them resolved."""
    clean_updates = {k: v.strip() for k, v in updates.items() if v and v.strip()}
    if not clean_updates or not item_ids:
        return 0
    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT id, path FROM review_items WHERE id IN ({placeholders})",
        item_ids,
    ).fetchall()
    resolved_ids: list[int] = []
    for row in rows:
        p = resolve_safe_path(row["path"], music_root)
        if p.is_file():
            write_tags(p, clean_updates, dry_run=False)
            resolved_ids.append(int(row["id"]))
    mark_review_status(
        conn,
        resolved_ids,
        status="resolved",
        resolution={"method": "write_tags", "tags": clean_updates},
    )
    return len(resolved_ids)


def list_duplicate_hash_groups(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return duplicate audio_hash groups tracked in track_paths."""
    total = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT audio_hash FROM track_paths
                GROUP BY audio_hash HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )
    hashes = conn.execute(
        """
        SELECT audio_hash, COUNT(*) AS cnt
        FROM track_paths
        GROUP BY audio_hash
        HAVING cnt > 1
        ORDER BY cnt DESC, audio_hash ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()

    groups: list[dict[str, Any]] = []
    for hrow in hashes:
        ah = hrow["audio_hash"]
        locs = conn.execute(
            "SELECT path, size, is_canonical FROM track_paths WHERE audio_hash = ? ORDER BY is_canonical DESC, path ASC",
            (ah,),
        ).fetchall()
        groups.append(
            {
                "audio_hash": ah,
                "locations": [
                    {
                        "path": loc["path"],
                        "exists": Path(loc["path"]).exists(),
                        "size_kb": round((loc["size"] or 0) / 1024, 1),
                        "is_canonical": bool(loc["is_canonical"]),
                    }
                    for loc in locs
                ],
            }
        )
    return groups, total


def quarantine_duplicate_hash_copy(
    conn: sqlite3.Connection,
    *,
    audio_hash_val: str,
    keep_path: str,
    quarantine_path: str,
    music_root: Path,
    features_path: Path | None = None,
) -> dict[str, Any]:
    """Quarantine one redundant duplicate copy, relink playlists, and preserve highest rating."""
    keep = resolve_safe_path(keep_path, music_root)
    drop = resolve_safe_path(quarantine_path, music_root)
    if keep == drop or not drop.exists():
        raise ValueError("Invalid duplicate paths")

    if keep.is_file() and drop.is_file():
        keep_tags = read_tags(keep) or {}
        drop_tags = read_tags(drop) or {}
        keep_r = float(keep_tags["rating"]) if keep_tags.get("rating") else 0.0
        drop_r = float(drop_tags["rating"]) if drop_tags.get("rating") else 0.0
        if drop_r > keep_r:
            write_tags(keep, {"rating": str(drop_r)}, dry_run=False)

    run_id = f"ui-dedup-{uuid.uuid4().hex[:8]}"
    q_dest = _move_to_quarantine(conn, drop, music_root, run_id=run_id, seq=1)
    update_downstream_paths({drop: keep}, music_root=music_root, logger=log, features_path=features_path)
    conn.execute(
        "UPDATE tracks SET canonical_path = ?, updated_at = ? WHERE audio_hash = ?",
        (str(keep), utc_now_iso(), audio_hash_val),
    )
    conn.execute(
        "UPDATE track_paths SET is_canonical = 1 WHERE path = ? AND audio_hash = ?",
        (str(keep), audio_hash_val),
    )
    conn.commit()
    return {"kept": str(keep), "quarantined": str(q_dest), "run_id": run_id}


def list_quarantine_files(music_root: Path) -> list[dict[str, Any]]:
    """List files currently inside <music_root>/0quarantine/."""
    qroot = quarantine_root(music_root)
    if not qroot.exists():
        return []
    files = []
    for p in sorted(qroot.rglob("*")):
        if p.is_file():
            rel = p.relative_to(qroot)
            files.append(
                {
                    "path": str(p),
                    "rel_path": str(rel),
                    "original_guess": str(music_root / rel),
                    "size_kb": round(p.stat().st_size / 1024, 1),
                    "is_audio": p.suffix.lower() in config.AUDIO_EXTENSIONS,
                }
            )
    return files


def restore_quarantined_file(
    conn: sqlite3.Connection,
    quarantined_path: str,
    music_root: Path,
) -> Path:
    """Restore a file from 0quarantine/ back to its original relative path."""
    qroot = quarantine_root(music_root).resolve()
    src = resolve_safe_path(quarantined_path, music_root)
    if not src.is_relative_to(qroot) or not src.is_file():
        raise ValueError("File is not inside 0quarantine")
    rel = src.relative_to(qroot)
    dest = music_root / rel
    if dest.exists():
        raise FileExistsError(f"Restore destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    run_id = f"ui-restore-{uuid.uuid4().hex[:8]}"
    _record_undo(conn, run_id=run_id, seq=1, op_type="restore", src_path=str(src), dst_path=str(dest))
    conn.commit()
    return dest


def empty_quarantine(music_root: Path) -> int:
    """Permanently remove all files inside 0quarantine/."""
    qroot = quarantine_root(music_root)
    if not qroot.exists():
        return 0
    removed = 0
    for p in sorted(qroot.rglob("*"), reverse=True):
        if p.is_file():
            p.unlink()
            removed += 1
        elif p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass
    return removed


def undo_run(
    conn: sqlite3.Connection,
    run_id: str,
    music_root: Path,
    *,
    features_path: Path | None = None,
) -> int:
    """Revert all operations recorded under *run_id* in reverse order."""
    from cratedigger.engine.executor import undo_plan_run

    entries = conn.execute(
        "SELECT src_path, dst_path FROM undo_journal WHERE run_id = ? AND reverted = 0",
        (run_id,),
    ).fetchall()
    for entry in entries:
        if entry["src_path"]:
            resolve_safe_path(entry["src_path"], music_root)
        if entry["dst_path"]:
            resolve_safe_path(entry["dst_path"], music_root)

    return undo_plan_run(
        conn,
        run_id,
        music_root=music_root,
        features_path=features_path,
    )

"""Transactional plan executor with SQLite undo journal & features.pkl sync."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import sqlite3
from typing import Any
import uuid

from cratedigger.config_schema import Settings, load_settings
from cratedigger.core import read_tags, write_tags
from cratedigger.db import utc_now_iso
from cratedigger.engine.artwork import ensure_single_embedded_cover
from cratedigger.engine.playlists import append_to_playlist, write_m3u8
from cratedigger.engine.transcoder import transcode_to_mp3
from lib.path_updates import update_downstream_paths
from scripts.consolidate_small_releases import _remove_empty_directory
from scripts.enrich_covers import process_image

log = logging.getLogger("cratedigger.engine.executor")


def _record_undo(
    conn: sqlite3.Connection,
    *,
    plan_id: int | None,
    run_id: str,
    seq: int,
    op_type: str,
    src_path: str | None = None,
    dst_path: str | None = None,
    before_obj: Any = None,
    after_obj: Any = None,
) -> None:
    conn.execute(
        """
        INSERT INTO undo_journal (plan_id, run_id, seq, op_type, src_path, dst_path, before_json, after_json, reverted, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            plan_id,
            run_id,
            seq,
            op_type,
            src_path,
            dst_path,
            json.dumps(before_obj, ensure_ascii=False) if before_obj is not None else None,
            json.dumps(after_obj, ensure_ascii=False) if after_obj is not None else None,
            utc_now_iso(),
        ),
    )


def _quarantine_move(
    file_path: Path,
    *,
    music_root: Path,
    quarantine_root: Path,
) -> Path:
    try:
        rel = file_path.resolve().relative_to(music_root.resolve())
    except ValueError:
        rel = Path(file_path.name)
    dest = quarantine_root / rel
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        idx = 2
        while dest.exists():
            dest = dest.with_name(f"{stem} ({idx}){suf}")
            idx += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(file_path), str(dest))
    return dest


def _update_track_db_path(conn: sqlite3.Connection, old_path: Path, new_path: Path, ah: str | None) -> None:
    if not ah:
        row = conn.execute("SELECT audio_hash FROM track_paths WHERE path = ?", (str(old_path),)).fetchone()
        ah = row["audio_hash"] if row else None
    conn.execute("DELETE FROM track_paths WHERE path = ?", (str(old_path),))
    if ah:
        st = new_path.stat()
        conn.execute(
            """
            INSERT INTO track_paths (path, audio_hash, size, mtime_ns, is_canonical, updated_at)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(path) DO UPDATE SET
                audio_hash = excluded.audio_hash,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                updated_at = excluded.updated_at
            """,
            (str(new_path), ah, st.st_size, st.st_mtime_ns, utc_now_iso()),
        )
        conn.execute(
            "UPDATE tracks SET canonical_path = ?, updated_at = ? WHERE audio_hash = ?",
            (str(new_path), utc_now_iso(), ah),
        )


def execute_plan(
    conn: sqlite3.Connection,
    plan_id: int,
    *,
    settings: Settings | None = None,
    features_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute *plan_id* actions transactionally, recording every step in ``undo_journal``."""
    settings = settings or load_settings(conn)
    music_root = settings.music_root_path()
    q_root = settings.quarantine_path()
    if q_root == Path("0quarantine"):
        q_root = music_root / "0quarantine"

    plan_row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan_row is None:
        raise KeyError(f"Plan {plan_id} not found")

    action_rows = conn.execute(
        "SELECT * FROM plan_actions WHERE plan_id = ? AND status = 'pending' ORDER BY id ASC",
        (plan_id,),
    ).fetchall()

    if dry_run:
        return {
            "plan_id": plan_id,
            "dry_run": True,
            "pending_actions": len(action_rows),
            "executed": 0,
        }

    run_id = f"plan-{plan_id}-{uuid.uuid4().hex[:8]}"
    seq = 0
    executed = 0
    failed = 0
    path_remaps: dict[Path, Path] = {}
    resolved_source_locations: dict[Path, Path] = {}

    for row in action_rows:
        act_id = int(row["id"])
        atype = row["action_type"]
        raw_src = Path(row["source_path"]) if row["source_path"] else None
        src = resolved_source_locations.get(raw_src, raw_src) if raw_src else None
        dst = Path(row["target_path"]) if row["target_path"] else None
        ah = row["audio_hash"]
        payload = json.loads(row["payload_json"])

        try:
            if atype == "write_tags" and src and src.is_file():
                updates = payload.get("updates") or {}
                if updates:
                    existing = read_tags(src) or {}
                    before_snapshot = {k: (existing.get(k) or "") for k in updates}
                    write_tags(src, updates, dry_run=False)
                    seq += 1
                    _record_undo(
                        conn,
                        plan_id=plan_id,
                        run_id=run_id,
                        seq=seq,
                        op_type="write_tags",
                        src_path=str(src),
                        before_obj=before_snapshot,
                        after_obj=updates,
                    )

            elif atype == "embed_artwork" and src and src.is_file():
                fc = Path(payload["folder_cover"]) if payload.get("folder_cover") else None
                ensure_single_embedded_cover(
                    src,
                    folder_cover=fc,
                    mb_albumid=payload.get("mb_albumid"),
                    max_dim=int(payload.get("max_dim", 1200)),
                    online=False,
                    dry_run=False,
                )

            elif atype == "copy_cover" and src and dst and src.is_file():
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    jpeg_bytes, _ = process_image(src.read_bytes(), max_size=int(payload.get("max_dim", 1200)))
                    dst.write_bytes(jpeg_bytes)
                    seq += 1
                    _record_undo(
                        conn,
                        plan_id=plan_id,
                        run_id=run_id,
                        seq=seq,
                        op_type="create_file",
                        dst_path=str(dst),
                        before_obj={"existed": False},
                    )

            elif atype == "quarantine_duplicate" and src and dst and src.exists():
                if src.is_file() and dst.is_file():
                    s_tags = read_tags(src) or {}
                    d_tags = read_tags(dst) or {}
                    sr = float(s_tags["rating"]) if s_tags.get("rating") else 0.0
                    dr = float(d_tags["rating"]) if d_tags.get("rating") else 0.0
                    if sr > dr:
                        write_tags(dst, {"rating": str(sr)}, dry_run=False)
                q_dest = _quarantine_move(src, music_root=music_root, quarantine_root=q_root)
                seq += 1
                _record_undo(
                    conn,
                    plan_id=plan_id,
                    run_id=run_id,
                    seq=seq,
                    op_type="quarantine_move",
                    src_path=str(src),
                    dst_path=str(q_dest),
                )
                conn.execute("DELETE FROM track_paths WHERE path = ?", (str(src),))
                path_remaps[src] = dst

            elif atype == "transcode" and src and dst and src.is_file():
                transcode_to_mp3(
                    src,
                    dst,
                    bitrate=str(payload.get("bitrate", settings.transcode_bitrate)),
                    max_cover_dim=int(payload.get("max_cover_dim", settings.cover_max_dim)),
                    dry_run=False,
                )
                if payload.get("clear_album"):
                    write_tags(dst, {"album": "", "compilation": "0"}, dry_run=False)
                q_dest = _quarantine_move(src, music_root=music_root, quarantine_root=q_root)
                seq += 1
                _record_undo(
                    conn,
                    plan_id=plan_id,
                    run_id=run_id,
                    seq=seq,
                    op_type="transcode",
                    src_path=str(src),
                    dst_path=str(dst),
                    before_obj={"quarantined_source": str(q_dest)},
                )
                resolved_source_locations[raw_src] = dst  # type: ignore[index]
                path_remaps[src] = dst
                _update_track_db_path(conn, src, dst, ah)

            elif atype == "move" and src and dst and src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                seq += 1
                _record_undo(
                    conn,
                    plan_id=plan_id,
                    run_id=run_id,
                    seq=seq,
                    op_type="rename",
                    src_path=str(src),
                    dst_path=str(dst),
                )
                resolved_source_locations[raw_src] = dst  # type: ignore[index]
                path_remaps[src] = dst
                _update_track_db_path(conn, src, dst, ah)

            elif atype == "write_playlist" and dst:
                before_text = dst.read_text(encoding="utf-8") if dst.is_file() else None
                entries = [Path(p) for p in payload.get("entries", [])]
                write_m3u8(dst, entries, dry_run=False)
                seq += 1
                _record_undo(
                    conn,
                    plan_id=plan_id,
                    run_id=run_id,
                    seq=seq,
                    op_type="playlist_write",
                    dst_path=str(dst),
                    before_obj={"existed": before_text is not None, "content": before_text},
                )

            elif atype == "update_singles_playlists" and dst:
                singles = payload.get("singles", [])
                run_date = payload.get("run_date") or utc_now_iso()[:10]
                paths_only = [Path(s["path"]) for s in singles if Path(s["path"]).exists() or Path(s["path"]) in path_remaps.values()]
                touched: list[Path] = []
                if payload.get("auto_intake") and paths_only:
                    p_intake = dst / f"{payload.get('intake_prefix', 'Intake_')}{run_date}.m3u8"
                    before_txt = p_intake.read_text(encoding="utf-8") if p_intake.is_file() else None
                    if append_to_playlist(p_intake, paths_only, dry_run=False):
                        touched.append(p_intake)
                        seq += 1
                        _record_undo(
                            conn,
                            plan_id=plan_id,
                            run_id=run_id,
                            seq=seq,
                            op_type="playlist_write",
                            dst_path=str(p_intake),
                            before_obj={"existed": before_txt is not None, "content": before_txt},
                        )
                if payload.get("auto_genre") and paths_only:
                    by_root: dict[str, list[Path]] = {}
                    for s in singles:
                        sp = Path(s["path"])
                        if sp.exists() or sp in path_remaps.values():
                            by_root.setdefault(s.get("root_genre") or "Other", []).append(sp)
                    for rg, grp in sorted(by_root.items()):
                        p_gen = dst / f"Singles - {rg}.m3u8"
                        before_txt = p_gen.read_text(encoding="utf-8") if p_gen.is_file() else None
                        if append_to_playlist(p_gen, grp, dry_run=False):
                            seq += 1
                            _record_undo(
                                conn,
                                plan_id=plan_id,
                                run_id=run_id,
                                seq=seq,
                                op_type="playlist_write",
                                dst_path=str(p_gen),
                                before_obj={"existed": before_txt is not None, "content": before_txt},
                            )
                    p_all = dst / "All_Singles.m3u8"
                    before_all = p_all.read_text(encoding="utf-8") if p_all.is_file() else None
                    if append_to_playlist(p_all, paths_only, dry_run=False):
                        seq += 1
                        _record_undo(
                            conn,
                            plan_id=plan_id,
                            run_id=run_id,
                            seq=seq,
                            op_type="playlist_write",
                            dst_path=str(p_all),
                            before_obj={"existed": before_all is not None, "content": before_all},
                        )

            elif atype == "cleanup_dir" and src and src.is_dir():
                _remove_empty_directory(src, dry_run=False)

            conn.execute("UPDATE plan_actions SET status = 'applied' WHERE id = ?", (act_id,))
            executed += 1
        except Exception as exc:
            log.exception("Action %s (%s) failed: %s", act_id, atype, exc)
            conn.execute("UPDATE plan_actions SET status = 'failed' WHERE id = ?", (act_id,))
            failed += 1

    if path_remaps:
        update_downstream_paths(
            path_remaps,
            music_root=music_root,
            logger=log,
            features_path=features_path,
        )

    plan_status = "applied" if failed == 0 else "partial_failed"
    conn.execute(
        "UPDATE plans SET status = ?, applied_at = ? WHERE id = ?",
        (plan_status, utc_now_iso(), plan_id),
    )
    conn.commit()
    return {
        "plan_id": plan_id,
        "run_id": run_id,
        "status": plan_status,
        "executed": executed,
        "failed": failed,
        "remapped_paths": len(path_remaps),
    }


def undo_plan_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    music_root: Path,
    features_path: Path | None = None,
) -> int:
    """Revert all journal entries under *run_id* in reverse order."""
    entries = conn.execute(
        "SELECT * FROM undo_journal WHERE run_id = ? AND reverted = 0 ORDER BY seq DESC",
        (run_id,),
    ).fetchall()
    reverted = 0
    remaps: dict[Path, Path] = {}

    for entry in entries:
        op = entry["op_type"]
        src = Path(entry["src_path"]) if entry["src_path"] else None
        dst = Path(entry["dst_path"]) if entry["dst_path"] else None
        before = json.loads(entry["before_json"]) if entry["before_json"] else None

        if op in {"quarantine_move", "rename", "restore", "move"} and src and dst:
            if dst.exists() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                remaps[dst] = src
                _update_track_db_path(conn, dst, src, None)

        elif op == "transcode" and src and dst and before:
            q_src = Path(before.get("quarantined_source", ""))
            if q_src.is_file() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(q_src), str(src))
            if dst.is_file():
                dst.unlink()
            remaps[dst] = src
            _update_track_db_path(conn, dst, src, None)

        elif op == "write_tags" and src and src.is_file() and isinstance(before, dict):
            write_tags(src, before, dry_run=False)

        elif op == "create_file" and dst and dst.is_file():
            if not before or not before.get("existed"):
                dst.unlink(missing_ok=True)

        elif op == "playlist_write" and dst:
            if before and before.get("existed") and before.get("content") is not None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(before["content"], encoding="utf-8")
            elif before and not before.get("existed"):
                dst.unlink(missing_ok=True)

        conn.execute("UPDATE undo_journal SET reverted = 1 WHERE id = ?", (entry["id"],))
        reverted += 1

    if remaps:
        update_downstream_paths(
            remaps,
            music_root=music_root,
            logger=log,
            features_path=features_path,
        )
    conn.commit()
    return reverted

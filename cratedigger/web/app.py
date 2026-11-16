"""FastAPI application for Cratedigger Review Queue, Library Health, and Quarantine UI."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from cratedigger.db import DEFAULT_DB_PATH, open_db
from cratedigger.jobs import cancel_job, get_job, list_jobs, read_job_log, reconcile_jobs, start_job
from cratedigger.ops.registry import OPERATIONS, REPOSITORY_ROOT, public_operation
from cratedigger.web import services
from cratedigger.web.security import LocalSecurityMiddleware

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _is_music_root_readonly(music_root: Path) -> bool:
    try:
        if music_root.exists():
            st = os.statvfs(music_root)
            if bool(getattr(os, "ST_RDONLY", 1) & st.f_flag):
                return True
            return not os.access(music_root, os.W_OK)
    except OSError:
        return False
    return False


def _redirect_with_flash(url: str, *, msg: str = "", err: str = "") -> RedirectResponse:
    params = {}
    if msg:
        params["msg"] = msg
    if err:
        params["err"] = err
    sep = "&" if "?" in url else "?"
    target = f"{url}{sep}{urlencode(params)}" if params else url
    return RedirectResponse(url=target, status_code=303)


def _nav_badges(db_path: Path, music_root: Path) -> dict[str, object]:
    with open_db(db_path) as conn:
        act = int(
            conn.execute(
                "SELECT COUNT(*) FROM review_items WHERE queue_type='actionable' AND status='pending'"
            ).fetchone()[0]
        )
        hlth = int(
            conn.execute(
                "SELECT COUNT(*) FROM review_items WHERE queue_type='health' AND status='pending'"
            ).fetchone()[0]
        )
        dups = int(
            conn.execute(
                "SELECT COUNT(*) FROM (SELECT audio_hash FROM track_paths GROUP BY audio_hash HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )
        undoable = int(
            conn.execute("SELECT COUNT(DISTINCT run_id) FROM undo_journal WHERE reverted = 0").fetchone()[0]
        )
    q_count = len(services.list_quarantine_files(music_root))
    return {
        "actionable": act,
        "health": hlth,
        "quarantine": q_count,
        "duplicates": dups,
        "undoable": undoable,
        "read_only": _is_music_root_readonly(music_root),
        "music_root": str(music_root),
    }


def create_app(
    *,
    db_path: Path | None = None,
    music_root: Path | None = None,
    features_path: Path | None = None,
    logs_dir: Path | None = None,
) -> FastAPI:
    """Create configured FastAPI app instance."""
    resolved_db = Path(db_path or DEFAULT_DB_PATH)
    resolved_root = Path(music_root or config.MUSIC_ROOT)
    resolved_features = Path(features_path or getattr(config, "FEATURES_STORE_PATH", Path("features.pkl")))

    app = FastAPI(title="Cratedigger", version="0.2.0")
    app.add_middleware(LocalSecurityMiddleware)
    app.state.db_path = resolved_db
    app.state.music_root = resolved_root
    app.state.features_path = resolved_features
    app.state.repo_root = REPOSITORY_ROOT
    app.state.logs_dir = Path(logs_dir or REPOSITORY_ROOT / "logs" / "jobs")
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/review", status_code=302)

    @app.get("/library", response_class=HTMLResponse)
    def library_page(
        request: Request,
        q: str = Query(default="", max_length=200),
        artist: str = Query(default="", max_length=500),
        album: str = Query(default="", max_length=500),
        page: int = Query(default=1, ge=1),
    ) -> HTMLResponse:
        per_page = 50
        q = q.strip()
        with open_db(app.state.db_path) as conn:
            tracks, total, page = services.list_library_tracks(
                conn, query=q, artist=artist, album=album, page=page, per_page=per_page
            )
        filters = urlencode({key: value for key, value in (("q", q), ("artist", artist), ("album", album)) if value})
        return _TEMPLATES.TemplateResponse(
            request,
            "library.html",
            {
                "active_nav": "library",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "tracks": tracks,
                "total": total,
                "page": page,
                "total_pages": max(1, (total + per_page - 1) // per_page),
                "q": q,
                "artist": artist,
                "album": album,
                "filters": filters,
            },
        )

    @app.get("/ops", response_class=HTMLResponse)
    def operations_page(
        request: Request,
        msg: str = Query(default=""),
        err: str = Query(default=""),
    ) -> HTMLResponse:
        groups: dict[str, list[dict[str, object]]] = {}
        for operation in OPERATIONS.values():
            groups.setdefault(operation.group, []).append(public_operation(operation))
        with open_db(app.state.db_path) as conn:
            reconcile_jobs(conn)
            recent = list_jobs(conn, limit=8)
        return _TEMPLATES.TemplateResponse(
            request,
            "ops.html",
            {
                "active_nav": "ops",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "groups": groups,
                "recent_jobs": recent,
                "msg": msg,
                "err": err,
            },
        )

    @app.post("/ops/{operation_id}/start")
    async def operation_start(operation_id: str, request: Request) -> RedirectResponse:
        try:
            form = await request.form()
            raw_params = {key: value for key, value in form.multi_items() if key not in {"csrf_token", "mode"}}
            mode = str(form.get("mode") or "preview")
            operation = OPERATIONS.get(operation_id)
            if operation is None:
                raise ValueError(f"Unknown operation: {operation_id}")
            if mode == "apply" and operation.risk in {"writes_tags", "moves_files", "writes_library"} and _is_music_root_readonly(app.state.music_root):
                raise ValueError("The music library is mounted read-only")
            if raw_params.get("report"):
                report_path = Path(str(raw_params["report"]))
                if not report_path.is_absolute():
                    report_path = app.state.repo_root / report_path
                report_path = report_path.resolve()
                reports_root = (app.state.repo_root / "reports").resolve()
                if not report_path.is_relative_to(reports_root):
                    raise ValueError("Report paths must stay inside the repository reports directory")
                raw_params["report"] = str(report_path)
            with open_db(app.state.db_path) as conn:
                reconcile_jobs(conn)
                job_id = start_job(
                    conn,
                    db_path=app.state.db_path,
                    operation_id=operation_id,
                    raw_params=raw_params,
                    mode=mode,
                    repo_root=app.state.repo_root,
                    logs_dir=app.state.logs_dir,
                    music_root=app.state.music_root,
                )
            return _redirect_with_flash(f"/jobs/{job_id}", msg=f"{mode.title()} job queued")
        except (ValueError, RuntimeError) as exc:
            return _redirect_with_flash("/ops", err=str(exc))

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(
        request: Request,
        status: str = Query(default=""),
    ) -> HTMLResponse:
        with open_db(app.state.db_path) as conn:
            reconcile_jobs(conn)
            jobs = list_jobs(conn)
        if status:
            jobs = [job for job in jobs if job["status"] == status]
        return _TEMPLATES.TemplateResponse(
            request,
            "jobs.html",
            {
                "active_nav": "jobs",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "jobs": jobs,
                "status": status,
            },
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str) -> HTMLResponse:
        with open_db(app.state.db_path) as conn:
            reconcile_jobs(conn)
            job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        log_text, _ = read_job_log(str(job["log_path"]), allowed_dir=app.state.logs_dir)
        return _TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {
                "active_nav": "jobs",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "job": job,
                "log_text": log_text,
            },
        )

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                cancel_job(conn, job_id)
            return _redirect_with_flash(f"/jobs/{job_id}", msg="Cancellation requested")
        except (KeyError, RuntimeError, ValueError) as exc:
            return _redirect_with_flash(f"/jobs/{job_id}", err=str(exc))

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str) -> StreamingResponse:
        with open_db(app.state.db_path) as conn:
            job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")

        async def event_stream():
            offset = 0
            while True:
                text, offset = read_job_log(
                    str(job["log_path"]), allowed_dir=app.state.logs_dir, offset=offset
                )
                if text:
                    yield f"data: {json.dumps({'log': text})}\n\n"
                with open_db(app.state.db_path) as conn:
                    current = get_job(conn, job_id)
                if current is None or current["status"] in {"succeeded", "failed", "cancelled", "stale"}:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/review", response_class=HTMLResponse)
    def review_page(
        request: Request,
        reason: str | None = Query(default=None),
        status: str = Query(default="pending"),
        q: str = Query(default=""),
        group: str = Query(default=""),
        page: int = Query(default=1, ge=1),
        msg: str = Query(default=""),
        err: str = Query(default=""),
    ) -> HTMLResponse:
        per_page = 30
        offset = (page - 1) * per_page
        with open_db(app.state.db_path) as conn:
            reason_counts = services.get_review_reason_counts(conn, queue_type="actionable", status=status)
            status_counts = services.get_review_status_counts(conn, queue_type="actionable")
            if reason == "no_genre" and group == "folder":
                groups, total = services.list_grouped_no_genre(
                    conn, status=status, query=q.strip() or None, limit=per_page, offset=offset
                )
                items = []
            else:
                groups = []
                items, total = services.list_review_items(
                    conn,
                    queue_type="actionable",
                    status=status,
                    reason=reason or None,
                    query=q.strip() or None,
                    limit=per_page,
                    offset=offset,
                )
                for it in items:
                    it["ctx"] = services.enrich_review_item_context(it, app.state.music_root)

        total_pages = max(1, (total + per_page - 1) // per_page)
        badges = _nav_badges(app.state.db_path, app.state.music_root)
        return _TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {
                "active_nav": "review",
                "badges": badges,
                "items": items,
                "groups": groups,
                "total": total,
                "page": page,
                "total_pages": total_pages,
                "reason": reason or "",
                "status": status,
                "q": q,
                "group": group,
                "reason_counts": reason_counts,
                "status_counts": status_counts,
                "msg": msg,
                "err": err,
            },
        )

    @app.post("/review/bulk")
    def review_bulk(
        action: Annotated[str, Form()],
        item_ids: Annotated[list[int] | None, Form()] = None,
        genre_value: Annotated[str, Form()] = "",
        return_url: Annotated[str, Form()] = "/review",
    ) -> RedirectResponse:
        ids = item_ids or []
        msg = "No items selected"
        try:
            with open_db(app.state.db_path) as conn:
                if action == "dismiss" and ids:
                    n = services.mark_review_status(conn, ids, status="dismissed", resolution={"method": "bulk_dismiss"})
                    msg = f"Dismissed {n} item(s)"
                elif action == "resolve" and ids:
                    n = services.mark_review_status(conn, ids, status="resolved", resolution={"method": "bulk_resolve"})
                    msg = f"Marked {n} item(s) resolved"
                elif action == "apply_genre" and ids and genre_value.strip():
                    n = services.apply_tags_to_items(
                        conn, ids, updates={"genre": genre_value.strip()}, music_root=app.state.music_root
                    )
                    msg = f"Applied genre '{genre_value.strip()}' to {n} file(s)"
                elif action == "reopen" and ids:
                    n = services.mark_review_status(conn, ids, status="pending")
                    msg = f"Reopened {n} item(s)"
            return _redirect_with_flash(return_url, msg=msg)
        except Exception as exc:
            return _redirect_with_flash(return_url, err=str(exc))

    @app.post("/review/group-genre")
    def review_group_genre(
        item_ids_csv: Annotated[str, Form()],
        genre_value: Annotated[str, Form()],
        return_url: Annotated[str, Form()] = "/review?reason=no_genre&group=folder",
    ) -> RedirectResponse:
        ids = [int(tok) for tok in item_ids_csv.split(",") if tok.strip().isdigit()]
        try:
            with open_db(app.state.db_path) as conn:
                n = services.apply_tags_to_items(
                    conn, ids, updates={"genre": genre_value.strip()}, music_root=app.state.music_root
                )
            return _redirect_with_flash(return_url, msg=f"Applied genre to {n} file(s)")
        except Exception as exc:
            return _redirect_with_flash(return_url, err=str(exc))

    @app.post("/review/{item_id}/pick-cover")
    def review_pick_cover(
        item_id: int,
        selected_path: Annotated[str, Form()],
        return_url: Annotated[str, Form()] = "/review?reason=ambiguous_cover_art",
    ) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                res = services.pick_cover_image(conn, item_id, selected_path, app.state.music_root)
            return _redirect_with_flash(
                return_url,
                msg=f"Saved cover.jpg (quarantined {res['quarantined']} alternate image(s))",
            )
        except Exception as exc:
            return _redirect_with_flash(return_url, err=str(exc))

    @app.post("/review/{item_id}/resolve-conflict")
    def review_resolve_conflict(
        item_id: int,
        decision: Annotated[str, Form()],
        return_url: Annotated[str, Form()] = "/review?reason=rename_conflict",
    ) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                res = services.resolve_rename_conflict(
                    conn,
                    item_id,
                    decision,
                    app.state.music_root,
                    features_path=app.state.features_path,
                )
            return _redirect_with_flash(return_url, msg=f"Conflict resolved ({res['method']})")
        except Exception as exc:
            return _redirect_with_flash(return_url, err=str(exc))

    @app.post("/review/{item_id}/apply-folder-tag")
    def review_apply_folder_tag(
        item_id: int,
        field: Annotated[str, Form()],
        value: Annotated[str, Form()],
        return_url: Annotated[str, Form()] = "/review",
    ) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                res = services.apply_folder_tag_value(
                    conn, item_id, field=field, value=value, music_root=app.state.music_root
                )
            return _redirect_with_flash(
                return_url,
                msg=f"Updated {res['field']} on {res['updated_files']} file(s)",
            )
        except Exception as exc:
            return _redirect_with_flash(return_url, err=str(exc))

    @app.post("/review/{item_id}/write-tags")
    def review_write_tags(
        item_id: int,
        artist: Annotated[str, Form()] = "",
        album: Annotated[str, Form()] = "",
        year: Annotated[str, Form()] = "",
        genre: Annotated[str, Form()] = "",
        title: Annotated[str, Form()] = "",
        return_url: Annotated[str, Form()] = "/review",
    ) -> RedirectResponse:
        updates = {
            "artist": artist,
            "album": album,
            "year": year,
            "genre": genre,
            "title": title,
        }
        try:
            with open_db(app.state.db_path) as conn:
                n = services.apply_tags_to_items(
                    conn, [item_id], updates=updates, music_root=app.state.music_root
                )
            msg = f"Updated tags on {n} file" if n else "No tags updated"
            return _redirect_with_flash(return_url, msg=msg)
        except Exception as exc:
            return _redirect_with_flash(return_url, err=str(exc))

    @app.post("/review/{item_id}/status")
    def review_item_status(
        item_id: int,
        status: Annotated[str, Form()],
        return_url: Annotated[str, Form()] = "/review",
    ) -> RedirectResponse:
        with open_db(app.state.db_path) as conn:
            services.mark_review_status(conn, [item_id], status=status, resolution={"method": f"manual_{status}"})
        sep = "&" if "?" in return_url else "?"
        return RedirectResponse(url=f"{return_url}{sep}{urlencode({'msg': f'Marked {status}'})}", status_code=303)

    @app.get("/health", response_class=HTMLResponse)
    def health_page(
        request: Request,
        reason: str | None = Query(default=None),
        status: str = Query(default="pending"),
        q: str = Query(default=""),
        page: int = Query(default=1, ge=1),
        msg: str = Query(default=""),
        err: str = Query(default=""),
    ) -> HTMLResponse:
        per_page = 40
        offset = (page - 1) * per_page
        with open_db(app.state.db_path) as conn:
            reason_counts = services.get_review_reason_counts(conn, queue_type="health", status=status)
            status_counts = services.get_review_status_counts(conn, queue_type="health")
            items, total = services.list_review_items(
                conn,
                queue_type="health",
                status=status,
                reason=reason or None,
                query=q.strip() or None,
                limit=per_page,
                offset=offset,
            )
        total_pages = max(1, (total + per_page - 1) // per_page)
        badges = _nav_badges(app.state.db_path, app.state.music_root)
        return _TEMPLATES.TemplateResponse(
            request,
            "health.html",
            {
                "active_nav": "health",
                "badges": badges,
                "items": items,
                "total": total,
                "page": page,
                "total_pages": total_pages,
                "reason": reason or "",
                "status": status,
                "q": q,
                "reason_counts": reason_counts,
                "status_counts": status_counts,
                "msg": msg,
                "err": err,
            },
        )

    @app.get("/quarantine", response_class=HTMLResponse)
    def quarantine_page(
        request: Request,
        tab: str = Query(default="duplicates"),
        page: int = Query(default=1, ge=1),
        msg: str = Query(default=""),
        err: str = Query(default=""),
    ) -> HTMLResponse:
        per_page = 25
        offset = (page - 1) * per_page
        with open_db(app.state.db_path) as conn:
            dup_groups, total_dups = services.list_duplicate_hash_groups(conn, limit=per_page, offset=offset)
        q_files = services.list_quarantine_files(app.state.music_root)
        total_pages = max(1, (total_dups + per_page - 1) // per_page)
        badges = _nav_badges(app.state.db_path, app.state.music_root)
        return _TEMPLATES.TemplateResponse(
            request,
            "quarantine.html",
            {
                "active_nav": "quarantine",
                "badges": badges,
                "tab": tab,
                "dup_groups": dup_groups,
                "total_dups": total_dups,
                "q_files": q_files,
                "page": page,
                "total_pages": total_pages,
                "msg": msg,
                "err": err,
            },
        )

    @app.post("/quarantine/dedup")
    def quarantine_dedup(
        audio_hash_val: Annotated[str, Form()],
        keep_path: Annotated[str, Form()],
        quarantine_path: Annotated[str, Form()],
    ) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                services.quarantine_duplicate_hash_copy(
                    conn,
                    audio_hash_val=audio_hash_val,
                    keep_path=keep_path,
                    quarantine_path=quarantine_path,
                    music_root=app.state.music_root,
                    features_path=app.state.features_path,
                )
            return _redirect_with_flash("/quarantine?tab=duplicates", msg="Quarantined duplicate copy and relinked playlists")
        except Exception as exc:
            return _redirect_with_flash("/quarantine?tab=duplicates", err=str(exc))

    @app.post("/quarantine/restore")
    def quarantine_restore(quarantined_path: Annotated[str, Form()]) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                dest = services.restore_quarantined_file(conn, quarantined_path, app.state.music_root)
            return _redirect_with_flash("/quarantine?tab=files", msg=f"Restored {dest.name}")
        except Exception as exc:
            return _redirect_with_flash("/quarantine?tab=files", err=str(exc))

    @app.post("/quarantine/empty")
    def quarantine_empty() -> RedirectResponse:
        try:
            removed = services.empty_quarantine(app.state.music_root)
            return _redirect_with_flash("/quarantine?tab=files", msg=f"Permanently removed {removed} quarantined file(s)")
        except Exception as exc:
            return _redirect_with_flash("/quarantine?tab=files", err=str(exc))

    @app.get("/undo", response_class=HTMLResponse)
    def undo_page(
        request: Request,
        msg: str = Query(default=""),
        err: str = Query(default=""),
    ) -> HTMLResponse:
        with open_db(app.state.db_path) as conn:
            rows = conn.execute(
                """
                SELECT run_id, COUNT(*) AS ops, MAX(created_at) AS created_at,
                       MIN(reverted) AS any_pending, MAX(op_type) AS op_type,
                       MIN(src_path) AS sample_src, MIN(dst_path) AS sample_dst
                FROM undo_journal
                GROUP BY run_id
                ORDER BY MAX(id) DESC
                LIMIT 100
                """
            ).fetchall()
            runs = [dict(r) for r in rows]
        badges = _nav_badges(app.state.db_path, app.state.music_root)
        return _TEMPLATES.TemplateResponse(
            request,
            "undo.html",
            {"active_nav": "undo", "badges": badges, "runs": runs, "msg": msg, "err": err},
        )

    @app.post("/undo/{run_id}")
    def undo_execute(run_id: str) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                n = services.undo_run(
                    conn, run_id, app.state.music_root, features_path=app.state.features_path
                )
            return _redirect_with_flash("/undo", msg=f"Reverted {n} operation(s) in {run_id}")
        except Exception as exc:
            return _redirect_with_flash("/undo", err=str(exc))

    @app.get("/media/image")
    def serve_image(path: str = Query(...)) -> FileResponse:
        try:
            safe = services.resolve_safe_path(path, app.state.music_root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not safe.is_file() or safe.suffix.lower() not in services._IMAGE_EXTS:
            raise HTTPException(status_code=404, detail="Image not found")
        return FileResponse(safe)

    @app.get("/media/audio")
    def serve_audio(path: str = Query(...)) -> FileResponse:
        try:
            safe = services.resolve_safe_path(path, app.state.music_root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not safe.is_file() or safe.suffix.lower() not in config.AUDIO_EXTENSIONS:
            raise HTTPException(status_code=404, detail="Audio not found")
        return FileResponse(safe)

    return app

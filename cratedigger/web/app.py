"""FastAPI application for Cratedigger Review Queue, Library Health, and Quarantine UI."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager
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
from cratedigger.analytics import DEFAULT_SNAPSHOT_PATH
from cratedigger.db import DEFAULT_DB_PATH, open_db
from cratedigger.jobs import (
    cancel_job, get_job, library_mutation_guard, list_jobs, read_job_log, reconcile_jobs, start_job,
)
from cratedigger.ops.registry import OPERATIONS, REPOSITORY_ROOT, public_operation
from cratedigger.web import reports, services
from cratedigger.web.export_status import export_job_status
from cratedigger.web.insights import load_insights, snapshot_freshness
from cratedigger.web.security import LocalSecurityMiddleware
from cratedigger.web.similarity import SEARCH_LIMIT, SimilarityCache, SnapshotTrack
from cratedigger.web.sound_profile import load_sound_profile

_WEB_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"))


def _is_music_root_readonly(music_root: Path) -> bool:
    try:
        if not music_root.is_dir():
            return True
        st = os.statvfs(music_root)
        if bool(getattr(os, "ST_RDONLY", 1) & st.f_flag):
            return True
        return not os.access(music_root, os.W_OK)
    except OSError:
        return True


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


def _similarity_track(track: SnapshotTrack, music_root: Path) -> dict[str, object]:
    audio_url = ""
    audio_problem = "No audio path recorded in the snapshot"
    if track.path:
        try:
            path = services.resolve_safe_path(track.path, music_root)
            if not path.is_file():
                audio_problem = "Audio file is unavailable"
            elif path.suffix.lower() not in config.AUDIO_EXTENSIONS:
                audio_problem = "Audio format is not supported"
            else:
                audio_url = "/media/audio?" + urlencode({"path": str(path)})
                audio_problem = ""
        except ValueError:
            audio_problem = "Audio path is outside the configured library"
        except (OSError, RuntimeError) as exc:
            audio_problem = f"Cannot access audio file: {exc}"
    return {
        "audio_hash": track.audio_hash,
        "title": track.display_title,
        "artist": track.artist,
        "album": track.album,
        "genre": track.genre,
        "eligible": track.vector_index is not None,
        "audio_url": audio_url,
        "audio_problem": audio_problem,
    }


def create_app(
    *,
    db_path: Path | None = None,
    music_root: Path | None = None,
    features_path: Path | None = None,
    logs_dir: Path | None = None,
    analytics_path: Path | None = None,
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
    app.state.analytics_path = Path(analytics_path or DEFAULT_SNAPSHOT_PATH)
    app.state.similarity_cache = SimilarityCache()
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    def protected_mutation() -> AbstractContextManager[None]:
        if _is_music_root_readonly(app.state.music_root):
            raise RuntimeError("The music library is unavailable or mounted read-only")
        return library_mutation_guard(app.state.db_path, app.state.repo_root)

    @app.get("/", response_class=HTMLResponse)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/review", status_code=302)

    @app.get("/insights", response_class=HTMLResponse)
    def insights_page(
        request: Request,
        tier: int = Query(default=1, ge=1, le=2),
        top_n: int = Query(default=6, ge=1, le=10),
    ) -> HTMLResponse:
        error = ""
        try:
            data = load_insights(app.state.analytics_path, tier=tier, top_n=top_n)
        except ValueError as exc:
            data = None
            error = str(exc)
        return _TEMPLATES.TemplateResponse(
            request,
            "insights.html",
            {
                "active_nav": "insights",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "insights": data,
                "freshness": snapshot_freshness(
                    data["metadata"], app.state.features_path, app.state.repo_root
                ) if data else None,
                "tier": tier,
                "top_n": top_n,
                "err": error,
            },
            status_code=503 if error else 200,
        )

    @app.get("/insights/similarity", response_class=HTMLResponse)
    def similarity_page(
        request: Request,
        q: str = Query(default="", max_length=200),
        seed: str = Query(default="", max_length=256),
    ) -> HTMLResponse:
        error = ""
        index = None
        selected = None
        seed_error = ""
        matches: list[dict[str, object]] = []
        neighbors: list[dict[str, object]] = []
        total = 0
        q = q.strip()
        try:
            index = app.state.similarity_cache.get(app.state.analytics_path)
            if index and index.metadata["snapshot_version"] >= 3:
                hits, total = index.search(q)
                matches = [_similarity_track(track, app.state.music_root) for track in hits]
                if seed:
                    track = index.by_hash.get(seed)
                    if track is None:
                        seed_error = "This track is not in the snapshot. Rebuild insights after updating the feature store."
                    else:
                        selected = _similarity_track(track, app.state.music_root)
                        neighbors = [
                            {**_similarity_track(neighbor, app.state.music_root), "score": score}
                            for neighbor, score in index.neighbors(track)
                        ]
        except ValueError as exc:
            index = None
            error = str(exc)
        return _TEMPLATES.TemplateResponse(
            request,
            "similarity.html",
            {
                "active_nav": "insights",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "index": index,
                "freshness": snapshot_freshness(
                    index.metadata, app.state.features_path, app.state.repo_root
                ) if index else None,
                "q": q,
                "selected": selected,
                "seed_error": seed_error,
                "matches": matches,
                "total": total,
                "search_limit": SEARCH_LIMIT,
                "neighbors": neighbors,
                "err": error,
            },
            status_code=503 if error else 200,
        )

    @app.get("/insights/profile", response_class=HTMLResponse)
    def sound_profile_page(request: Request) -> HTMLResponse:
        error = ""
        try:
            data = load_sound_profile(app.state.analytics_path)
        except ValueError as exc:
            data = None
            error = str(exc)
        return _TEMPLATES.TemplateResponse(
            request,
            "sound_profile.html",
            {
                "active_nav": "insights",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "profile": data,
                "freshness": snapshot_freshness(
                    data["metadata"], app.state.features_path, app.state.repo_root
                ) if data else None,
                "err": error,
            },
            status_code=503 if error else 200,
        )

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(
        request: Request,
        q: str = Query(default="", max_length=200),
        page: int = Query(default=1, ge=1),
    ) -> HTMLResponse:
        error = ""
        try:
            matches = reports.list_reports(app.state.repo_root / "reports", query=q.strip())
        except (OSError, ValueError) as exc:
            matches = []
            error = str(exc)
        total = len(matches)
        pages = max(1, (total + reports.PAGE_SIZE - 1) // reports.PAGE_SIZE)
        page = min(page, pages)
        return _TEMPLATES.TemplateResponse(
            request,
            "reports.html",
            {
                "active_nav": "reports",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "reports": matches[(page - 1) * reports.PAGE_SIZE:page * reports.PAGE_SIZE],
                "q": q, "total": total, "page": page, "pages": pages,
                "err": error,
            },
            status_code=503 if error else 200,
        )

    @app.get("/reports/view", response_class=HTMLResponse)
    def report_page(
        request: Request,
        path: str = Query(min_length=1, max_length=4096),
        view: str = Query(default="table", pattern="^(table|raw)$"),
        section: str = Query(default="", max_length=500),
        sort: str = Query(default="", max_length=500),
        order: str = Query(default="asc", pattern="^(asc|desc)$"),
        page: int = Query(default=1, ge=1),
        err: str = Query(default=""),
    ) -> HTMLResponse:
        root = app.state.repo_root / "reports"
        data: dict[str, object] = {
            "active_nav": "reports",
            "badges": _nav_badges(app.state.db_path, app.state.music_root),
            "report": None, "err": err,
        }
        status = 200
        try:
            source = reports.report_path(root, path)
            relative = source.relative_to(root.resolve()).as_posix()
            suffix = source.suffix.lower()
            text = ""
            digest = ""
            summary: list[dict[str, object]] = []
            sections: dict[str, list[dict[str, object]]] = {}
            table = None
            actions: dict[str, object] = {"beets": [], "bpm": False}
            json_error = ""
            if suffix in reports.TEXT_TYPES:
                try:
                    text, digest = reports.read_report(source)
                except ValueError as exc:
                    json_error = str(exc)
                else:
                    if suffix == ".json":
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError as exc:
                            json_error = f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
                        else:
                            summary_data, sections = reports.report_sections(payload)
                            summary = [
                                {
                                    "key": key,
                                    "nested": isinstance(value, (dict, list)),
                                    "value": json.dumps(value, ensure_ascii=False, indent=2)
                                    if isinstance(value, (dict, list)) else value,
                                }
                                for key, value in summary_data.items()
                            ]
                            table = reports.report_table(
                                sections, section=section, sort=sort, descending=order == "desc", page=page
                            )
                            actions = reports.evidence_actions(payload)

            def url_for_report(**changes: object) -> str:
                values: dict[str, object] = {
                    "view": view, "section": section, "sort": sort, "order": order, "page": page,
                }
                values.update(changes)
                return reports.report_url(relative, **values)

            data.update({
                "report": {"path": relative, "size": source.stat().st_size, "suffix": suffix},
                "raw_text": text, "digest": digest, "summary": summary, "sections": sections,
                "table": table, "actions": actions, "view": view,
                "url_for_report": url_for_report, "report_error": json_error,
                "image": suffix in reports.IMAGE_TYPES,
                "text_supported": suffix in reports.TEXT_TYPES,
            })
        except (OSError, ValueError) as exc:
            data["err"] = str(exc)
            status = 400
        return _TEMPLATES.TemplateResponse(request, "report.html", data, status_code=status)

    @app.get("/reports/file")
    def report_file(path: str = Query(min_length=1, max_length=4096), inline: bool = False) -> FileResponse:
        try:
            source = reports.report_path(app.state.repo_root / "reports", path)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        media_type = reports.IMAGE_TYPES.get(source.suffix.lower()) if inline else "application/octet-stream"
        if media_type is None:
            raise HTTPException(status_code=400, detail="Only supported raster images can be displayed inline")
        return FileResponse(
            source,
            media_type=media_type,
            filename=None if inline else source.name,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    @app.post("/reports/preview")
    async def report_preview(request: Request) -> RedirectResponse:
        form = await request.form()
        relative = str(form.get("path") or "")
        try:
            source = reports.report_path(app.state.repo_root / "reports", relative)
            if source.suffix.lower() != ".json":
                raise ValueError("Only supported JSON evidence reports can be previewed")
            text, digest = reports.read_report(source)
            expected = str(form.get("digest") or "")
            if digest != expected:
                raise ValueError("The report changed after it was opened; review it again before previewing")
            payload = json.loads(text)
            actions = reports.evidence_actions(payload)
            operation_id = str(form.get("operation") or "")
            if operation_id == "apply-beets":
                release_id = str(form.get("release_id") or "")
                if release_id not in {item["release_id"] for item in actions["beets"]}:
                    raise ValueError("Select a matched release from this report")
                params = {"report": str(source), "release_id": release_id}
            elif operation_id == "repair-bpm" and actions["bpm"]:
                params = {"apply_report": str(source)}
            else:
                raise ValueError("This report does not support the selected operation")
            with open_db(app.state.db_path) as conn:
                reconcile_jobs(conn)
                job_id = start_job(
                    conn,
                    db_path=app.state.db_path, operation_id=operation_id, raw_params=params, mode="preview",
                    repo_root=app.state.repo_root, logs_dir=app.state.logs_dir, music_root=app.state.music_root,
                    features_path=app.state.features_path, analytics_path=app.state.analytics_path,
                    expected_report_digest=expected,
                )
            return _redirect_with_flash(f"/jobs/{job_id}", msg="Reviewed report preview queued")
        except (OSError, ValueError, RuntimeError) as exc:
            return _redirect_with_flash(reports.report_url(relative), err=str(exc))

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
            last_export = conn.execute(
                "SELECT id FROM jobs WHERE operation_id='recommend-export' AND mode='apply' AND status='succeeded' "
                "ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
            export_job = get_job(conn, last_export["id"]) if last_export else None
        return _TEMPLATES.TemplateResponse(
            request,
            "ops.html",
            {
                "active_nav": "ops",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "groups": groups,
                "recent_jobs": recent,
                "export_status": export_job_status(
                    export_job, app.state.logs_dir, app.state.repo_root, app.state.features_path
                ) if export_job else None,
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
                raise ValueError("The music library is unavailable or mounted read-only")
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
                    features_path=app.state.features_path,
                    analytics_path=app.state.analytics_path,
                )
            return _redirect_with_flash(f"/jobs/{job_id}", msg=f"{mode.title()} job queued")
        except (OSError, ValueError, RuntimeError) as exc:
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
    def job_detail(request: Request, job_id: str, msg: str = "", err: str = "") -> HTMLResponse:
        with open_db(app.state.db_path) as conn:
            reconcile_jobs(conn)
            job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        log_text, log_offset = read_job_log(str(job["log_path"]), allowed_dir=app.state.logs_dir)
        return _TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {
                "active_nav": "jobs",
                "badges": _nav_badges(app.state.db_path, app.state.music_root),
                "job": job,
                "operation": OPERATIONS.get(str(job["operation_id"])),
                "log_text": log_text,
                "log_offset": log_offset,
                "artifacts": reports.job_report_artifacts(job, app.state.repo_root / "reports"),
                "export_status": export_job_status(job, app.state.logs_dir, app.state.repo_root, app.state.features_path),
                "msg": msg,
                "err": err,
            },
        )

    @app.get("/jobs/{job_id}/export")
    def job_export(job_id: str) -> FileResponse:
        with open_db(app.state.db_path) as conn:
            job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Export job not found")
        status = export_job_status(job, app.state.logs_dir, app.state.repo_root, app.state.features_path)
        if not status or not status["available"]:
            raise HTTPException(status_code=404, detail="Export output is unavailable")
        path = Path(status["output"])
        return FileResponse(
            path,
            media_type="application/gzip" if path.name.endswith(".gz") else "application/json",
            filename=path.name,
            content_disposition_type="attachment",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                cancel_job(conn, job_id)
            return _redirect_with_flash(f"/jobs/{job_id}", msg="Cancellation requested")
        except (KeyError, RuntimeError, ValueError) as exc:
            return _redirect_with_flash(f"/jobs/{job_id}", err=str(exc))

    def restart_job(job_id: str, mode: str) -> RedirectResponse:
        try:
            with open_db(app.state.db_path) as conn:
                reconcile_jobs(conn)
                job = get_job(conn, job_id)
                if job is None:
                    raise ValueError("Job not found")
                operation = OPERATIONS.get(str(job["operation_id"]))
                if operation is None:
                    raise ValueError("This operation is no longer available")
                if mode == "apply":
                    if job["mode"] != "preview" or job["status"] != "succeeded" or not operation.can_apply:
                        raise ValueError("Only a successful, applicable preview can be applied")
                    if operation.risk in {"writes_tags", "moves_files", "writes_library"} and _is_music_root_readonly(app.state.music_root):
                        raise ValueError("The music library is unavailable or mounted read-only")
                params = job["params"]
                if not isinstance(params, dict):
                    raise ValueError("Job parameters are invalid")
                next_id = start_job(
                    conn,
                    db_path=app.state.db_path,
                    operation_id=operation.id,
                    raw_params=params,
                    mode=mode,
                    repo_root=app.state.repo_root,
                    logs_dir=app.state.logs_dir,
                    music_root=app.state.music_root,
                    expected_preview_id=job_id if mode == "apply" else None,
                    features_path=app.state.features_path,
                    analytics_path=app.state.analytics_path,
                )
            return _redirect_with_flash(f"/jobs/{next_id}", msg=f"{mode.title()} job queued")
        except (OSError, ValueError, RuntimeError) as exc:
            return _redirect_with_flash(f"/jobs/{job_id}", err=str(exc))

    @app.post("/jobs/{job_id}/rerun")
    def job_rerun(job_id: str) -> RedirectResponse:
        return restart_job(job_id, "preview")

    @app.post("/jobs/{job_id}/apply")
    def job_apply_preview(job_id: str) -> RedirectResponse:
        return restart_job(job_id, "apply")

    @app.get("/jobs/{job_id}/events")
    async def job_events(request: Request, job_id: str, offset: int = Query(default=0, ge=0)) -> StreamingResponse:
        with open_db(app.state.db_path) as conn:
            job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        last_event_id = request.headers.get("last-event-id")
        if last_event_id:
            if not last_event_id.isdecimal():
                raise HTTPException(status_code=400, detail="Invalid log offset")
            offset = int(last_event_id)

        async def event_stream():
            current_offset = offset
            while True:
                text, current_offset = read_job_log(
                    str(job["log_path"]), allowed_dir=app.state.logs_dir, offset=current_offset
                )
                if text:
                    yield f"id: {current_offset}\ndata: {json.dumps({'log': text})}\n\n"
                    continue
                with open_db(app.state.db_path) as conn:
                    current = get_job(conn, job_id)
                if current is None or current["status"] in {"succeeded", "failed", "cancelled", "stale", "orphaned"}:
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
                    with protected_mutation():
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
                dest = services.restore_quarantined_file(conn, quarantined_path, app.state.music_root)
            return _redirect_with_flash("/quarantine?tab=files", msg=f"Restored {dest.name}")
        except Exception as exc:
            return _redirect_with_flash("/quarantine?tab=files", err=str(exc))

    @app.post("/quarantine/empty")
    def quarantine_empty() -> RedirectResponse:
        try:
            with protected_mutation():
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
            with protected_mutation(), open_db(app.state.db_path) as conn:
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

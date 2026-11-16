"""Persistent, allowlisted subprocess jobs for long-running library operations."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid

from cratedigger.db import utc_now_iso
from cratedigger.ops.registry import OPERATIONS, build_argv, normalize_params, validate_scope_value

_ACTIVE = ("queued", "running", "cancelling")
_TERMINAL = ("succeeded", "failed", "cancelled", "stale")


def _process_matches(pid: int, job_id: str) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, ValueError):
        return False
    return "cratedigger.jobs_worker" in command and job_id in command


def external_analysis_running(repo_root: Path) -> bool:
    """Detect a legacy feature-extraction process not registered as a UI job."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    expected_cwd = repo_root.resolve()
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if (entry / "cwd").resolve() != expected_cwd:
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        executable = Path(args[0].decode(errors="replace")).name if args and args[0] else ""
        script_arg = args[1].decode(errors="replace") if len(args) > 1 and args[1] else ""
        if executable == "06_analyze.py" or (
            executable.startswith("python") and Path(script_arg).name == "06_analyze.py"
        ):
            return True
    return False


def reconcile_jobs(conn: sqlite3.Connection) -> None:
    """Mark dead workers stale and release their locks after a server restart."""
    rows = conn.execute(
        "SELECT id, pid, status, created_at FROM jobs WHERE status IN ('queued', 'running', 'cancelling')"
    ).fetchall()
    now = utc_now_iso()
    for row in rows:
        pid = row["pid"]
        if pid is None:
            created = datetime.fromisoformat(row["created_at"])
            age = (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
            if age < 15:
                continue
        elif _process_matches(int(pid), row["id"]):
            continue
        conn.execute(
            "UPDATE jobs SET status='stale', error='Worker process is no longer available', finished_at=? WHERE id=?",
            (now, row["id"]),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (row["id"],))
    conn.commit()


def _fingerprint(operation_id: str, params: dict[str, object]) -> str:
    payload = json.dumps({"operation": operation_id, "params": params}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def list_jobs(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT id, operation_id, params_json, mode, status, exit_code, log_path, error, "
        "created_at, started_at, finished_at FROM jobs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["params"] = json.loads(item.pop("params_json"))
        result.append(item)
    return result


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, status, pid, pgid, "
        "exit_code, log_path, error, created_at, started_at, finished_at FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ("params_json", "argv_json", "locks_json"):
        result[key.removesuffix("_json")] = json.loads(result.pop(key))
    return result


def start_job(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    operation_id: str,
    raw_params: dict[str, object],
    mode: str,
    repo_root: Path,
    logs_dir: Path,
    music_root: Path,
) -> str:
    """Validate, persist, lock, and start a registry operation worker."""
    if mode not in {"preview", "apply"}:
        raise ValueError("Mode must be preview or apply")
    try:
        operation = OPERATIONS[operation_id]
    except KeyError as exc:
        raise ValueError(f"Unknown operation: {operation_id}") from exc
    if mode == "apply" and not operation.can_apply:
        raise ValueError("This operation is read-only")
    params = normalize_params(operation, raw_params)
    if (operation_id == "analyze" or operation_id.startswith("recommend-")) and external_analysis_running(repo_root):
        raise RuntimeError(
            "A separate 06_analyze.py process is already running; wait for it before starting another feature-store job"
        )
    if "folder" in params:
        params["folder"] = validate_scope_value(str(params["folder"]), music_root)
    if "library_root" in params:
        override = Path(str(params["library_root"])).resolve()
        if not override.is_relative_to(music_root.resolve()):
            raise ValueError("Library root override must stay inside the configured library root")
        params["library_root"] = str(override)
    argv = build_argv(operation_id, params, mode=mode, repo_root=repo_root)
    fingerprint = _fingerprint(operation_id, params)
    locks = list(operation.locks)
    conn.execute("BEGIN IMMEDIATE")

    if mode == "apply":
        preview = conn.execute(
            "SELECT id, finished_at FROM jobs WHERE operation_id=? AND mode='preview' AND fingerprint=? "
            "AND status='succeeded' AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1",
            (operation_id, fingerprint),
        ).fetchone()
        if preview is None:
            conn.rollback()
            raise ValueError("Run a successful preview with these exact parameters before applying")
        for lock in locks:
            newer = conn.execute(
                "SELECT 1 FROM jobs WHERE mode='apply' AND started_at>? "
                "AND instr(locks_json, ?) > 0 LIMIT 1",
                (preview["finished_at"], f'"{lock}"'),
            ).fetchone()
            if newer:
                conn.rollback()
                raise ValueError("The library changed after this preview; run a fresh preview before applying")

    if locks:
        active = conn.execute(
            "SELECT lock_key FROM job_locks WHERE lock_key IN (" + ",".join("?" for _ in locks) + ") LIMIT 1",
            locks,
        ).fetchone()
        if active:
            conn.rollback()
            raise RuntimeError(f"Resource is busy: {active['lock_key']}")

    job_id = uuid.uuid4().hex
    now = utc_now_iso()
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        conn.rollback()
        raise
    log_path = logs_dir / f"{job_id}.log"
    conn.execute(
        "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, "
        "status, log_path, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
        (
            job_id, operation_id, json.dumps(params, sort_keys=True), mode, fingerprint,
            json.dumps(argv), json.dumps(locks), str(log_path), now,
        ),
    )
    for lock in locks:
        conn.execute("INSERT INTO job_locks (lock_key, job_id) VALUES (?, ?)", (lock, job_id))
    conn.commit()

    command = [
        sys.executable,
        "-m",
        "cratedigger.jobs_worker",
        "--db",
        str(db_path.resolve()),
        "--job-id",
        job_id,
    ]
    try:
        worker = subprocess.Popen(
            command,
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        conn.execute(
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
            (str(exc), utc_now_iso(), job_id),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
        conn.commit()
        raise RuntimeError(f"Could not start operation worker: {exc}") from exc
    conn.execute("UPDATE jobs SET pid=?, pgid=? WHERE id=?", (worker.pid, worker.pid, job_id))
    conn.commit()
    threading.Thread(target=worker.wait, name=f"job-reaper-{job_id}", daemon=True).start()
    return job_id


def cancel_job(conn: sqlite3.Connection, job_id: str) -> None:
    row = conn.execute("SELECT pid, pgid, status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise KeyError("Job not found")
    if row["status"] not in _ACTIVE:
        raise ValueError("Job is not running")
    if row["pgid"] is None:
        conn.execute(
            "UPDATE jobs SET status='cancelled', error='Cancelled before worker startup', finished_at=? WHERE id=?",
            (utc_now_iso(), job_id),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
        conn.commit()
        return
    pid = int(row["pid"])
    pgid = int(row["pgid"])
    try:
        identity_matches = _process_matches(pid, job_id) and os.getpgid(pid) == pgid
    except ProcessLookupError:
        identity_matches = False
    if not identity_matches:
        conn.execute(
            "UPDATE jobs SET status='stale', error='Worker process identity could not be verified', finished_at=? WHERE id=?",
            (utc_now_iso(), job_id),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
        conn.commit()
        raise RuntimeError("Worker process identity could not be verified; refusing to signal it")
    conn.execute("UPDATE jobs SET status='cancelling' WHERE id=?", (job_id,))
    conn.commit()
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        conn.execute(
            "UPDATE jobs SET status='stale', error='Worker process exited before cancellation', finished_at=? WHERE id=?",
            (utc_now_iso(), job_id),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
        conn.commit()


def read_job_log(
    path: str | Path,
    *,
    allowed_dir: Path,
    offset: int = 0,
    limit: int = 65536,
) -> tuple[str, int]:
    """Read an appended log window only from the configured job-log directory."""
    target = Path(path).resolve()
    if not target.is_relative_to(allowed_dir.resolve()):
        raise ValueError("Job log path is outside the configured job-log directory")
    if not target.is_file() or offset < 0:
        return "", 0
    with target.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(limit)
        next_offset = stream.tell()
    return data.decode("utf-8", errors="replace"), next_offset

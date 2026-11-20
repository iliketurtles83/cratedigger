"""Persistent, allowlisted subprocess jobs for long-running library operations."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
from typing import Iterator
import uuid

import config
from cratedigger.analytics import DEFAULT_SNAPSHOT_PATH, source_file_state
from cratedigger.db import open_db, utc_now_iso
from cratedigger.feature_backfill import BackfillKind, reviewed_backfill_plan
from cratedigger.feature_export import export_output_path, output_file_state
from cratedigger.ops.registry import OPERATIONS, build_argv, normalize_params, validate_scope_value
from cratedigger.server_sync import reviewed_sync_plan, sync_scope

_ACTIVE = ("queued", "running", "cancelling", "orphaned")
_INPUT_REPORTS = {"apply-beets": "report", "repair-bpm": "apply_report"}
_BACKFILLS: dict[str, BackfillKind] = {
    "recommend-backfill-hashes": "hashes",
    "recommend-backfill-embeddings": "embeddings",
}


def _process_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    fields = stat.rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        return None
    return fields[19]


def _group_processes(pgid: int, *, exclude_pid: int | None = None) -> list[int]:
    members = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == exclude_pid:
            continue
        try:
            if os.getpgid(pid) != pgid:
                continue
        except ProcessLookupError:
            continue
        try:
            if _process_start(pid) is not None:
                members.append(pid)
        except PermissionError:
            members.append(pid)
    return members


def _group_has_processes(pgid: int, *, exclude_pid: int | None = None) -> bool:
    return bool(_group_processes(pgid, exclude_pid=exclude_pid))


def _conflicting_locks(locks: list[str] | tuple[str, ...]) -> set[str]:
    result: set[str] = set()
    for lock in locks:
        if lock == "music_root:read":
            result.add("music_root:write")
        elif lock == "music_root:write":
            result.update(("music_root:read", "music_root:write"))
        else:
            result.add(lock)
    return result


def _cleanup_mutation_locks(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT DISTINCT owner_token, owner_pid, process_start FROM ui_mutation_locks"
    ).fetchall()
    for row in rows:
        try:
            start = _process_start(int(row["owner_pid"]))
        except PermissionError:
            continue
        if start != row["process_start"]:
            conn.execute("DELETE FROM ui_mutation_locks WHERE owner_token = ?", (row["owner_token"],))


def _busy_resource(conn: sqlite3.Connection, locks: list[str] | tuple[str, ...]) -> str | None:
    conflicts = sorted(_conflicting_locks(locks))
    if not conflicts:
        return None
    placeholders = ",".join("?" for _ in conflicts)
    for table in ("job_locks", "ui_mutation_locks"):
        row = conn.execute(
            f"SELECT lock_key FROM {table} WHERE lock_key IN ({placeholders}) LIMIT 1",
            conflicts,
        ).fetchone()
        if row:
            return str(row["lock_key"])
    return None


def _process_matches(pid: int, job_id: str) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except (OSError, ValueError):
        return False
    return "cratedigger.jobs_worker" in command and job_id in command


def _same_user_commands(proc_root: Path) -> Iterator[list[bytes]]:
    if not proc_root.is_dir():
        raise RuntimeError("Cannot check for active feature-store processes: /proc is unavailable")
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        yield args


def external_analysis_running(repo_root: Path, *, proc_root: Path = Path("/proc")) -> bool:
    """Detect a legacy feature-extraction process not registered as a UI job."""
    script_name = (repo_root / "06_analyze.py").name
    for args in _same_user_commands(proc_root):
        for arg in args:
            if arg and Path(os.fsdecode(arg)).name == script_name:
                return True
    return False


def external_backfill_running(repo_root: Path, *, proc_root: Path = Path("/proc")) -> bool:
    """Detect standalone legacy recommender backfills, not read-only queries."""
    script_name = (repo_root / "07_recommend.py").name
    for args in _same_user_commands(proc_root):
        if (
            any(arg and Path(os.fsdecode(arg)).name == script_name for arg in args)
            and any(arg in {b"--backfill-hashes", b"--backfill-embeddings"} for arg in args)
        ):
            return True
    return False


@contextmanager
def library_mutation_guard(db_path: Path, repo_root: Path) -> Iterator[None]:
    """Serialize direct UI writes with jobs and avoid an external extraction."""
    if external_analysis_running(repo_root):
        raise RuntimeError("Feature extraction is running; wait for it before modifying the library")
    if external_backfill_running(repo_root):
        raise RuntimeError("A standalone feature backfill is running; wait for it before modifying the library")
    start = _process_start(os.getpid())
    if start is None:
        raise RuntimeError("Cannot identify the web process for a library mutation lock")
    owner = uuid.uuid4().hex
    locks = ("music_root:write", "features_store")
    with open_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _cleanup_mutation_locks(conn)
            busy = _busy_resource(conn, locks)
            if busy:
                raise RuntimeError(f"Resource is busy: {busy}")
            conn.executemany(
                "INSERT INTO ui_mutation_locks (lock_key, owner_token, owner_pid, process_start, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(lock, owner, os.getpid(), start, utc_now_iso()) for lock in locks],
            )
            conn.executemany(
                "INSERT INTO ui_resource_changes (lock_key, changed_at) VALUES (?, ?)",
                [(lock, utc_now_iso()) for lock in locks],
            )
            conn.commit()
        except (RuntimeError, sqlite3.Error):
            conn.rollback()
            raise
    try:
        yield
    finally:
        with open_db(db_path) as conn:
            conn.execute("DELETE FROM ui_mutation_locks WHERE owner_token = ?", (owner,))
            conn.commit()


def reconcile_jobs(conn: sqlite3.Connection) -> None:
    """Reconcile dead workers without releasing locks held by surviving children."""
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        "SELECT id, pid, pgid, status, created_at FROM jobs "
        "WHERE status IN ('queued', 'running', 'cancelling', 'orphaned')"
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
        if row["pgid"] is not None and _group_has_processes(int(row["pgid"])):
            conn.execute(
                "UPDATE jobs SET status='orphaned', error=? WHERE id=?",
                ("Worker exited while operation processes are still running; locks remain held", row["id"]),
            )
            continue
        conn.execute(
            "UPDATE jobs SET status='stale', error='Worker process is no longer available', finished_at=? WHERE id=?",
            (now, row["id"]),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (row["id"],))
    conn.commit()


def _file_fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


_LOADED_CONFIG_FINGERPRINT = _file_fingerprint(Path(config.__file__))


def _fingerprint(
    operation_id: str,
    params: dict[str, object],
    *,
    music_root: Path,
    repo_root: Path,
    input_reports: dict[str, bytes],
    analytics_path: Path | None = None,
    feature_state: tuple[int, int, int, int] | None = None,
    export_state: tuple[int, int, int, int] | None = None,
    sync_source_state: tuple[int, int, int, int] | None = None,
    sync_destination_state: tuple[int, int, int, int] | None = None,
) -> str:
    operation = OPERATIONS[operation_id]
    sources = [
        repo_root / operation.script,
        repo_root / "cratedigger" / "ops" / "registry.py",
        Path(config.__file__),
    ]
    if operation_id == "analytics-snapshot":
        sources.extend((
            repo_root / "cratedigger" / "analytics.py",
            repo_root / "lib" / "recommender.py",
            repo_root / "lib" / "descriptors.py",
            repo_root / "lib" / "embeddings.py",
        ))
    if operation_id == "recommend-export":
        sources.extend((
            repo_root / "cratedigger" / "feature_export.py",
            repo_root / "cratedigger" / "analytics.py",
            repo_root / "scripts" / "build_analytics.py",
            repo_root / "lib" / "recommender.py",
            repo_root / "lib" / "descriptors.py",
            repo_root / "lib" / "embeddings.py",
        ))
    if operation_id in _BACKFILLS:
        sources.extend((
            repo_root / "07_recommend.py",
            repo_root / "cratedigger" / "feature_backfill.py",
            repo_root / "cratedigger" / "analytics.py",
            repo_root / "cratedigger" / "job_logs.py",
            repo_root / "scripts" / "build_analytics.py",
            repo_root / "lib" / "recommend_backfill.py",
            repo_root / "lib" / "audiohash.py",
            repo_root / "lib" / "decode.py",
            repo_root / "lib" / "embeddings.py",
        ))
    if operation_id == "server-sync":
        sources.extend((
            repo_root / "cratedigger" / "server_sync.py",
            repo_root / "cratedigger" / "job_logs.py",
            repo_root / "cratedigger" / "analytics.py",
            repo_root / "scripts" / "build_analytics.py",
            repo_root / "scripts" / "server_sync.sh",
        ))
    inputs = {key: hashlib.sha256(data).hexdigest() for key, data in input_reports.items()}
    payload = json.dumps(
        {
            "operation": operation_id,
            "params": params,
            "music_root": str(music_root.resolve()),
            "sources": [_file_fingerprint(source) for source in sources],
            "inputs": inputs,
            "feature_store": feature_state,
            "export_output": export_state if operation_id == "recommend-export" else None,
            "sync_source": sync_source_state if operation_id == "server-sync" else None,
            "sync_destination": sync_destination_state if operation_id == "server-sync" else None,
            "analytics_output": (
                str((analytics_path or DEFAULT_SNAPSHOT_PATH).resolve())
                if operation_id == "analytics-snapshot" else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _prepare_params(
    operation_id: str, params: dict[str, object], *, music_root: Path, repo_root: Path
) -> dict[str, object]:
    operation = OPERATIONS[operation_id]
    root = music_root.resolve()
    needs_library = operation_id not in {"analytics-snapshot", "server-sync"} and not operation_id.startswith("recommend-")
    if not root.is_dir() and needs_library:
        raise ValueError(f"Library root does not exist: {root}")

    fields = {field.name for field in operation.fields}
    root_field = "library_root" if "library_root" in fields else "target_root" if "target_root" in fields else None
    supports_override = root_field is not None and operation_id not in {"convert-lists", "retag-lists", "repair-bpm"}
    if not supports_override and needs_library and root != Path(config.MUSIC_ROOT).resolve():
        raise ValueError(
            "This operation uses config.MUSIC_ROOT and cannot run with a different UI library root"
        )
    if root_field:
        selected = Path(str(params.get(root_field) or root)).resolve()
        if not selected.is_relative_to(root) or not selected.is_dir():
            raise ValueError("Library root override must be an existing directory inside the UI library root")
        if not supports_override and selected != root:
            raise ValueError("This operation cannot use a different library root")
        params[root_field] = str(selected)
        root = selected

    if params.get("folder"):
        folder = validate_scope_value(str(params["folder"]), root)
        candidates = [root / folder]
        if operation_id in {"convert-lists", "retag-lists"}:
            candidates.insert(0, root / "0compilations" / folder)
        if not any(path.resolve().is_relative_to(root) and path.is_dir() for path in candidates):
            raise ValueError("Folder scope must be an existing directory inside the selected library root")
        params["folder"] = folder

    selected_buckets: list[str] = []
    for key in ("bucket", "buckets"):
        value = params.get(key)
        if not value:
            continue
        parts = str(value).strip().upper().split(",")
        if key == "buckets" and parts == ["ALL"]:
            parts = ["#", "A-Z"]
        if key == "bucket" and len(parts) != 1:
            raise ValueError("A bucket must be one letter or #")
        if any(not re.fullmatch(r"(?:[A-Z#]|[A-Z]-[A-Z])", part.strip()) for part in parts):
            raise ValueError("Buckets must contain only letters, #, or letter ranges")
        if key == "bucket" and len(parts[0].strip()) != 1:
            raise ValueError("A bucket must be one letter or #")
        if any(len(part.strip()) == 3 and part.strip()[0] > part.strip()[2] for part in parts):
            raise ValueError("Bucket ranges must be in ascending order")
        for part in parts:
            part = part.strip()
            if len(part) == 3:
                selected_buckets.extend(chr(code) for code in range(ord(part[0]), ord(part[2]) + 1))
            else:
                selected_buckets.append(part)
    if params.get("all_buckets"):
        selected_buckets.extend(["#"] + [chr(code) for code in range(ord("A"), ord("Z") + 1)])
    if any(not (root / bucket).resolve().is_relative_to(root) for bucket in selected_buckets):
        raise ValueError("Bucket folders must stay inside the selected library root")
    if operation_id == "enrich-covers" and sum(
        bool(params.get(key)) for key in ("folder", "bucket", "buckets", "all_buckets")
    ) > 1:
        raise ValueError("Choose only one folder or bucket scope")

    reports_root = (repo_root / "reports").resolve()
    for key in ("report", "apply_report", "result"):
        if not params.get(key):
            continue
        path = Path(str(params[key]))
        if not path.is_absolute():
            path = repo_root / path
        path = path.resolve()
        if not path.is_relative_to(reports_root):
            raise ValueError("Report paths must stay inside the repository reports directory")
        if _INPUT_REPORTS.get(operation_id) == key and not path.is_file():
            raise ValueError(f"Reviewed report does not exist: {path}")
        params[key] = str(path)
    if params.get("report") and params.get("result") and params["report"] == params["result"]:
        raise ValueError("The result report must not overwrite its input report")
    if operation_id in {"analytics-snapshot", "recommend-export", *_BACKFILLS}:
        source = Path(str(params.get("features") or config.FEATURES_STORE_PATH))
        if not source.is_absolute():
            source = repo_root / source
        source = source.resolve()
        if not source.is_file():
            raise ValueError(f"Feature store does not exist: {source}")
        params["features"] = str(source)
    if operation_id == "recommend-export":
        output = export_output_path(str(params.get("output") or ""), repo_root)
        if output == source or (output.exists() and os.path.samefile(source, output)):
            raise ValueError("The player export must not overwrite the feature store")
        params["output"] = str(output)
    if operation_id == "server-sync":
        source, destination, _local_destination = sync_scope(
            str(params["source"]), str(params["dest"]), complete_source=params["complete_source"] is True,
            protected_library_root=root,
        )
        params["source"] = str(source)
        params["dest"] = destination
    return params


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
    expected_preview_id: str | None = None,
    features_path: Path | None = None,
    analytics_path: Path | None = None,
    expected_report_digest: str | None = None,
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
    inputs = dict(raw_params)
    if operation_id in {"analytics-snapshot", "recommend-export", *_BACKFILLS} and not inputs.get("features") and features_path is not None:
        inputs["features"] = str(features_path)
    params = dict(normalize_params(operation, inputs, mode=mode))
    locks = list(operation.locks)
    if {"music_root:write", "features_store"} & set(locks):
        if external_analysis_running(repo_root):
            raise RuntimeError(
                "A separate 06_analyze.py process is already running; wait for it before starting this operation"
            )
        if external_backfill_running(repo_root):
            raise RuntimeError(
                "A separate 07_recommend.py backfill is already running; wait for it before starting this operation"
            )
    if _file_fingerprint(Path(config.__file__)) != _LOADED_CONFIG_FINGERPRINT:
        raise RuntimeError("config.py changed after the UI started; restart the UI before running operations")
    params = _prepare_params(operation_id, params, music_root=music_root, repo_root=repo_root)
    feature_state = (
        source_file_state(Path(str(params["features"])))
        if operation_id in {"analytics-snapshot", "recommend-export", *_BACKFILLS} else None
    )
    export_state = output_file_state(Path(str(params["output"]))) if operation_id == "recommend-export" else None
    sync_source_state = source_file_state(Path(str(params["source"]))) if operation_id == "server-sync" else None
    sync_destination_state = (
        source_file_state(Path(str(params["dest"])))
        if operation_id == "server-sync" and Path(str(params["dest"])).is_absolute() else None
    )
    report_key = _INPUT_REPORTS.get(operation_id)
    input_reports = {report_key: Path(str(params[report_key])).read_bytes()} if report_key and params.get(report_key) else {}
    if expected_report_digest is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_report_digest) or len(input_reports) != 1:
            raise ValueError("Invalid reviewed report fingerprint")
        if hashlib.sha256(next(iter(input_reports.values()))).hexdigest() != expected_report_digest:
            raise ValueError("The report changed after it was opened; review it again before previewing")
    fingerprint = _fingerprint(
        operation_id, params, music_root=music_root, repo_root=repo_root, input_reports=input_reports,
        analytics_path=analytics_path, feature_state=feature_state, export_state=export_state,
        sync_source_state=sync_source_state, sync_destination_state=sync_destination_state,
    )
    job_id = uuid.uuid4().hex
    now = utc_now_iso()
    command_params = dict(params)
    report_dirs = {
        "audit-beets": "beets",
        "consolidate": "consolidation",
        "reconcile-genres": "genres",
        "repair-bpm": "bpm",
    }
    if operation_id in report_dirs and not command_params.get("report") and not command_params.get("apply_report"):
        command_params["report"] = str(repo_root / "reports" / report_dirs[operation_id] / f"ui-{job_id}.json")
    if operation_id == "apply-beets" and not command_params.get("result"):
        command_params["result"] = str(repo_root / "reports" / "beets" / f"ui-{job_id}-result.json")
    snapshots = {
        key: (logs_dir / f"{job_id}-{key}.json").resolve()
        for key in input_reports
    }
    command_params.update({key: str(path) for key, path in snapshots.items()})
    argv = build_argv(operation_id, command_params, mode=mode, repo_root=repo_root)
    if operation_id == "repair-bpm" and command_params.get("apply_report"):
        argv.append("--verify-report-evidence")
    if operation_id == "analytics-snapshot":
        argv.extend((
            "--output", str((analytics_path or DEFAULT_SNAPSHOT_PATH).resolve()),
            "--expected-source-state", json.dumps(feature_state),
        ))
    if operation_id == "recommend-export":
        argv.extend((
            "--expected-source-state", json.dumps(feature_state),
            "--expected-output-state", json.dumps(export_state),
        ))
    if operation_id in _BACKFILLS:
        argv.extend(("--expected-source-state", json.dumps(feature_state)))
    if operation_id == "server-sync":
        argv.extend((
            "--expected-source-state", json.dumps(sync_source_state),
            "--expected-destination-state", json.dumps(sync_destination_state),
            "--protected-library-root", str(music_root.resolve()),
        ))

    created_snapshots: list[Path] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        _cleanup_mutation_locks(conn)
        busy = _busy_resource(conn, locks)
        if busy:
            raise RuntimeError(f"Resource is busy: {busy}")
        if mode == "apply":
            query = (
                "SELECT id, finished_at, log_path FROM jobs WHERE operation_id=? AND mode='preview' AND fingerprint=? "
                "AND status='succeeded' AND finished_at IS NOT NULL"
            )
            preview_args: list[str] = [operation_id, fingerprint]
            if expected_preview_id is not None:
                query += " AND id=?"
                preview_args.append(expected_preview_id)
            preview = conn.execute(query + " ORDER BY finished_at DESC LIMIT 1", preview_args).fetchone()
            if preview is None:
                raise ValueError("Run a successful preview with these exact parameters before applying")
            for lock in _conflicting_locks(locks):
                newer_job = conn.execute(
                    "SELECT 1 FROM jobs WHERE mode='apply' AND started_at>? "
                    "AND instr(locks_json, ?) > 0 LIMIT 1",
                    (preview["finished_at"], f'"{lock}"'),
                ).fetchone()
                newer_direct_write = conn.execute(
                    "SELECT 1 FROM ui_resource_changes WHERE lock_key=? AND changed_at>? LIMIT 1",
                    (lock, preview["finished_at"]),
                ).fetchone()
                if newer_job or newer_direct_write:
                    raise ValueError("The library changed after this preview; run a fresh preview before applying")
            if operation_id == "server-sync":
                plan = reviewed_sync_plan(
                    Path(preview["log_path"]), logs_dir, Path(str(params["source"])), str(params["dest"])
                )
                argv.extend(("--expected-plan", plan))
            if operation_id in _BACKFILLS:
                max_embeddings = params.get("max_embeddings")
                if max_embeddings is not None and type(max_embeddings) is not int:
                    raise ValueError("Maximum embeddings must be an integer")
                plan = reviewed_backfill_plan(
                    Path(preview["log_path"]), logs_dir, Path(str(params["features"])),
                    _BACKFILLS[operation_id], max_embeddings,
                )
                argv.extend(("--expected-plan", plan))
        logs_dir.mkdir(parents=True, exist_ok=True)
        for key, path in snapshots.items():
            with path.open("xb") as stream:
                created_snapshots.append(path)
                stream.write(input_reports[key])
        for key in ("report", "result"):
            if command_params.get(key) and _INPUT_REPORTS.get(operation_id) != key:
                Path(str(command_params[key])).parent.mkdir(parents=True, exist_ok=True)
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
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        conn.rollback()
        for path in created_snapshots:
            path.unlink(missing_ok=True)
        raise

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
            "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=? AND status='queued'",
            (str(exc), utc_now_iso(), job_id),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
        conn.commit()
        raise RuntimeError(f"Could not start operation worker: {exc}") from exc
    conn.execute(
        "UPDATE jobs SET pid=COALESCE(pid, ?), pgid=COALESCE(pgid, ?) "
        "WHERE id=? AND status IN ('queued', 'running', 'cancelling')",
        (worker.pid, worker.pid, job_id),
    )
    conn.commit()
    threading.Thread(target=worker.wait, name=f"job-reaper-{job_id}", daemon=True).start()
    return job_id


def cancel_job(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT pid, pgid, status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["status"] not in _ACTIVE:
        conn.rollback()
        if row is None:
            raise KeyError("Job not found")
        raise ValueError("Job is not running")
    if row["status"] == "queued":
        conn.execute(
            "UPDATE jobs SET status='cancelled', error='Cancelled before worker startup', finished_at=? "
            "WHERE id=? AND status='queued'",
            (utc_now_iso(), job_id),
        )
        conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
        conn.commit()
        return
    if row["pid"] is None or row["pgid"] is None:
        conn.rollback()
        raise RuntimeError("Worker process identity is missing; refusing to release its locks")
    pid, pgid = int(row["pid"]), int(row["pgid"])
    try:
        identity_matches = _process_matches(pid, job_id) and os.getpgid(pid) == pgid
    except (ProcessLookupError, PermissionError):
        identity_matches = False
    if not identity_matches:
        if _group_has_processes(pgid):
            conn.execute(
                "UPDATE jobs SET status='orphaned', error=? WHERE id=?",
                ("Worker identity could not be verified; operation processes may still be running", job_id),
            )
        else:
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
        reconcile_jobs(conn)
    except OSError as exc:
        raise RuntimeError(f"Could not signal the operation process group: {exc}") from exc


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

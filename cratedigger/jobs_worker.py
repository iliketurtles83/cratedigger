"""Worker entry point used by the local web UI job manager."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from cratedigger.db import open_db, utc_now_iso
from cratedigger.jobs import _group_processes
from cratedigger.ops.registry import REPOSITORY_ROOT

log = logging.getLogger("cratedigger.jobs_worker")
_cancel_requested = threading.Event()


def _request_cancel(_signum: int, _frame: object) -> None:
    _cancel_requested.set()


def _signal_children(pgid: int, sig: int) -> None:
    for pid in _group_processes(pgid, exclude_pid=os.getpid()):
        try:
            if os.getpgid(pid) == pgid:
                os.kill(pid, sig)
        except ProcessLookupError:
            continue


def run_job(db_path: Path, job_id: str) -> int:
    _cancel_requested.clear()
    signal.signal(signal.SIGTERM, _request_cancel)
    signal.signal(signal.SIGINT, _request_cancel)
    with open_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT argv_json, log_path, status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            conn.rollback()
            log.error("Unknown job id %s", job_id)
            return 2
        if row["status"] != "queued":
            conn.rollback()
            if row["status"] == "cancelled":
                return 0
            log.error("Job %s is unexpectedly in state %s", job_id, row["status"])
            return 2
        argv = json.loads(row["argv_json"])
        log_path = Path(row["log_path"])
        if os.getpgrp() != os.getpid():
            conn.execute(
                "UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                ("Worker was not started in its own process group", utc_now_iso(), job_id),
            )
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
            conn.commit()
            return 2
        conn.execute(
            "UPDATE jobs SET status='running', started_at=?, pid=?, pgid=? WHERE id=? AND status='queued'",
            (utc_now_iso(), os.getpid(), os.getpgrp(), job_id),
        )
        conn.commit()

    exit_code = 1
    error: str | None = None
    orphaned = False
    process: subprocess.Popen[bytes] | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab", buffering=0) as output:
            output.write(("$ " + " ".join(argv) + "\n").encode("utf-8", errors="replace"))
            if not _cancel_requested.is_set():
                process = subprocess.Popen(
                    argv,
                    cwd=REPOSITORY_ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                terminate_at: float | None = None
                kill_at: float | None = None
                while True:
                    result = process.poll()
                    if result is not None:
                        exit_code = result
                        if not _group_processes(os.getpgrp(), exclude_pid=os.getpid()):
                            break
                    now = time.monotonic()
                    if _cancel_requested.is_set() and terminate_at is None:
                        _signal_children(os.getpgrp(), signal.SIGTERM)
                        terminate_at = now
                    elif terminate_at is not None and kill_at is None and now - terminate_at >= 5:
                        _signal_children(os.getpgrp(), signal.SIGKILL)
                        kill_at = now
                    elif kill_at is not None and now - kill_at >= 5:
                        orphaned = True
                        error = "Operation processes survived cancellation; resource locks remain held"
                        break
                    time.sleep(0.25)
    except OSError as exc:
        error = f"Could not run operation: {exc}"
        exit_code = 127
        if process is not None:
            orphaned = True

    finished_at = utc_now_iso()
    status = "orphaned" if orphaned else "cancelled" if _cancel_requested.is_set() else "succeeded" if exit_code == 0 else "failed"
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, error=?, finished_at=? WHERE id=?",
            (status, exit_code, error, None if orphaned else finished_at, job_id),
        )
        if not orphaned:
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (job_id,))
        conn.commit()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    return run_job(args.db, args.job_id)


if __name__ == "__main__":
    sys.exit(main())

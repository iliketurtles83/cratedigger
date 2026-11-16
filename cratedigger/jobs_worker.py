"""Worker entry point used by the local web UI job manager."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import signal
import subprocess
import sys
import threading

from cratedigger.db import open_db, utc_now_iso
from cratedigger.ops.registry import REPOSITORY_ROOT

log = logging.getLogger("cratedigger.jobs_worker")
_cancel_requested = threading.Event()


def _request_cancel(_signum: int, _frame: object) -> None:
    _cancel_requested.set()


def run_job(db_path: Path, job_id: str) -> int:
    signal.signal(signal.SIGTERM, _request_cancel)
    signal.signal(signal.SIGINT, _request_cancel)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT argv_json, log_path, status FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            log.error("Unknown job id %s", job_id)
            return 2
        if row["status"] not in {"queued", "cancelling"}:
            log.error("Job %s is unexpectedly in state %s", job_id, row["status"])
            return 2
        argv = json.loads(row["argv_json"])
        log_path = Path(row["log_path"])
        conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
            (utc_now_iso(), job_id),
        )
        conn.commit()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = 1
    error: str | None = None
    try:
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
                while True:
                    try:
                        exit_code = process.wait(timeout=0.25)
                        break
                    except subprocess.TimeoutExpired:
                        if _cancel_requested.is_set():
                            try:
                                process.terminate()
                            except ProcessLookupError:
                                pass
                            try:
                                exit_code = process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                exit_code = process.wait()
                            break
    except OSError as exc:
        error = f"Could not run operation: {exc}"
        exit_code = 127

    finished_at = utc_now_iso()
    status = "cancelled" if _cancel_requested.is_set() else "succeeded" if exit_code == 0 else "failed"
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, error=?, finished_at=? WHERE id=?",
            (status, exit_code, error, finished_at, job_id),
        )
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

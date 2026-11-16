"""Tests for the allowlisted operation registry and persistent job lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cratedigger.db import open_db
from cratedigger.jobs import cancel_job, read_job_log, start_job
from cratedigger.ops.registry import OPERATIONS, REPOSITORY_ROOT, build_argv, normalize_params, validate_scope_value


class FakeWorker:
    pid = 12345

    def wait(self) -> int:
        return 0


class OperationRegistryTests(unittest.TestCase):
    def test_preview_and_apply_argv_are_shell_free_and_differ_only_by_mode(self) -> None:
        preview = build_argv("tag", {"folder": "A", "no_mb": True}, mode="preview")
        apply = build_argv("tag", {"folder": "A", "no_mb": True}, mode="apply")
        self.assertEqual(preview[0], sys.executable)
        self.assertEqual(Path(preview[1]), REPOSITORY_ROOT / "01_tag.py")
        self.assertEqual(preview[2:], ["--folder", "A", "--no-mb", "--dry-run"])
        self.assertEqual(apply[2:], ["--folder", "A", "--no-mb", "--no-dry-run"])
        with self.assertRaises(ValueError):
            build_argv("tag; touch /tmp/pwned", {}, mode="preview")
        with self.assertRaises(ValueError):
            normalize_params(OPERATIONS["tag"], {"folder": "A", "arbitrary": "--no-dry-run"})

    def test_route_requires_one_explicit_mode_and_read_only_ops_cannot_apply(self) -> None:
        with self.assertRaises(ValueError):
            build_argv("route", {"folder": "incoming"}, mode="preview")
        route = build_argv("route", {"folder": "incoming", "route_mode": "intake"}, mode="preview")
        self.assertEqual(route[2:], ["--folder", "incoming", "--intake", "--dry-run"])
        with self.assertRaises(ValueError):
            build_argv("audit-beets", {}, mode="apply")

    def test_folder_scope_cannot_escape_or_be_absolute(self) -> None:
        root = Path("/tmp/music")
        self.assertEqual(validate_scope_value("A/Artist", root), "A/Artist")
        for value in ("../outside", "/tmp/music/A", "A/../../outside", "A\x00/B"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_scope_value(value, root)

    def test_registered_options_match_script_help(self) -> None:
        for operation in OPERATIONS.values():
            with self.subTest(operation=operation.id):
                result = subprocess.run(
                    [sys.executable, str(REPOSITORY_ROOT / operation.script), "--help"],
                    cwd=REPOSITORY_ROOT,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                flags = set(re.findall(r"--[a-z][a-z0-9-]*", result.stdout))
                expected = {field.flag for field in operation.fields if field.flag}
                if operation.preview_flag:
                    expected.add("--dry-run")
                if operation.can_apply:
                    expected.update(operation.live_flags)
                expected.update(flag for flag in operation.fixed_args if flag.startswith("--"))
                if operation.id == "route":
                    expected.update({"--intake", "--restructure", "--promote", "--to-artist-root", "--relocate-compilations"})
                self.assertEqual(expected - flags, set())


class PersistentJobsTests(unittest.TestCase):
    def test_queued_cancel_releases_lock_and_log_paths_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            with open_db(db_path) as conn:
                conn.execute(
                    "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, status, "
                    "log_path, created_at) VALUES ('queued', 'tag', '{}', 'preview', 'f', '[]', '[]', 'queued', ?, ?)",
                    (str(root / "logs" / "queued.log"), "2026-01-01"),
                )
                conn.commit()
                cancel_job(conn, "queued")
                self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id='queued'").fetchone()["status"], "cancelled")
            log_path = root / "logs" / "job.log"
            log_path.parent.mkdir()
            log_path.write_text("test log", encoding="utf-8")
            self.assertEqual(read_job_log(log_path, allowed_dir=root / "logs")[0], "test log")
            outside = root / "outside.log"
            outside.write_text("private", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "outside"):
                read_job_log(outside, allowed_dir=root / "logs")

    def test_analysis_job_refuses_when_legacy_extraction_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("cratedigger.jobs.external_analysis_running", return_value=True):
                with open_db(root / "state.db") as conn:
                    for operation_id, params in (("analyze", {}), ("recommend-evaluate", {})):
                        with self.subTest(operation_id=operation_id), self.assertRaisesRegex(
                            RuntimeError, "separate 06_analyze.py process"
                        ):
                            start_job(
                                conn,
                                db_path=root / "state.db",
                                operation_id=operation_id,
                                raw_params=params,
                                mode="preview",
                                repo_root=REPOSITORY_ROOT,
                                logs_dir=root / "logs",
                                music_root=root / "music",
                            )

    def test_preview_lock_and_matching_apply_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            logs = root / "logs"
            started: list[list[str]] = []

            def fake_popen(argv: list[str], **_kwargs: object):
                started.append(argv)
                return FakeWorker()

            with patch("cratedigger.jobs.subprocess.Popen", side_effect=fake_popen):
                with open_db(db_path) as conn:
                    preview_id = start_job(
                        conn,
                        db_path=db_path,
                        operation_id="normalize-covers",
                        raw_params={"folder": "A"},
                        mode="preview",
                        repo_root=REPOSITORY_ROOT,
                        logs_dir=logs,
                        music_root=root / "music",
                    )
                    preview = conn.execute("SELECT status, mode, fingerprint FROM jobs WHERE id=?", (preview_id,)).fetchone()
                    self.assertEqual((preview["status"], preview["mode"]), ("queued", "preview"))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0], 1)
                    with self.assertRaisesRegex(RuntimeError, "Resource is busy"):
                        start_job(
                            conn,
                            db_path=db_path,
                            operation_id="rename",
                            raw_params={},
                            mode="preview",
                            repo_root=REPOSITORY_ROOT,
                            logs_dir=logs,
                            music_root=root / "music",
                        )

                    conn.execute(
                        "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                        (preview_id,),
                    )
                    conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview_id,))
                    conn.commit()
                    apply_id = start_job(
                        conn,
                        db_path=db_path,
                        operation_id="normalize-covers",
                        raw_params={"folder": "A"},
                        mode="apply",
                        repo_root=REPOSITORY_ROOT,
                        logs_dir=logs,
                        music_root=root / "music",
                    )
                    applied = conn.execute("SELECT argv_json FROM jobs WHERE id=?", (apply_id,)).fetchone()
                    self.assertIn("--no-dry-run", json.loads(applied["argv_json"]))
                    self.assertEqual(len(started), 2)

    def test_apply_requires_successful_matching_preview_and_blocks_stale_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            with patch("cratedigger.jobs.subprocess.Popen", return_value=FakeWorker()):
                with open_db(db_path) as conn:
                    with self.assertRaisesRegex(ValueError, "successful preview"):
                        start_job(
                            conn, db_path=db_path, operation_id="normalize-covers", raw_params={"folder": "A"},
                            mode="apply", repo_root=REPOSITORY_ROOT, logs_dir=root / "logs", music_root=root / "music",
                        )
                    preview_id = start_job(
                        conn, db_path=db_path, operation_id="normalize-covers", raw_params={"folder": "A"},
                        mode="preview", repo_root=REPOSITORY_ROOT, logs_dir=root / "logs", music_root=root / "music",
                    )
                    conn.execute(
                        "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                        (preview_id,),
                    )
                    conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview_id,))
                    conn.commit()
                    apply_id = start_job(
                        conn, db_path=db_path, operation_id="normalize-covers", raw_params={"folder": "A"},
                        mode="apply", repo_root=REPOSITORY_ROOT, logs_dir=root / "logs", music_root=root / "music",
                    )
                    conn.execute(
                        "UPDATE jobs SET status='succeeded', started_at='2026-01-02T00:00:00+00:00' WHERE id=?",
                        (apply_id,),
                    )
                    conn.execute("DELETE FROM job_locks")
                    conn.commit()
                    with self.assertRaisesRegex(ValueError, "library changed"):
                        start_job(
                            conn, db_path=db_path, operation_id="normalize-covers", raw_params={"folder": "A"},
                            mode="apply", repo_root=REPOSITORY_ROOT, logs_dir=root / "logs", music_root=root / "music",
                        )

    def test_worker_executes_preview_and_persists_result_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music_root = root / "music"
            library = music_root / "A"
            library.mkdir(parents=True)
            db_path = root / "state.db"
            with open_db(db_path) as conn:
                job_id = start_job(
                    conn,
                    db_path=db_path,
                    operation_id="normalize-covers",
                    raw_params={"library_root": str(library)},
                    mode="preview",
                    repo_root=REPOSITORY_ROOT,
                    logs_dir=root / "logs",
                    music_root=music_root,
                )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                with open_db(db_path) as conn:
                    job = conn.execute("SELECT status, exit_code, log_path, pid FROM jobs WHERE id=?", (job_id,)).fetchone()
                if job["status"] in {"succeeded", "failed", "cancelled", "stale"}:
                    break
                time.sleep(0.1)
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(job["exit_code"], 0)
            log_path = Path(job["log_path"])
            self.assertTrue(log_path.is_file())
            self.assertIn("--dry-run", log_path.read_text(encoding="utf-8"))
            self.assertEqual((library).exists(), True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(int(job["pid"]), 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("Worker process did not exit after persisting its terminal status")


if __name__ == "__main__":
    unittest.main()

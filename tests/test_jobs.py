"""Tests for the allowlisted operation registry and persistent job lifecycle."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cratedigger.db import open_db
from cratedigger.jobs import (
    cancel_job, external_analysis_running, library_mutation_guard, read_job_log, reconcile_jobs, start_job,
)
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

    def test_preview_requirements_and_operation_flags(self) -> None:
        self.assertIn("--dry-run", build_argv("audit-repair", {}, mode="preview"))
        with self.assertRaisesRegex(ValueError, "required to apply"):
            build_argv("audit-repair", {}, mode="apply")
        self.assertEqual(
            build_argv("repair-bpm", {}, mode="apply")[-2:],
            ["--fix", "--no-dry-run"],
        )
        collision = build_argv("reconcile-artists", {"collision_report": True}, mode="preview")
        self.assertEqual(collision[-1], "--collision-report")
        bpm = build_argv("repair-bpm", {"actions": "clear_corrupt, update_mismatch"}, mode="preview")
        index = bpm.index("--actions")
        self.assertEqual(bpm[index + 1:index + 3], ["clear_corrupt", "update_mismatch"])
        with self.assertRaisesRegex(ValueError, "Actions must"):
            build_argv("repair-bpm", {"actions": "--no-dry-run"}, mode="preview")
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite"):
                build_argv("repair-bpm", {"tolerance": value}, mode="preview")

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
    def setUp(self) -> None:
        patcher = patch("cratedigger.jobs.external_analysis_running", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

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
                    for operation_id, params in (
                        ("analyze", {}), ("recommend-evaluate", {}), ("recommend-export", {}),
                        ("normalize-covers", {})
                    ):
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
            (root / "music" / "A").mkdir(parents=True)
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
                            operation_id="enrich-covers",
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
            (root / "music" / "A").mkdir(parents=True)
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

    def test_existing_locks_migrate_and_preserve_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            with open_db(db_path) as conn:
                conn.execute(
                    "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, "
                    "status, log_path, created_at) VALUES "
                    "('old', 'tag', '{}', 'preview', 'f', '[]', '[]', 'queued', 'old.log', '2026-01-01')"
                )
                conn.execute("DROP TABLE job_locks")
                conn.execute(
                    "CREATE TABLE job_locks (lock_key TEXT PRIMARY KEY, "
                    "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE)"
                )
                conn.execute("INSERT INTO job_locks VALUES ('music_root:read', 'old')")
                conn.commit()
            with open_db(db_path) as conn:
                columns = {row["name"]: row["pk"] for row in conn.execute("PRAGMA table_info(job_locks)")}
                self.assertEqual(columns["job_id"], 2)
                self.assertEqual(conn.execute("SELECT job_id FROM job_locks").fetchone()[0], "old")
                conn.execute(
                    "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, "
                    "status, log_path, created_at) VALUES "
                    "('new', 'tag', '{}', 'preview', 'f', '[]', '[]', 'queued', 'new.log', '2026-01-01')"
                )
                conn.execute("INSERT INTO job_locks VALUES ('music_root:read', 'new')")
                conn.commit()
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0], 2)

    def test_readers_share_lock_but_exclude_writers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            db_path = root / "state.db"
            reader = replace(
                OPERATIONS["normalize-covers"], id="test-reader", risk="read_only",
                locks=("music_root:read",), preview_flag=False,
            )
            with patch.dict(OPERATIONS, {"test-reader": reader}), patch(
                "cratedigger.jobs.subprocess.Popen", return_value=FakeWorker()
            ), open_db(db_path) as conn:
                args = dict(
                    db_path=db_path, raw_params={}, mode="preview",
                    repo_root=REPOSITORY_ROOT, logs_dir=root / "logs", music_root=music,
                )
                start_job(conn, operation_id="test-reader", **args)
                start_job(conn, operation_id="test-reader", **args)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0], 2)
                with self.assertRaisesRegex(RuntimeError, "Resource is busy"):
                    start_job(conn, operation_id="normalize-covers", **args)
                conn.execute("DELETE FROM job_locks")
                conn.commit()
                start_job(conn, operation_id="normalize-covers", **args)
                with self.assertRaisesRegex(RuntimeError, "Resource is busy"):
                    start_job(conn, operation_id="test-reader", **args)

    def test_root_scope_and_bucket_are_validated_before_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            (music / "A").mkdir(parents=True)
            db_path = root / "state.db"
            with patch("cratedigger.jobs.subprocess.Popen", return_value=FakeWorker()), open_db(db_path) as conn:
                args = dict(
                    db_path=db_path, mode="preview", repo_root=REPOSITORY_ROOT,
                    logs_dir=root / "logs", music_root=music,
                )
                with self.assertRaisesRegex(ValueError, "config.MUSIC_ROOT"):
                    start_job(conn, operation_id="rename", raw_params={}, **args)
                with self.assertRaisesRegex(ValueError, "existing directory"):
                    start_job(conn, operation_id="enrich-covers", raw_params={"folder": "missing"}, **args)
                with self.assertRaisesRegex(ValueError, "Buckets"):
                    start_job(conn, operation_id="enrich-covers", raw_params={"buckets": "../outside"}, **args)
                outside = root / "outside"
                outside.mkdir()
                (music / "B").symlink_to(outside, target_is_directory=True)
                for params in ({"bucket": "B"}, {"buckets": "A-C"}, {"all_buckets": True}):
                    with self.subTest(params=params), self.assertRaisesRegex(ValueError, "Bucket folders"):
                        start_job(conn, operation_id="enrich-covers", raw_params=params, **args)
                job_id = start_job(conn, operation_id="normalize-covers", raw_params={"folder": "A"}, **args)
                argv = json.loads(conn.execute("SELECT argv_json FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
                self.assertEqual(argv[argv.index("--library-root") + 1], str(music))

    def test_direct_mutation_conflicts_and_invalidates_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            db_path = root / "state.db"
            args = dict(
                db_path=db_path, operation_id="normalize-covers", raw_params={},
                repo_root=REPOSITORY_ROOT, logs_dir=root / "logs", music_root=music,
            )
            with patch("cratedigger.jobs.subprocess.Popen", return_value=FakeWorker()), open_db(db_path) as conn:
                preview = start_job(conn, mode="preview", **args)
                with self.assertRaisesRegex(RuntimeError, "Resource is busy"):
                    with library_mutation_guard(db_path, REPOSITORY_ROOT):
                        pass
                conn.execute(
                    "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                    (preview,),
                )
                conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview,))
                conn.commit()
                with library_mutation_guard(db_path, REPOSITORY_ROOT):
                    with self.assertRaisesRegex(RuntimeError, "Resource is busy"):
                        start_job(conn, mode="preview", **args)
                with self.assertRaisesRegex(ValueError, "library changed"):
                    start_job(conn, mode="apply", **args)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM ui_mutation_locks").fetchone()[0], 0)

    def test_preview_fingerprint_tracks_report_and_script_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            (root / "scripts").mkdir()
            script = root / "scripts" / "apply_beets_matches.py"
            script.write_text("# script one\n", encoding="utf-8")
            registry = root / "cratedigger" / "ops" / "registry.py"
            registry.parent.mkdir(parents=True)
            registry.write_text("# registry\n", encoding="utf-8")
            report = root / "reports" / "review.json"
            report.parent.mkdir()
            report.write_text('{"evidence": 1}', encoding="utf-8")
            db_path = root / "state.db"
            args = dict(
                db_path=db_path, operation_id="apply-beets",
                raw_params={"report": str(report), "release_id": "release"},
                repo_root=root, logs_dir=root / "logs", music_root=music,
            )
            with patch("cratedigger.jobs.config.MUSIC_ROOT", music), patch(
                "cratedigger.jobs.subprocess.Popen", return_value=FakeWorker()
            ), open_db(db_path) as conn:
                preview = start_job(conn, mode="preview", **args)
                argv = json.loads(conn.execute("SELECT argv_json FROM jobs WHERE id=?", (preview,)).fetchone()[0])
                snapshot = Path(argv[argv.index("--report") + 1])
                self.assertNotEqual(snapshot, report)
                self.assertEqual(snapshot.read_text(encoding="utf-8"), report.read_text(encoding="utf-8"))
                conn.execute(
                    "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                    (preview,),
                )
                conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview,))
                conn.commit()
                report.write_text('{"evidence": 2}', encoding="utf-8")
                self.assertEqual(snapshot.read_text(encoding="utf-8"), '{"evidence": 1}')
                with self.assertRaisesRegex(ValueError, "successful preview"):
                    start_job(conn, mode="apply", **args)
                report.write_text('{"evidence": 1}', encoding="utf-8")
                script.write_text("# script two\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "successful preview"):
                    start_job(conn, mode="apply", **args)

    def test_apply_from_job_checks_the_selected_preview_not_a_newer_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            db_path = root / "state.db"
            args = dict(
                db_path=db_path, operation_id="normalize-covers", raw_params={},
                repo_root=REPOSITORY_ROOT, logs_dir=root / "logs", music_root=music,
            )
            with patch("cratedigger.jobs.subprocess.Popen", return_value=FakeWorker()), open_db(db_path) as conn:
                first = start_job(conn, mode="preview", **args)
                conn.execute(
                    "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                    (first,),
                )
                conn.execute("DELETE FROM job_locks WHERE job_id=?", (first,))
                conn.commit()
                with library_mutation_guard(db_path, REPOSITORY_ROOT):
                    pass
                second = start_job(conn, mode="preview", **args)
                conn.execute(
                    "UPDATE jobs SET status='succeeded', finished_at='2099-01-01T00:00:00+00:00' WHERE id=?",
                    (second,),
                )
                conn.execute("DELETE FROM job_locks WHERE job_id=?", (second,))
                conn.commit()
                with self.assertRaisesRegex(ValueError, "library changed"):
                    start_job(conn, mode="apply", expected_preview_id=first, **args)
                applied = start_job(conn, mode="apply", expected_preview_id=second, **args)
                self.assertEqual(conn.execute("SELECT mode FROM jobs WHERE id=?", (applied,)).fetchone()[0], "apply")

    def test_generated_reports_are_unique_and_stay_under_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            (root / "scripts").mkdir()
            (root / "scripts" / "reconcile_genres.py").write_text("# script\n", encoding="utf-8")
            registry = root / "cratedigger" / "ops" / "registry.py"
            registry.parent.mkdir(parents=True)
            registry.write_text("# registry\n", encoding="utf-8")
            db_path = root / "state.db"
            with patch("cratedigger.jobs.config.MUSIC_ROOT", music), patch(
                "cratedigger.jobs.subprocess.Popen", return_value=FakeWorker()
            ), open_db(db_path) as conn:
                job_id = start_job(
                    conn, db_path=db_path, operation_id="reconcile-genres", raw_params={},
                    mode="preview", repo_root=root, logs_dir=root / "logs", music_root=music,
                )
                argv = json.loads(conn.execute("SELECT argv_json FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
                report = Path(argv[argv.index("--report") + 1])
                self.assertEqual(report, root / "reports" / "genres" / f"ui-{job_id}.json")
                self.assertTrue(report.parent.is_dir())

    def test_cancelled_queued_worker_never_launches_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            marker = root / "ran"
            argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
            with open_db(db_path) as conn:
                conn.execute(
                    "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, "
                    "status, log_path, created_at) VALUES (?, 'test', '{}', 'preview', 'f', ?, '[]', "
                    "'queued', ?, '2026-01-01')",
                    ("cancelled", json.dumps(argv), str(root / "job.log")),
                )
                conn.commit()
                cancel_job(conn, "cancelled")
            result = subprocess.run(
                [sys.executable, "-m", "cratedigger.jobs_worker", "--db", str(db_path), "--job-id", "cancelled"],
                cwd=REPOSITORY_ROOT, start_new_session=True, timeout=10, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())

    def test_orphaned_process_group_keeps_locks_until_it_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            try:
                with open_db(db_path) as conn:
                    conn.execute(
                        "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, "
                        "status, pid, pgid, log_path, created_at) VALUES "
                        "('orphan', 'tag', '{}', 'preview', 'f', '[]', '[]', 'running', 99999999, ?, ?, '2026-01-01')",
                        (child.pid, str(root / "job.log")),
                    )
                    conn.execute("INSERT INTO job_locks VALUES ('music_root:write', 'orphan')")
                    conn.commit()
                    reconcile_jobs(conn)
                    self.assertEqual(conn.execute("SELECT status FROM jobs").fetchone()[0], "orphaned")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0], 1)
                    child.terminate()
                    child.wait(timeout=10)
                    reconcile_jobs(conn)
                    self.assertEqual(conn.execute("SELECT status FROM jobs").fetchone()[0], "stale")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0], 0)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)

    def test_running_cancellation_waits_for_stubborn_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "state.db"
            child_ready = root / "child.pid"
            parent_ready = root / "parent.pid"
            child_script = root / "child.py"
            parent_script = root / "parent.py"
            child_script.write_text(
                "import os, signal, time\n"
                "from pathlib import Path\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"Path({str(child_ready)!r}).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            parent_script.write_text(
                "import os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                f"Path({str(parent_ready)!r}).write_text(str(os.getpid()))\n"
                f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            with open_db(db_path) as conn:
                conn.execute(
                    "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, "
                    "status, log_path, created_at) VALUES "
                    "('running', 'test', '{}', 'preview', 'f', ?, '[]', 'queued', ?, '2026-01-01')",
                    (json.dumps([sys.executable, str(parent_script)]), str(root / "job.log")),
                )
                conn.execute("INSERT INTO job_locks VALUES ('music_root:write', 'running')")
                conn.commit()
            worker = subprocess.Popen(
                [sys.executable, "-m", "cratedigger.jobs_worker", "--db", str(db_path), "--job-id", "running"],
                cwd=REPOSITORY_ROOT, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while not child_ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(child_ready.exists(), "Synthetic operation did not start its child")
                with open_db(db_path) as conn:
                    cancel_job(conn, "running")
                    time.sleep(0.5)
                    self.assertEqual(conn.execute("SELECT status FROM jobs").fetchone()[0], "cancelling")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0], 1)
                worker.wait(timeout=15)
                with open_db(db_path) as conn:
                    self.assertEqual(conn.execute("SELECT status FROM jobs").fetchone()[0], "cancelled")
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_locks").fetchone()[0], 0)
            finally:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait(timeout=10)
                for marker, script in ((parent_ready, parent_script), (child_ready, child_script)):
                    if not marker.exists():
                        continue
                    pid = int(marker.read_text())
                    try:
                        command = Path(f"/proc/{pid}/cmdline").read_bytes()
                        if os.fsencode(str(script)) in command:
                            os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except FileNotFoundError:
                        pass

    def test_detects_analyzer_with_interpreter_flags_and_absolute_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc"
            process = proc / "123"
            process.mkdir(parents=True)
            command = process / "cmdline"
            command.write_bytes(b"/venv/bin/python\0-u\0/other/repo/06_analyze.py\0--workers\0")
            self.assertTrue(external_analysis_running(root, proc_root=proc))
            command.write_bytes(b"/venv/bin/python\0-u\0/other/repo/not_06_analyze.py\0")
            self.assertFalse(external_analysis_running(root, proc_root=proc))

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

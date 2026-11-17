"""Tests for reviewed, complete-library sync jobs using isolated directories."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from cratedigger.analytics import source_file_state
from cratedigger.db import open_db
from cratedigger.jobs import cancel_job, start_job
from cratedigger.ops.registry import REPOSITORY_ROOT, build_argv
from cratedigger.server_sync import reviewed_sync_plan, run_guarded_sync, sync_scope
from cratedigger.web.app import create_app


@unittest.skipUnless(shutil.which("rsync") and shutil.which("bash"), "rsync and bash are required")
class GuardedSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.destination.mkdir()
        self.write(self.source, "A/Artist/track.mp3", b"fixture audio")
        self.write(self.source, "Playlists/Faves.m3u8", b"#EXTM3U\n")
        self.write(self.destination, "obsolete.mp3", b"old")
        self.write(self.destination, "0videos/protected.mp4", b"protected")

    def write(self, root: Path, relative: str, data: bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self, root: Path) -> dict[str, bytes | None]:
        return {
            str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*")
        }

    def preview(self) -> tuple[dict, str]:
        stdout = StringIO()
        with redirect_stdout(stdout):
            plan = run_guarded_sync(self.source, str(self.destination), complete_source=True)
        return plan, stdout.getvalue()

    def test_preview_is_repeatable_and_matching_apply_mirrors_with_remote_reminder(self) -> None:
        before = self.snapshot(self.destination)
        plan, text = self.preview()
        repeated, _ = self.preview()
        self.assertEqual(self.snapshot(self.destination), before)
        self.assertEqual(plan["sha256"], repeated["sha256"])
        self.assertGreaterEqual(plan["changes"], 1)
        self.assertGreaterEqual(plan["deletions"], 1)
        self.assertIn("@@sync-change *deleting", text)
        self.assertIn("obsolete.mp3", text)
        self.assertIn("@@sync-plan ", text)

        stdout = StringIO()
        with redirect_stdout(stdout):
            run_guarded_sync(
                self.source, str(self.destination), complete_source=True, dry_run=False,
                expected_plan=plan["sha256"],
            )
        self.assertEqual((self.destination / "A/Artist/track.mp3").read_bytes(), b"fixture audio")
        self.assertFalse((self.destination / "obsolete.mp3").exists())
        self.assertEqual((self.destination / "0videos/protected.mp4").read_bytes(), b"protected")
        self.assertIn("Refresh the player on the remote host", stdout.getvalue())

    def test_changed_source_between_preview_and_apply_is_rejected(self) -> None:
        plan, _ = self.preview()
        track = self.source / "A/Artist/track.mp3"
        track.write_bytes(b"changed fixture audio, different size")
        before = self.snapshot(self.destination)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "plan changed"):
            run_guarded_sync(
                self.source, str(self.destination), complete_source=True, dry_run=False,
                expected_plan=plan["sha256"],
            )
        self.assertEqual(self.snapshot(self.destination), before)

    def test_changed_destination_between_preview_and_apply_is_rejected(self) -> None:
        self.write(self.destination, "old/nested/first.mp3", b"old")
        plan, _ = self.preview()
        self.write(self.destination, "old/nested/second.mp3", b"new")
        before = self.snapshot(self.destination)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "plan changed"):
            run_guarded_sync(
                self.source, str(self.destination), complete_source=True, dry_run=False,
                expected_plan=plan["sha256"],
            )
        self.assertEqual(self.snapshot(self.destination), before)

    def test_requires_matching_plan_and_unchanged_root_identities(self) -> None:
        before = self.snapshot(self.destination)
        source_state = source_file_state(self.source)
        destination_state = source_file_state(self.destination)
        changed = (source_state[0], source_state[1], source_state[2] + 1, source_state[3])
        with patch("cratedigger.server_sync._run_sync", side_effect=AssertionError("unexpected rsync")):
            with self.assertRaisesRegex(ValueError, "successful reviewed sync preview"):
                run_guarded_sync(self.source, str(self.destination), complete_source=True, dry_run=False)
            with self.assertRaisesRegex(RuntimeError, "source changed after"):
                run_guarded_sync(
                    self.source, str(self.destination), complete_source=True, expected_source_state=changed,
                )
            with self.assertRaisesRegex(RuntimeError, "destination changed after"):
                run_guarded_sync(
                    self.source, str(self.destination), complete_source=True,
                    check_destination_state=True, expected_destination_state=None,
                )
        self.assertEqual(source_file_state(self.destination), destination_state)
        self.assertEqual(self.snapshot(self.destination), before)

    def test_scope_rejects_empty_partial_roots_overlap_and_unsafe_remote_arguments(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        self.write(empty, "0videos/clip.mp3", b"excluded")
        self.write(empty, ".hidden/track.mp3", b"excluded")
        (empty / "link.mp3").symlink_to(self.source / "A/Artist/track.mp3")
        with self.assertRaisesRegex(ValueError, "no eligible audio"):
            sync_scope(empty, str(self.destination), complete_source=True)
        with self.assertRaisesRegex(ValueError, "Confirm"):
            sync_scope(self.source, str(self.destination), complete_source=False)
        for destination in (
            "/", str(self.source), str(self.source / "A"), str(self.root),
            "relative", "-option", "host:/", "host:/foo/../bar", "host:/foo;command",
            "user@-host:/music", "host:/music with spaces", "localhost:/music",
            "user@127.0.0.1:/music", "127.1:/music", "2130706433:/music", "0x7f000001:/music",
            "0:/music", "[::1]:/music", "[::ffff:0:0]:/music", str(Path.home()), str(REPOSITORY_ROOT),
        ):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                sync_scope(self.source, destination, complete_source=True)
        source, destination, local = sync_scope(
            self.source, "user@nas.example:/volume1/music/", complete_source=True,
        )
        self.assertEqual(source, self.source)
        self.assertEqual(destination, "user@nas.example:/volume1/music")
        self.assertIsNone(local)
        with self.assertRaises(ValueError):
            sync_scope("/", str(self.destination), complete_source=True)
        with self.assertRaisesRegex(ValueError, "broader home or repository"):
            sync_scope(Path.home(), str(self.destination), complete_source=True)
        with self.assertRaisesRegex(ValueError, "not a parent or subfolder"):
            sync_scope(
                self.source / "A", str(self.destination), complete_source=True,
                protected_library_root=self.source,
            )
        with self.assertRaisesRegex(ValueError, "configured music library"):
            sync_scope(
                self.source, str(self.destination), complete_source=True,
                protected_library_root=self.destination,
            )

    def test_preview_log_must_match_source_destination_and_digest(self) -> None:
        plan, text = self.preview()
        logs = self.root / "logs"
        logs.mkdir()
        log = logs / "preview.log"
        log.write_text("π" * 40000 + "\n" + text, encoding="utf-8")
        self.assertEqual(reviewed_sync_plan(log, logs, self.source, str(self.destination)), plan["sha256"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            reviewed_sync_plan(log, logs, self.source, "host:/other")
        log.write_text("@@sync-plan invalid-json\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid recorded plan"):
            reviewed_sync_plan(log, logs, self.source, str(self.destination))
        log.write_text("no plan\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no recorded plan"):
            reviewed_sync_plan(log, logs, self.source, str(self.destination))
        with self.assertRaisesRegex(ValueError, "outside"):
            reviewed_sync_plan(self.root / "outside.log", logs, self.source, str(self.destination))

    def test_shell_refuses_itemized_mode_without_dry_run(self) -> None:
        result = subprocess.run(
            ["bash", str(REPOSITORY_ROOT / "scripts/server_sync.sh"), "--itemize-plan",
             "--source", str(self.source), "--dest", str(self.destination)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --dry-run", result.stderr)

    def test_subprocess_does_not_prompt_and_preserves_a_custom_remote_shell(self) -> None:
        with patch.dict(os.environ, {"RSYNC_RSH": "ssh -p 2222"}), patch(
            "cratedigger.server_sync.subprocess.Popen"
        ) as popen, redirect_stdout(StringIO()):
            process = popen.return_value.__enter__.return_value
            process.stdout = iter(["@@sync-change >f+++++++++|12|2026/01/01-00:00:00|track.mp3\n"])
            process.wait.return_value = 0
            run_guarded_sync(self.source, str(self.destination), complete_source=True)
            self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(popen.call_args.kwargs["env"]["SSH_ASKPASS_REQUIRE"], "never")
            self.assertEqual(popen.call_args.kwargs["env"]["RSYNC_RSH"], "ssh -p 2222")

    def test_registry_job_requires_preview_and_captures_reviewed_plan(self) -> None:
        db = self.root / "state.db"
        logs = self.root / "logs"
        params = {"source": str(self.source), "dest": str(self.destination), "complete_source": True}
        kwargs = dict(
            db_path=db, operation_id="server-sync", raw_params=params, repo_root=REPOSITORY_ROOT,
            logs_dir=logs, music_root=self.root / "unused-library",
        )
        with self.assertRaisesRegex(ValueError, "Complete Source"):
            build_argv("server-sync", {"source": str(self.source), "dest": str(self.destination)}, mode="preview")
        plan, text = self.preview()
        with patch("cratedigger.jobs.subprocess.Popen") as popen, open_db(db) as conn:
            popen.return_value.pid = 12345
            with self.assertRaisesRegex(ValueError, "successful preview"):
                start_job(conn, mode="apply", **kwargs)
            preview = start_job(conn, mode="preview", **kwargs)
            row = conn.execute("SELECT argv_json, log_path FROM jobs WHERE id=?", (preview,)).fetchone()
            argv = json.loads(row["argv_json"])
            self.assertIn("--complete-source", argv)
            self.assertIn("--dry-run", argv)
            self.assertEqual(
                json.loads(argv[argv.index("--expected-source-state") + 1]),
                list(source_file_state(self.source)),
            )
            self.assertEqual(argv[argv.index("--protected-library-root") + 1], str(self.root / "unused-library"))
            Path(row["log_path"]).write_text(text, encoding="utf-8")
            conn.execute(
                "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (preview,),
            )
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview,))
            conn.commit()
            apply_id = start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)
            apply_argv = json.loads(conn.execute("SELECT argv_json FROM jobs WHERE id=?", (apply_id,)).fetchone()[0])
            self.assertIn("--no-dry-run", apply_argv)
            self.assertEqual(apply_argv[apply_argv.index("--expected-plan") + 1], plan["sha256"])
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (apply_id,))
            conn.execute("DELETE FROM jobs WHERE id=?", (apply_id,))
            conn.commit()
            Path(row["log_path"]).write_text("missing plan\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no recorded plan"):
                start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)

    def test_web_preview_apply_and_remote_reminder_with_temporary_mirror(self) -> None:
        client, db, csrf, preview = self._start_ui_preview()
        self.assertTrue((self.destination / "obsolete.mp3").exists())
        detail = client.get(f"/jobs/{preview}")
        self.assertIn("Apply this preview", detail.text)
        self.assertIn("deletes destination paths", detail.text)
        self.assertIn("@@sync-plan", detail.text)

        response = client.post(
            f"/jobs/{preview}/apply", data={"csrf_token": csrf}, follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        applied = urlsplit(response.headers["location"]).path.rsplit("/", 1)[-1]
        self.assertNotEqual(applied, preview)
        self.assertEqual(self._wait_for_job(db, applied), "succeeded")
        self.assertFalse((self.destination / "obsolete.mp3").exists())
        self.assertEqual((self.destination / "A/Artist/track.mp3").read_bytes(), b"fixture audio")
        self.assertEqual((self.destination / "0videos/protected.mp4").read_bytes(), b"protected")
        detail = client.get(f"/jobs/{applied}")
        self.assertIn("Refresh the player on the remote host", detail.text)
        self.assertIn("no local refresh was run", detail.text)

    def test_web_apply_rechecks_the_plan_after_nested_destination_changes(self) -> None:
        self.write(self.destination, "old/nested/first.mp3", b"old")
        client, db, csrf, preview = self._start_ui_preview()
        root_state = source_file_state(self.destination)
        self.write(self.destination, "old/nested/second.mp3", b"new")
        self.assertEqual(source_file_state(self.destination), root_state)
        before = self.snapshot(self.destination)
        response = client.post(
            f"/jobs/{preview}/apply", data={"csrf_token": csrf}, follow_redirects=False,
        )
        applied = urlsplit(response.headers["location"]).path.rsplit("/", 1)[-1]
        self.assertNotEqual(applied, preview)
        self.assertEqual(self._wait_for_job(db, applied), "failed")
        self.assertEqual(self.snapshot(self.destination), before)
        self.assertIn("sync plan changed since preview", client.get(f"/jobs/{applied}").text)

    def _start_ui_preview(self) -> tuple[TestClient, Path, str, str]:
        db = self.root / "web.db"
        music = self.root / "ui-library"
        music.mkdir()
        client = TestClient(create_app(
            db_path=db, music_root=music, logs_dir=self.root / "web-logs",
        ), base_url="http://127.0.0.1:8765")
        page = client.get("/ops")
        self.assertIn("I confirm this source is a complete library mirror", page.text)
        csrf = client.cookies["cratedigger_session"]
        missing_confirmation = client.post(
            "/ops/server-sync/start",
            data={
                "csrf_token": csrf, "source": str(self.source), "dest": str(self.destination),
                "mode": "preview",
            },
            follow_redirects=False,
        )
        self.assertIn("err=", missing_confirmation.headers["location"])
        with open_db(db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        response = client.post(
            "/ops/server-sync/start",
            data={
                "csrf_token": csrf, "source": str(self.source), "dest": str(self.destination),
                "complete_source": "true", "mode": "preview",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        preview = urlsplit(response.headers["location"]).path.rsplit("/", 1)[-1]
        self.assertEqual(self._wait_for_job(db, preview), "succeeded")
        return client, db, csrf, preview

    def _wait_for_job(self, db: Path, job_id: str) -> str:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with open_db(db) as conn:
                row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None and row["status"] in {"succeeded", "failed", "cancelled", "stale"}:
                return str(row["status"])
            time.sleep(0.1)
        with open_db(db) as conn:
            cancel_job(conn, job_id)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            with open_db(db) as conn:
                row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is not None and row["status"] in {"succeeded", "failed", "cancelled", "stale"}:
                break
            time.sleep(0.1)
        self.fail(f"Timed out waiting for fixture job {job_id}")


if __name__ == "__main__":
    unittest.main()

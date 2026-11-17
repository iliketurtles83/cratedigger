"""Isolated preview/apply tests for recommender backfill jobs."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd

from cratedigger.analytics import source_file_state
from cratedigger.db import open_db
from cratedigger.feature_backfill import BackfillKind, reviewed_backfill_plan, run_guarded_backfill
from cratedigger.jobs import external_backfill_running, library_mutation_guard, start_job
from cratedigger.ops.registry import REPOSITORY_ROOT, build_argv
from cratedigger.web.app import create_app
from lib.audiohash import audio_hash
from lib.embeddings import EMBEDDING_OK, LATENT_DIM, STYLE_DIM, EmbeddingResult, embedding_fields
from scripts import run_recommend_backfill as backfill_cli


class GuardedBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "features.pkl"
        self.audio = self.root / "track.mp3"
        self.missing = self.root / "missing.mp3"
        self.audio.write_bytes(b"fixture audio")
        pd.DataFrame([
            {"path": str(self.audio), "audio_hash": None, "schema_version": 12},
            {"path": str(self.missing), "audio_hash": None, "schema_version": 11},
            {"path": str(self.audio), "audio_hash": "a1:existing", "schema_version": 10},
        ]).to_pickle(self.store)

    def preview(self, kind: BackfillKind = "hashes", *, max_embeddings: int | None = None) -> tuple[dict, str]:
        output = StringIO()
        with redirect_stdout(output):
            summary = run_guarded_backfill(self.store, kind, max_embeddings=max_embeddings)
        return summary, output.getvalue()

    def assert_no_stage(self) -> None:
        self.assertEqual(list(self.root.glob(".feature-backfill-*")), [])

    def test_hash_preview_and_apply_only_publish_successful_changes(self) -> None:
        original = self.store.read_bytes()
        audio_before = self.audio.read_bytes()
        if os.name == "posix":
            self.store.chmod(0o640)
        source_state = source_file_state(self.store)
        plan, text = self.preview()
        self.assertEqual((plan["selected"], plan["available"], plan["skipped"]), (2, 1, 1))
        self.assertIn("Preview does not compute features", text)
        self.assertEqual(self.store.read_bytes(), original)
        self.assert_no_stage()
        with redirect_stdout(StringIO()):
            result = run_guarded_backfill(
                self.store, "hashes", dry_run=False, expected_plan=plan["sha256"],
                expected_source_state=source_state,
            )
        self.assertTrue(result["published"])
        self.assertEqual((result["updated"], result["skipped"]), (1, 1))
        saved = pd.read_pickle(self.store)
        self.assertEqual(saved.loc[0, "audio_hash"], audio_hash(str(self.audio)))
        self.assertTrue(pd.isna(saved.loc[1, "audio_hash"]))
        self.assertEqual(saved.loc[2, "audio_hash"], "a1:existing")
        self.assertEqual(saved["schema_version"].tolist(), [12, 11, 10])
        self.assertEqual(self.audio.read_bytes(), audio_before)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o640)
        self.assert_no_stage()

    def test_changed_track_or_newly_available_path_invalidates_preview(self) -> None:
        original = self.store.read_bytes()
        plan, _ = self.preview()
        self.audio.write_bytes(b"changed audio")
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "plan changed"):
            run_guarded_backfill(self.store, "hashes", dry_run=False, expected_plan=plan["sha256"])
        self.assertEqual(self.store.read_bytes(), original)
        plan, _ = self.preview()
        self.missing.write_bytes(b"newly available")
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "plan changed"):
            run_guarded_backfill(self.store, "hashes", dry_run=False, expected_plan=plan["sha256"])
        self.assertEqual(self.store.read_bytes(), original)
        self.assert_no_stage()

    def test_standalone_preview_binds_the_feature_store_even_if_candidates_are_unchanged(self) -> None:
        plan, _ = self.preview()
        frame = pd.read_pickle(self.store)
        frame.loc[2, "schema_version"] = 99
        frame.to_pickle(self.store)
        external_change = self.store.read_bytes()
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "plan changed"):
            run_guarded_backfill(self.store, "hashes", dry_run=False, expected_plan=plan["sha256"])
        self.assertEqual(self.store.read_bytes(), external_change)
        self.assert_no_stage()

    def test_rejects_extractor_changed_store_and_failed_subprocess(self) -> None:
        original = self.store.read_bytes()
        plan, _ = self.preview()
        with patch("cratedigger.feature_backfill.pd.read_pickle", side_effect=AssertionError("unexpected read")):
            with self.assertRaisesRegex(RuntimeError, "extraction is running"):
                run_guarded_backfill(self.store, "hashes", extraction_running=lambda: True)
            changed = list(source_file_state(self.store))
            changed[2] += 1
            with self.assertRaisesRegex(RuntimeError, "changed after the job was queued"):
                run_guarded_backfill(self.store, "hashes", expected_source_state=tuple(changed))
        with patch(
            "cratedigger.feature_backfill.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["fake"]),
        ), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(RuntimeError, "original feature store was not changed"):
                run_guarded_backfill(self.store, "hashes", dry_run=False, expected_plan=plan["sha256"])
        self.assertEqual(self.store.read_bytes(), original)
        self.assert_no_stage()

    def test_requires_plan_and_rechecks_running_writer_before_publication(self) -> None:
        original = self.store.read_bytes()
        with patch("cratedigger.feature_backfill.pd.read_pickle", side_effect=AssertionError("unexpected read")):
            with self.assertRaisesRegex(ValueError, "reviewed backfill preview"):
                run_guarded_backfill(self.store, "hashes", dry_run=False)
        plan, _ = self.preview()

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            staged = Path(command[command.index("--store") + 1])
            frame = pd.read_pickle(staged)
            frame.at[0, "audio_hash"] = "a1:fixture"
            frame.to_pickle(staged)
            return subprocess.CompletedProcess(command, 0)

        running = iter((False, False, False, True))
        with patch("cratedigger.feature_backfill.subprocess.run", side_effect=fake_run), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(RuntimeError, "backfill is running"):
                run_guarded_backfill(
                    self.store, "hashes", dry_run=False, expected_plan=plan["sha256"],
                    extraction_running=lambda: next(running),
                )
        self.assertEqual(self.store.read_bytes(), original)
        self.assert_no_stage()

    def test_external_changes_during_backfill_prevent_publication(self) -> None:
        original = self.store.read_bytes()
        plan, _ = self.preview()

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            staged = Path(command[command.index("--store") + 1])
            frame = pd.read_pickle(staged)
            frame.at[0, "audio_hash"] = "a1:fixture"
            frame.to_pickle(staged)
            self.audio.write_bytes(b"changed while processing")
            return subprocess.CompletedProcess(command, 0)

        with patch("cratedigger.feature_backfill.subprocess.run", side_effect=fake_run), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(RuntimeError, "changed during backfill"):
                run_guarded_backfill(self.store, "hashes", dry_run=False, expected_plan=plan["sha256"])
        self.assertEqual(self.store.read_bytes(), original)
        self.assert_no_stage()

    def test_empty_backfill_does_not_replace_the_store(self) -> None:
        pd.DataFrame([{"path": str(self.audio), "audio_hash": "a1:present"}]).to_pickle(self.store)
        identity = source_file_state(self.store)
        plan, _ = self.preview()
        with patch("cratedigger.feature_backfill.subprocess.run", side_effect=AssertionError("unexpected process")):
            with redirect_stdout(StringIO()):
                result = run_guarded_backfill(self.store, "hashes", dry_run=False, expected_plan=plan["sha256"])
        self.assertFalse(result["published"])
        self.assertEqual(source_file_state(self.store), identity)

    def test_limited_embedding_preview_and_atomic_apply(self) -> None:
        second = self.root / "second.flac"
        second.write_bytes(b"second")
        pd.DataFrame([
            {"path": str(self.audio), "schema_version": 9, "error": None},
            {"path": str(second), "schema_version": 10, "error": None},
            {"path": str(self.missing), "schema_version": 10, "error": None},
            {"path": str(second), "schema_version": 9, "error": "duration_exceeds_20min"},
        ]).to_pickle(self.store)
        original = self.store.read_bytes()
        provenance = {"embedding_model_sha256": None, "style_labels_sha256": None}

        def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            self.assertEqual(command[command.index("--max-embeddings") + 1], "1")
            self.assertIn("--strict-backfill", command)
            staged = Path(command[command.index("--store") + 1])
            frame = pd.read_pickle(staged)
            fields = embedding_fields(EmbeddingResult(
                EMBEDDING_OK,
                style_activations=np.full(STYLE_DIM, 0.5, dtype=np.float32),
                audio_embedding=np.full(LATENT_DIM, 0.1, dtype=np.float32),
                patches=3,
            ))
            for column, value in fields.items():
                if column not in frame:
                    frame[column] = None
                frame[column] = frame[column].astype(object)
                frame.at[0, column] = value
            frame.to_pickle(staged)
            return subprocess.CompletedProcess(command, 0)

        with patch("lib.embeddings.model_provenance", return_value=provenance):
            plan, text = self.preview("embeddings", max_embeddings=1)
            self.assertEqual((plan["selected"], plan["available"], plan["skipped"]), (1, 2, 1))
            self.assertEqual(self.store.read_bytes(), original)
            self.assertIn("unavailable", text)
            with patch("cratedigger.feature_backfill.subprocess.run", side_effect=fake_run), redirect_stdout(StringIO()):
                result = run_guarded_backfill(
                    self.store, "embeddings", max_embeddings=1, dry_run=False, expected_plan=plan["sha256"],
                )
        self.assertTrue(result["published"])
        self.assertEqual(result["updated"], 1)
        saved = pd.read_pickle(self.store)
        self.assertEqual(saved.at[0, "embedding_status"], EMBEDDING_OK)
        self.assertTrue(pd.isna(saved.at[1, "embedding_status"]))
        self.assertEqual(saved["schema_version"].tolist(), [9, 10, 10, 9])
        self.assert_no_stage()

    def test_embedding_selection_rejects_ambiguous_indices_and_invalid_limits(self) -> None:
        frame = pd.DataFrame([{"path": str(self.audio)}, {"path": str(self.audio)}], index=[0, 0])
        frame.to_pickle(self.store)
        original = self.store.read_bytes()
        with self.assertRaisesRegex(ValueError, "unique"):
            self.preview("embeddings")
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "positive"):
                self.preview("embeddings", max_embeddings=limit)
        with self.assertRaisesRegex(ValueError, "positive"):
            self.preview("hashes", max_embeddings=1)
        self.assertEqual(self.store.read_bytes(), original)

    def test_preview_plan_log_is_bounded_and_matches_parameters(self) -> None:
        plan, text = self.preview()
        logs = self.root / "logs"
        logs.mkdir()
        path = logs / "job.log"
        path.write_text("π" * 40000 + "\n" + text, encoding="utf-8")
        self.assertEqual(reviewed_backfill_plan(path, logs, self.store, "hashes", None), plan["sha256"])
        with self.assertRaisesRegex(ValueError, "does not match"):
            reviewed_backfill_plan(path, logs, self.store, "embeddings", None)
        path.write_text("@@backfill-plan bad-json\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid recorded plan"):
            reviewed_backfill_plan(path, logs, self.store, "hashes", None)
        path.write_text("missing\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no recorded plan"):
            reviewed_backfill_plan(path, logs, self.store, "hashes", None)

    def test_standalone_cli_defaults_to_preview_and_blocks_active_writers(self) -> None:
        args = ["run_recommend_backfill.py", "--hashes", "--features", str(self.store)]
        with patch("sys.argv", args), patch("scripts.run_recommend_backfill.signal.signal"), patch(
            "scripts.run_recommend_backfill.external_analysis_running", return_value=False
        ), patch("scripts.run_recommend_backfill.external_backfill_running", return_value=False):
            with redirect_stdout(StringIO()) as output:
                backfill_cli.main()
        self.assertIn("@@backfill-plan", output.getvalue())
        with patch("sys.argv", args), patch("scripts.run_recommend_backfill.signal.signal"), patch(
            "scripts.run_recommend_backfill.external_analysis_running", return_value=False
        ), patch("scripts.run_recommend_backfill.external_backfill_running", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "extraction is running"):
                backfill_cli.main()

    def test_ui_registry_requires_reviewed_plan_and_locks_store(self) -> None:
        plan, text = self.preview()
        db = self.root / "state.db"
        logs = self.root / "logs"
        kwargs = dict(
            db_path=db, operation_id="recommend-backfill-hashes", raw_params={}, repo_root=REPOSITORY_ROOT,
            logs_dir=logs, music_root=self.root / "unused-library", features_path=self.store,
        )
        preview_argv = build_argv("recommend-backfill-embeddings", {"max_embeddings": 3}, mode="preview")
        self.assertIn("--embeddings", preview_argv)
        self.assertEqual(preview_argv[preview_argv.index("--max-embeddings") + 1], "3")
        with self.assertRaisesRegex(ValueError, "at least 1"):
            build_argv("recommend-backfill-embeddings", {"max_embeddings": 0}, mode="preview")
        with patch("cratedigger.jobs.external_analysis_running", return_value=False), patch(
            "cratedigger.jobs.external_backfill_running", return_value=False
        ), patch("cratedigger.jobs.subprocess.Popen") as popen, open_db(db) as conn:
            popen.return_value.pid = 12345
            with self.assertRaisesRegex(ValueError, "successful preview"):
                start_job(conn, mode="apply", **kwargs)
            preview = start_job(conn, mode="preview", **kwargs)
            row = conn.execute("SELECT log_path, argv_json FROM jobs WHERE id=?", (preview,)).fetchone()
            self.assertIn("--expected-source-state", json.loads(row["argv_json"]))
            with self.assertRaisesRegex(RuntimeError, "Resource is busy"):
                start_job(conn, mode="preview", **kwargs)
            Path(row["log_path"]).write_text(text, encoding="utf-8")
            conn.execute(
                "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (preview,),
            )
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview,))
            conn.commit()
            apply_id = start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)
            argv = json.loads(conn.execute("SELECT argv_json FROM jobs WHERE id=?", (apply_id,)).fetchone()[0])
            self.assertEqual(argv[argv.index("--expected-plan") + 1], plan["sha256"])
            self.assertIn("--no-dry-run", argv)

    def test_ui_exposes_both_backfills_and_clear_confirmation(self) -> None:
        music = self.root / "library"
        music.mkdir()
        client = TestClient(create_app(
            db_path=self.root / "web.db", music_root=music, features_path=self.store,
            logs_dir=self.root / "logs", analytics_path=self.root / "analytics.db",
        ), base_url="http://127.0.0.1:8765")
        page = client.get("/ops")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Backfill missing audio hashes", page.text)
        self.assertIn("Backfill missing or stale embeddings", page.text)
        self.assertIn("Backfill and replace the feature store? This cannot be undone.", page.text)

    def test_detects_legacy_backfills_but_not_read_only_recommender(self) -> None:
        proc = self.root / "proc"
        process = proc / "123"
        process.mkdir(parents=True)
        cmdline = process / "cmdline"
        cmdline.write_bytes(b"python\0/tmp/07_recommend.py\0--backfill-hashes\0")
        self.assertTrue(external_backfill_running(REPOSITORY_ROOT, proc_root=proc))
        cmdline.write_bytes(b"python\0/tmp/07_recommend.py\0--backfill-embeddings\0")
        self.assertTrue(external_backfill_running(REPOSITORY_ROOT, proc_root=proc))
        cmdline.write_bytes(b"python\0/tmp/07_recommend.py\0--evaluate\0")
        self.assertFalse(external_backfill_running(REPOSITORY_ROOT, proc_root=proc))
        with patch("cratedigger.jobs.external_analysis_running", return_value=False), patch(
            "cratedigger.jobs.external_backfill_running", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "standalone feature backfill"):
                with library_mutation_guard(self.root / "state.db", REPOSITORY_ROOT):
                    self.fail("guard should block")
            with open_db(self.root / "state.db") as conn, self.assertRaisesRegex(
                RuntimeError, "07_recommend.py backfill"
            ):
                start_job(
                    conn, db_path=self.root / "state.db", operation_id="recommend-backfill-hashes",
                    raw_params={}, mode="preview", repo_root=REPOSITORY_ROOT, logs_dir=self.root / "logs",
                    music_root=self.root / "unused-library", features_path=self.store,
                )


if __name__ == "__main__":
    unittest.main()

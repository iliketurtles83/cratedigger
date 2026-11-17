"""Tests for guarded player exports and their UI status."""

from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import gzip
from io import StringIO
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd

from cratedigger.analytics import source_file_state
from cratedigger.db import open_db
from cratedigger.feature_export import (
    build_player_export, export_output_path, output_file_state, validate_player_export,
)
from cratedigger.jobs import start_job
from cratedigger.ops.registry import REPOSITORY_ROOT, build_argv
from cratedigger.web.app import create_app
from lib import descriptors
from lib.embeddings import LATENT_DIM
from lib.recommender import VectorBaker, export_features
from scripts import export_features as export_cli


def _features() -> pd.DataFrame:
    base = {
        "embedding": np.ones(400, dtype=np.float32), "bpm": 120.0,
        "key": "C", "mode": "major", "signal_status": "ok", "decode_coverage": 1.0,
    }
    return pd.DataFrame([
        {**base, "audio_hash": "a1:one", "path": "/music/0compilations/List/one.mp3", "artist_tag": "Copy"},
        {**base, "audio_hash": "a1:one", "path": "/music/A/Artist/one.mp3", "artist_tag": "Artist"},
        {**base, "audio_hash": "a1:two", "path": "/music/A/Artist/two.mp3",
         "signal_status": "truncated", "decode_coverage": 0.8},
        {**base, "audio_hash": "a1:silent", "path": "/music/A/Artist/silent.mp3", "signal_status": "silent"},
        {**base, "audio_hash": "a1:short", "path": "/music/A/Artist/short.mp3", "decode_coverage": 0.1},
        {**base, "audio_hash": None, "path": "/music/A/Artist/unhashed.mp3"},
    ])


class PlayerExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "features.pkl"
        self.output = self.root / "output" / "features.json.gz"
        _features().to_pickle(self.source)
        patcher = patch("lib.descriptors.load_head_sessions", side_effect=AssertionError("unexpected model download"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preview_and_apply_validate_contract_without_changing_source(self) -> None:
        original = self.source.read_bytes()
        preview = build_player_export(self.source, self.output)
        self.assertTrue(preview["dry_run"])
        self.assertTrue(preview["validated"])
        self.assertEqual((preview["track_count"], preview["excluded_count"], preview["dims"]), (2, 2, 410))
        self.assertFalse(self.output.parent.exists())
        self.assertEqual(self.source.read_bytes(), original)

        self.output.parent.mkdir()
        self.output.write_bytes(b"previous export")
        if os.name == "posix":
            self.output.chmod(0o640)
        old_state = output_file_state(self.output)
        preview = build_player_export(
            self.source, self.output, expected_output_state=old_state, check_output_state=True,
        )
        self.assertEqual(self.output.read_bytes(), b"previous export")
        result = build_player_export(
            self.source, self.output, dry_run=False,
            expected_source_state=source_file_state(self.source),
            expected_output_state=old_state, check_output_state=True,
        )
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["output_state"], output_file_state(self.output))
        self.assertEqual(self.source.read_bytes(), original)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o640)
        with gzip.open(self.output, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        self.assertEqual(validate_player_export(data)["track_count"], 2)
        tracks = {track["audio_hash"]: track for track in data["tracks"]}
        self.assertEqual(tracks["a1:one"]["artist"], "Artist")
        self.assertTrue(tracks["a1:two"]["truncated"])
        self.assertEqual(list(self.output.parent.glob(".player-export-*")), [])

    def test_plain_json_and_empty_export(self) -> None:
        pd.DataFrame(columns=["audio_hash"]).to_pickle(self.source)
        output = self.root / "empty.json"
        result = build_player_export(self.source, output, dry_run=False)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual((result["track_count"], result["dims"]), (0, 0))
        self.assertEqual((payload["track_count"], payload["dims"]), (0, 0))

    def test_learned_descriptors_are_computed_before_validation(self) -> None:
        frame = _features()
        frame["audio_embedding"] = [np.zeros(LATENT_DIM, dtype=np.float32) for _ in range(len(frame))]
        frame.to_pickle(self.source)
        runners = {
            spec.field: (
                (lambda matrix: np.tile([0.75, 0.25], (len(matrix), 1)))
                if spec.positive else (lambda matrix: np.full((len(matrix), 1), 0.42))
            )
            for spec in descriptors.HEADS
        }
        with patch("lib.descriptors.load_head_sessions", return_value=runners):
            build_player_export(self.source, self.output, dry_run=False)
        with gzip.open(self.output, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        self.assertIsNotNone(data["tracks"][0]["happy"])
        self.assertEqual(data["tracks"][0]["approachability"], 0.42)

    def test_contract_rejects_bad_metadata_tracks_and_nonfinite_vectors(self) -> None:
        build_player_export(self.source, self.output, dry_run=False)
        with gzip.open(self.output, "rt", encoding="utf-8") as stream:
            payload = json.load(stream)
        changes = [
            lambda value: value.update(model_version="wrong"),
            lambda value: value.update(created_at="not-a-date"),
            lambda value: value.update(descriptor_scale="probability"),
            lambda value: value.update(track_count=99),
            lambda value: value.update(excluded_count=-1),
            lambda value: value.update(dims=409),
            lambda value: value["weights"].update(embedding=float("inf")),
            lambda value: value["tracks"][0].update(audio_hash=" "),
            lambda value: value["tracks"][1].update(audio_hash=value["tracks"][0]["audio_hash"]),
            lambda value: value["tracks"][0]["vector"].__setitem__(0, float("nan")),
            lambda value: value["tracks"][0].pop("key"),
            lambda value: value["tracks"][0].update(energy="invalid"),
            lambda value: value["tracks"][0].update(truncated=False),
        ]
        for change in changes:
            with self.subTest(change=change):
                bad = deepcopy(payload)
                change(bad)
                with self.assertRaises(ValueError):
                    validate_player_export(bad)

    def test_failed_validation_or_publication_preserves_previous_output(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous export")
        with patch.object(VectorBaker, "bake_vector", return_value=np.ones(10, dtype=np.float32)):
            with self.assertRaisesRegex(ValueError, "vector dimension"):
                build_player_export(self.source, self.output, dry_run=False)
        self.assertEqual(self.output.read_bytes(), b"previous export")

        original_replace = Path.replace

        def fail_final_replace(path: Path, target: Path) -> Path:
            if target == self.output:
                raise OSError("simulated publication failure")
            return original_replace(path, target)

        with patch.object(Path, "replace", autospec=True, side_effect=fail_final_replace):
            with self.assertRaisesRegex(OSError, "publication failure"):
                build_player_export(self.source, self.output, dry_run=False)
        self.assertEqual(self.output.read_bytes(), b"previous export")
        self.assertEqual(list(self.output.parent.glob(".player-export-*")), [])

    def test_changed_source_destination_or_extractor_aborts_without_overwriting(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"previous export")
        source_state = source_file_state(self.source)
        output_state = output_file_state(self.output)
        changed = (source_state[0], source_state[1], source_state[2] + 1, source_state[3])
        with patch("pandas.read_pickle", side_effect=AssertionError("unexpected source read")):
            with self.assertRaisesRegex(RuntimeError, "changed after the job was queued"):
                build_player_export(self.source, self.output, expected_source_state=changed)
            with self.assertRaisesRegex(RuntimeError, "destination changed after"):
                build_player_export(self.source, self.output, check_output_state=True, expected_output_state=None)
        self.assertEqual(output_file_state(self.output), output_state)

        def change_source(frame: pd.DataFrame, destination: Path, **kwargs: object) -> dict:
            result = export_features(frame, destination, **kwargs)
            self.source.write_bytes(self.source.read_bytes() + b"changed")
            return result

        with patch("cratedigger.feature_export.export_features", side_effect=change_source):
            with self.assertRaisesRegex(RuntimeError, "Feature store changed during export"):
                build_player_export(self.source, self.output, dry_run=False)
        self.assertEqual(self.output.read_bytes(), b"previous export")
        _features().to_pickle(self.source)

        def change_output(frame: pd.DataFrame, destination: Path, **kwargs: object) -> dict:
            result = export_features(frame, destination, **kwargs)
            self.output.write_bytes(b"external update")
            return result

        with patch("cratedigger.feature_export.export_features", side_effect=change_output):
            with self.assertRaisesRegex(RuntimeError, "destination changed during export"):
                build_player_export(self.source, self.output, dry_run=False)
        self.assertEqual(self.output.read_bytes(), b"external update")
        with self.assertRaisesRegex(RuntimeError, "extraction is running"):
            build_player_export(self.source, self.output, extraction_running=lambda: True)
        running = iter((False, True))
        with self.assertRaisesRegex(RuntimeError, "extraction started during export"):
            build_player_export(self.source, self.output, dry_run=False, extraction_running=lambda: next(running))
        self.assertEqual(self.output.read_bytes(), b"external update")

    def test_rejects_source_overwrite_including_hardlink_and_invalid_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "overwrite"):
            build_player_export(self.source, self.source, dry_run=False)
        hardlink = self.root / "linked.json"
        os.link(self.source, hardlink)
        with self.assertRaisesRegex(ValueError, "overwrite"):
            build_player_export(self.source, hardlink, dry_run=False)
        with self.assertRaisesRegex(ValueError, "end in"):
            build_player_export(self.source, self.root / "bad.txt")

    def test_ui_output_paths_are_bounded_and_symlinks_cannot_escape(self) -> None:
        self.assertEqual(export_output_path(None, self.root), self.root / "features_export.json.gz")
        self.assertEqual(export_output_path("reports/exports/player.json", self.root), self.root / "reports/exports/player.json")
        for value in ("review.json", "../outside.json", "reports/other.json", "reports/exports/bad.txt"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                export_output_path(value, self.root)
        export_dir = self.root / "reports" / "exports"
        export_dir.mkdir(parents=True)
        (export_dir / "escape.json").symlink_to(self.root / "review.json")
        with self.assertRaisesRegex(ValueError, "Export output must"):
            export_output_path("reports/exports/escape.json", self.root)
        other_root = self.root / "other-root"
        (other_root / "reports").mkdir(parents=True)
        (other_root / "reports" / "exports").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(export_output_path(None, other_root), other_root / "features_export.json.gz")
        with self.assertRaisesRegex(ValueError, "exports directory"):
            export_output_path("reports/exports/out.json", other_root)

    def test_cli_defaults_to_preview_and_refuses_a_running_extractor(self) -> None:
        args = ["export_features.py", "--features", str(self.source), "--output", str(self.output)]
        with patch("sys.argv", args), patch("scripts.export_features.external_analysis_running", return_value=False):
            stdout = StringIO()
            with redirect_stdout(stdout):
                export_cli.main()
        result = json.loads(stdout.getvalue().split("@@export-result ", 1)[1])
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.output.exists())
        with patch("sys.argv", args + ["--no-dry-run"]), patch(
            "scripts.export_features.external_analysis_running", return_value=True
        ):
            with self.assertRaisesRegex(RuntimeError, "extraction is running"):
                export_cli.main()
        self.assertFalse(self.output.exists())

    def test_registry_and_job_enforce_preview_and_matching_file_states(self) -> None:
        args = build_argv("validate-config", {}, mode="preview")
        self.assertEqual(args[2:], ["--validate-config"])
        with self.assertRaises(ValueError):
            build_argv("validate-config", {}, mode="apply")
        db = self.root / "state.db"
        kwargs = dict(
            db_path=db, operation_id="recommend-export", raw_params={}, repo_root=REPOSITORY_ROOT,
            logs_dir=self.root / "logs", music_root=self.root / "unused-music", features_path=self.source,
        )
        original_output = output_file_state(REPOSITORY_ROOT / "features_export.json.gz")
        with patch("cratedigger.jobs.external_analysis_running", return_value=False), patch(
            "cratedigger.jobs.subprocess.Popen"
        ) as popen, open_db(db) as conn:
            popen.return_value.pid = 12345
            with self.assertRaisesRegex(ValueError, "successful preview"):
                start_job(conn, mode="apply", **kwargs)
            preview = start_job(conn, mode="preview", **kwargs)
            row = conn.execute("SELECT params_json, argv_json FROM jobs WHERE id=?", (preview,)).fetchone()
            params, argv = json.loads(row["params_json"]), json.loads(row["argv_json"])
            self.assertEqual(params["features"], str(self.source))
            self.assertEqual(params["output"], str(REPOSITORY_ROOT / "features_export.json.gz"))
            self.assertEqual(json.loads(argv[argv.index("--expected-source-state") + 1]), list(source_file_state(self.source)))
            self.assertEqual(
                json.loads(argv[argv.index("--expected-output-state") + 1]),
                list(original_output) if original_output is not None else None,
            )
            conn.execute(
                "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (preview,),
            )
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview,))
            conn.commit()
            changed_output = (1, 2, 3, 4)
            with patch("cratedigger.jobs.output_file_state", return_value=changed_output):
                with self.assertRaisesRegex(ValueError, "successful preview"):
                    start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)
            applied = start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)
            apply_argv = json.loads(conn.execute("SELECT argv_json FROM jobs WHERE id=?", (applied,)).fetchone()[0])
            self.assertIn("--no-dry-run", apply_argv)
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (applied,))
            conn.execute("DELETE FROM jobs WHERE id=?", (applied,))
            conn.commit()
            self.source.write_bytes(self.source.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "successful preview"):
                start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)
        self.assertEqual(output_file_state(REPOSITORY_ROOT / "features_export.json.gz"), original_output)


class ExportWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.source = self.root / "features.pkl"
        self.output = self.root / "features_export.json.gz"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.db = self.root / "state.db"
        _features().to_pickle(self.source)
        self.summary = build_player_export(self.source, self.output, dry_run=False)
        self.log = self.logs / "job.log"
        self.log.write_text("@@export-result " + json.dumps(self.summary) + "\n", encoding="utf-8")
        with open_db(self.db) as conn:
            conn.execute(
                "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, status, "
                "exit_code, log_path, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "export-job", "recommend-export",
                    json.dumps({"features": str(self.source), "output": str(self.output)}),
                    "apply", "fingerprint", json.dumps(["python", "--expected-source-state", json.dumps(self.summary["source_state"])]),
                    "[]", "succeeded", 0, str(self.log), "2026-01-01", "2026-01-02",
                ),
            )
            conn.commit()
        self.app = create_app(
            db_path=self.db, music_root=self.music, features_path=self.source, logs_dir=self.logs,
        )
        self.app.state.repo_root = self.root
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")

    def test_status_and_download_do_not_read_feature_pickle(self) -> None:
        with patch("pandas.read_pickle", side_effect=AssertionError("page loaded the feature store")):
            page = self.client.get("/ops")
            self.assertIn("The validated output and its feature store have not changed", page.text)
            self.assertIn("2 tracks exported; 2 excluded", page.text)
            detail = self.client.get("/jobs/export-job")
            self.assertIn("Download current export", detail.text)
            response = self.client.get("/jobs/export-job/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(response.content, self.output.read_bytes())

    def test_source_and_output_changes_are_visible(self) -> None:
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        self.assertIn("feature store changed since this export", self.client.get("/ops").text)
        self.output.write_bytes(b"external replacement")
        detail = self.client.get("/jobs/export-job")
        self.assertIn("output changed since this job", detail.text)
        self.assertIn("current contents have not been validated", detail.text)

    def test_missing_or_untrusted_artifacts_are_not_downloaded(self) -> None:
        self.output.unlink()
        self.assertIn("published output is missing", self.client.get("/ops").text)
        self.assertEqual(self.client.get("/jobs/export-job/export").status_code, 404)
        os.link(self.source, self.output)
        self.assertIn("export destination is the feature store", self.client.get("/ops").text)
        self.assertEqual(self.client.get("/jobs/export-job/export").status_code, 404)
        self.output.unlink()
        self.output.symlink_to(self.root / "review.json")
        self.assertEqual(self.client.get("/jobs/export-job/export").status_code, 404)

    def test_missing_or_corrupt_job_summary_is_explicit(self) -> None:
        self.log.write_text("not an export result\n", encoding="utf-8")
        self.assertIn("Validated export result is missing", self.client.get("/ops").text)
        self.log.write_text("@@export-result not-json\n", encoding="utf-8")
        self.assertIn("Cannot verify export freshness", self.client.get("/ops").text)

    def test_changed_ui_source_or_recipe_is_not_reported_current(self) -> None:
        self.app.state.features_path = self.root / "different.pkl"
        self.assertIn("different feature store", self.client.get("/ops").text)
        self.app.state.features_path = self.source
        self.summary["model_version"] = "old-recipe"
        self.log.write_text("@@export-result " + json.dumps(self.summary) + "\n", encoding="utf-8")
        self.assertIn("player export recipe changed", self.client.get("/ops").text)

    def test_status_reads_a_bounded_log_tail_at_a_utf8_boundary(self) -> None:
        self.log.write_text("π" * 40000 + "\n@@export-result " + json.dumps(self.summary) + "\n", encoding="utf-8")
        self.assertIn("validated output and its feature store have not changed", self.client.get("/ops").text)


if __name__ == "__main__":
    unittest.main()

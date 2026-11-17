"""Tests for the isolated feature analytics snapshot and Insights page."""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import pandas as pd

from cratedigger.analytics import build_snapshot, source_file_state
from cratedigger.db import open_db
from cratedigger.ops.registry import REPOSITORY_ROOT
from cratedigger.jobs import start_job
from cratedigger.web.app import create_app
from cratedigger.web.insights import load_insights
from scripts import build_analytics


def _features() -> pd.DataFrame:
    return pd.DataFrame([
        {
            "audio_hash": "a", "path": "/music/0compilations/List/a.mp3", "artist_tag": "Compilation",
            "album_tag": "List", "year_tag": "1990", "genre_tag": "Other", "error": None,
        },
        {
            "audio_hash": "a", "path": "/music/A/Artist/Album/a.FLAC", "artist_tag": "Artist",
            "album_tag": "Album", "title_tag": "First song", "track_tag": "1", "year_tag": "1990",
            "genre_tag": "Rock / Alternative", "duration_ms": 3_600_000, "size": 1_073_741_824,
            "schema_version": 12, "error": None,
        },
        {
            "audio_hash": "b", "path": "/music/A/Artist/Album/b.mp3", "artist_tag": "artist",
            "album_tag": "album", "track_tag": "2", "year_tag": "1990", "genre_tag": "Jazz / Bop",
            "duration_ms": 180_000, "size": 1024, "schema_version": 11, "error": None,
        },
        {
            "audio_hash": "c", "path": "/music/E/Elsewhere/c.ogg", "artist_tag": "Elsewhere",
            "album_tag": "Else", "track_tag": "3", "year_tag": "1992-10-01",
            "genre_tag": "Electronic / Ambient", "schema_version": 10, "error": "decode failed",
        },
        {
            "audio_hash": "d", "path": "/music/A/Artist/d.mp3", "artist_tag": "Artist",
            "year_tag": "1992", "genre_tag": "Rock / Alternative", "schema_version": 12, "error": None,
        },
        {
            "audio_hash": "e", "path": "/music/A/Artist/e.mp3", "year_tag": "2100",
            "genre_tag": None, "schema_version": None, "error": None,
        },
        {"audio_hash": None, "path": "/music/missing.mp3"},
        {"audio_hash": " ", "path": "/music/blank.mp3"},
    ])


class AnalyticsSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "features.pkl"
        self.output = self.root / "analytics" / "snapshot.sqlite3"
        _features().to_pickle(self.source)

    def test_preview_and_atomic_publication_are_deduplicated_and_leave_source_unchanged(self) -> None:
        original = self.source.read_bytes()
        preview = build_snapshot(self.source, self.output)
        self.assertEqual(
            {key: preview[key] for key in ("source_rows", "unique_tracks", "duplicate_rows", "unhashed_rows")},
            {"source_rows": 8, "unique_tracks": 5, "duplicate_rows": 1, "unhashed_rows": 2},
        )
        self.assertTrue(preview["dry_run"])
        self.assertFalse(self.output.parent.exists())

        published = build_snapshot(self.source, self.output, dry_run=False)
        self.assertFalse(published["dry_run"])
        self.assertEqual(self.source.read_bytes(), original)
        with closing(sqlite3.connect(self.output)) as conn:
            row = conn.execute(
                "SELECT path, title, release_year, genre_tier1, genre_tier2, file_format "
                "FROM tracks WHERE audio_hash='a'"
            ).fetchone()
            self.assertEqual(row, (
                "/music/A/Artist/Album/a.FLAC", "First song", 1990,
                "Rock", "Rock / Alternative", "flac",
            ))
            self.assertIsNone(conn.execute("SELECT title FROM tracks WHERE audio_hash='b'").fetchone()[0])
            self.assertIsNone(conn.execute("SELECT release_year FROM tracks WHERE audio_hash='e'").fetchone()[0])
        self.assertEqual(list(self.output.parent.glob(".analytics-*")), [])

    def test_aggregates_and_genre_timeline(self) -> None:
        build_snapshot(self.source, self.output, dry_run=False)
        data = load_insights(self.output, tier=1, top_n=1)
        assert data is not None
        self.assertEqual((data["tracks"], data["artists"], data["albums"]), (5, 2, 2))
        self.assertEqual((data["duration_ms"], data["duration_tracks"]), (3_780_000, 2))
        self.assertEqual((data["bytes"], data["sized_tracks"]), (1_073_742_848, 2))
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["undated_tracks"], 1)
        self.assertEqual(data["timeline"]["dated_tracks"], 4)
        self.assertEqual([item["label"] for item in data["timeline"]["legend"]], ["Rock", "Other genres"])
        years = data["timeline"]["years"]
        self.assertEqual([item["year"] for item in years], [1990, 1991, 1992])
        self.assertEqual([item["total"] for item in years], [2, 0, 2])
        self.assertEqual([item["percent"] for item in years[0]["segments"]], [50.0, 50.0])
        self.assertEqual([item["percent"] for item in years[2]["segments"]], [50.0, 50.0])
        self.assertEqual(years[1]["description"], "1991: no dated tracks")
        versions = {item["version"]: (item["count"], item["errors"]) for item in data["schemas"]}
        self.assertEqual(versions, {12: (2, 0), 11: (1, 0), 10: (1, 1), None: (1, 0)})
        tier2 = load_insights(self.output, tier=2, top_n=1)
        assert tier2 is not None
        self.assertEqual(tier2["timeline"]["legend"][0]["label"], "Rock / Alternative")

    def test_empty_snapshot_and_bad_options(self) -> None:
        pd.DataFrame(columns=["audio_hash"]).to_pickle(self.source)
        build_snapshot(self.source, self.output, dry_run=False)
        data = load_insights(self.output)
        assert data is not None
        self.assertEqual(data["tracks"], 0)
        self.assertEqual(data["timeline"]["years"], [])
        for tier, top_n in ((0, 6), (3, 6), (1, 0), (1, 11)):
            with self.subTest(tier=tier, top_n=top_n), self.assertRaises(ValueError):
                load_insights(self.output, tier=tier, top_n=top_n)

    def test_grouped_genre_tiers_combine_subgenres_before_calculating_percentages(self) -> None:
        frame = pd.concat([_features(), pd.DataFrame([{
            "audio_hash": "f", "path": "/music/A/Artist/f.mp3",
            "year_tag": "1990", "genre_tag": "Rock / Alternative / Shoegaze",
        }])], ignore_index=True)
        frame.to_pickle(self.source)
        build_snapshot(self.source, self.output, dry_run=False)
        for tier, label in ((1, "Rock"), (2, "Rock / Alternative")):
            data = load_insights(self.output, tier=tier, top_n=1)
            assert data is not None
            year = data["timeline"]["years"][0]
            self.assertEqual(year["total"], 3)
            self.assertEqual(year["segments"][0]["label"], label)
            self.assertEqual(year["segments"][0]["count"], 2)
            self.assertAlmostEqual(year["segments"][0]["percent"], 66.667, places=3)
            self.assertAlmostEqual(sum(segment["percent"] for segment in year["segments"]), 100.0, places=2)

    def test_source_change_never_replaces_existing_snapshot(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"old snapshot")
        signature = source_file_state(self.source)
        changed = (signature[0], signature[1], signature[2] + 1, signature[3] + 1)
        for states in ([signature, changed], [signature, signature, changed]):
            with self.subTest(states=states), patch("cratedigger.analytics.source_file_state", side_effect=states):
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    build_snapshot(self.source, self.output, dry_run=False)
            self.assertEqual(self.output.read_bytes(), b"old snapshot")
            self.assertEqual(list(self.output.parent.glob(".analytics-*")), [])

    def test_source_change_between_queue_and_execution_is_rejected_before_reading(self) -> None:
        signature = source_file_state(self.source)
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        with patch("pandas.read_pickle", side_effect=AssertionError("unexpected source read")):
            with self.assertRaisesRegex(RuntimeError, "changed after the job was queued"):
                build_snapshot(self.source, self.output, dry_run=False, expected_source_state=signature)
        self.assertFalse(self.output.exists())

    def test_failed_replace_and_invalid_source_preserve_previous_snapshot(self) -> None:
        self.output.parent.mkdir()
        self.output.write_bytes(b"old snapshot")
        with patch("cratedigger.analytics.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                build_snapshot(self.source, self.output, dry_run=False)
        self.assertEqual(self.output.read_bytes(), b"old snapshot")
        self.assertEqual(list(self.output.parent.glob(".analytics-*")), [])
        with self.assertRaisesRegex(ValueError, "overwrite"):
            build_snapshot(self.source, self.source, dry_run=False)
        pd.DataFrame([{"path": "/music/a.mp3"}]).to_pickle(self.source)
        with self.assertRaisesRegex(ValueError, "audio_hash"):
            build_snapshot(self.source, self.output, dry_run=False)
        self.assertEqual(self.output.read_bytes(), b"old snapshot")

    def test_cli_guards_extraction_and_defaults_to_preview(self) -> None:
        args = ["build_analytics.py", "--features", str(self.source), "--output", str(self.output)]
        with patch("sys.argv", args), patch("scripts.build_analytics.external_analysis_running", return_value=True):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as exc:
                build_analytics.main()
            self.assertEqual(exc.exception.code, 2)
        self.assertFalse(self.output.exists())
        with patch("sys.argv", args), patch("scripts.build_analytics.external_analysis_running", return_value=False):
            stdout = StringIO()
            with redirect_stdout(stdout):
                build_analytics.main()
        self.assertTrue(json.loads(stdout.getvalue())["dry_run"])
        self.assertFalse(self.output.exists())
        with patch("sys.argv", args + ["--no-dry-run"]), patch(
            "scripts.build_analytics.external_analysis_running", return_value=False
        ), redirect_stdout(StringIO()):
            build_analytics.main()
        self.assertTrue(self.output.is_file())
        with patch("sys.argv", args + ["--dry-run", "--no-dry-run"]), redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as exc:
                build_analytics.main()
        self.assertEqual(exc.exception.code, 2)

    def test_snapshot_job_tracks_input_and_destination_for_preview_apply(self) -> None:
        db_path = self.root / "state.db"
        kwargs = dict(
            db_path=db_path, operation_id="analytics-snapshot", raw_params={}, repo_root=REPOSITORY_ROOT,
            logs_dir=self.root / "logs", music_root=self.root / "nonexistent-music",
            features_path=self.source, analytics_path=self.output,
        )
        with patch("cratedigger.jobs.external_analysis_running", return_value=False), patch(
            "cratedigger.jobs.subprocess.Popen"
        ) as popen, open_db(db_path) as conn:
            popen.return_value.pid = 12345
            preview = start_job(conn, mode="preview", **kwargs)
            row = conn.execute("SELECT params_json, argv_json FROM jobs WHERE id=?", (preview,)).fetchone()
            self.assertEqual(json.loads(row["params_json"])["features"], str(self.source))
            argv = json.loads(row["argv_json"])
            self.assertEqual(argv[argv.index("--features") + 1], str(self.source))
            self.assertEqual(argv[argv.index("--output") + 1], str(self.output))
            self.assertEqual(
                json.loads(argv[argv.index("--expected-source-state") + 1]),
                list(source_file_state(self.source)),
            )
            conn.execute(
                "UPDATE jobs SET status='succeeded', finished_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (preview,),
            )
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview,))
            conn.commit()
            alternate = {**kwargs, "analytics_path": self.root / "other.sqlite3"}
            with self.assertRaisesRegex(ValueError, "successful preview"):
                start_job(conn, mode="apply", expected_preview_id=preview, **alternate)
            applied = start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)
            argv = json.loads(conn.execute("SELECT argv_json FROM jobs WHERE id=?", (applied,)).fetchone()[0])
            self.assertIn("--no-dry-run", argv)
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (applied,))
            conn.execute("DELETE FROM jobs WHERE id=?", (applied,))
            conn.commit()
            self.source.write_bytes(self.source.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "successful preview"):
                start_job(conn, mode="apply", expected_preview_id=preview, **kwargs)


class InsightsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.source = self.root / "features.pkl"
        self.output = self.root / "analytics.sqlite3"
        _features().to_pickle(self.source)
        self.client = TestClient(create_app(
            db_path=self.root / "state.db", music_root=self.music,
            features_path=self.source, analytics_path=self.output, logs_dir=self.root / "logs",
        ), base_url="http://127.0.0.1:8765")

    def test_missing_and_corrupt_snapshots_are_explicit(self) -> None:
        with patch("pandas.read_pickle", side_effect=AssertionError("page loaded the feature store")):
            missing = self.client.get("/insights")
            self.assertEqual(missing.status_code, 200)
            self.assertIn("No analytics snapshot yet", missing.text)
            self.output.write_bytes(b"not sqlite")
            bad = self.client.get("/insights")
            self.assertEqual(bad.status_code, 503)
            self.assertIn("Cannot read analytics snapshot", bad.text)
        self.assertEqual(self.client.get("/insights?tier=3").status_code, 422)

    def test_dashboard_renders_only_snapshot_data_and_chart_controls(self) -> None:
        build_snapshot(self.source, self.output, dry_run=False)
        with patch("pandas.read_pickle", side_effect=AssertionError("page loaded the feature store")):
            response = self.client.get("/insights?tier=2&top_n=1")
        self.assertEqual(response.status_code, 200)
        for text in (
            "Library insights", "Feature schema coverage", "v12", "Other genres",
            "Rock / Alternative", "1991: no dated tracks", "50.0%",
            "1 tracks have an unknown or out-of-range release year",
        ):
            self.assertIn(text, response.text)
        self.assertNotIn("First song", response.text)
        self.assertIn('href="/ops#op-analytics-snapshot"', response.text)
        self.assertEqual(self.client.get("/insights?top_n=11").status_code, 422)

    def test_snapshot_operation_uses_the_app_paths(self) -> None:
        self.client.get("/")
        with patch("cratedigger.web.app.start_job", return_value="snapshot-job") as start:
            response = self.client.post(
                "/ops/analytics-snapshot/start",
                data={"csrf_token": self.client.cookies["cratedigger_session"], "mode": "preview"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(start.call_args.kwargs["features_path"], self.source)
        self.assertEqual(start.call_args.kwargs["analytics_path"], self.output)

    def test_metadata_corruption_is_reported_instead_of_rendered(self) -> None:
        build_snapshot(self.source, self.output, dry_run=False)
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE metadata SET value='\"wrong\"' WHERE key='source_rows'")
            conn.commit()
        response = self.client.get("/insights")
        self.assertEqual(response.status_code, 503)
        self.assertIn("invalid count", response.text)


if __name__ == "__main__":
    unittest.main()

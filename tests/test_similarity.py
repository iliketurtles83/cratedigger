"""Snapshot-only similarity, vector compatibility, and audio preview tests."""

from __future__ import annotations

from contextlib import closing, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd

from cratedigger.analytics import SIMILARITY_DIMS, build_snapshot, source_file_state
from cratedigger.db import open_db
from cratedigger.web import similarity as similarity_module
from cratedigger.web.app import create_app
from cratedigger.web.insights import load_insights
from cratedigger.web.similarity import SimilarityCache
from lib.descriptors import HEADS
from lib.embeddings import LATENT_DIM
from lib.recommender import export_features
from scripts import build_analytics


def _runners():
    runners = {}
    for i, spec in enumerate(HEADS):
        def run(matrix, i=i, spec=spec):
            values = matrix[:, i].astype(np.float64)
            if spec.positive is None:
                return values[:, None]
            probabilities = 1.0 / (1.0 + np.exp(-values))
            positive = spec.classes.index(spec.positive)
            result = np.empty((len(matrix), 2))
            result[:, positive] = probabilities
            result[:, 1 - positive] = 1.0 - probabilities
            return result
        runners[spec.field] = run
    return runners


def _fixture(root: Path) -> pd.DataFrame:
    music = root / "music"
    album = music / "A" / "Test Artist" / "Album"
    album.mkdir(parents=True)
    for filename in ("seed.mp3", "close.mp3"):
        (album / filename).write_bytes(b"0123456789")
    outside = root / "outside.mp3"
    outside.write_bytes(b"outside")
    (album / "escape.mp3").symlink_to(outside)

    seed = np.zeros(400, dtype=np.float32)
    seed[0] = 1
    close = seed.copy()
    close[1] = 0.05
    far = np.zeros(400, dtype=np.float32)
    far[1] = 1
    nonfinite = seed.copy()
    nonfinite[0] = np.nan

    def row(audio_hash, title, embedding, filename, **extra):
        return {
            "audio_hash": audio_hash,
            "path": str(album / filename),
            "artist_tag": "Test Artist",
            "album_tag": "Album",
            "title_tag": title,
            "genre_tag": "Rock / Alternative",
            "embedding": embedding,
            "audio_embedding": np.full(LATENT_DIM, extra.pop("latent", 0.0), dtype=np.float32),
            "bpm": 120.0,
            "key": "C",
            "mode": "major",
            "decode_coverage": 1.0,
            "signal_status": "ok",
            "error": None,
            **extra,
        }

    records = [
        row("seed", "Seed & Song", seed, "seed.mp3", latent=0.1),
        row("close", "Close <script>alert(1)</script>", close, "close.mp3", latent=0.1),
        row("far", "Far Away", far, "escape.mp3", latent=-1.0),
        row("boundary", "Boundary", seed, "missing.mp3", bpm=90.0, decode_coverage=0.5,
            signal_status="truncated", latent=1.0),
        row("silent", "Silent", seed, "silent.mp3", signal_status="silent"),
        row("short", "Short", seed, "short.mp3", decode_coverage=0.49),
        row("malformed", "Malformed", np.ones(399, dtype=np.float32), "malformed.mp3"),
        row("missing", "Missing", None, "missing-feature.mp3"),
        row("nonfinite", "Nonfinite", nonfinite, "nonfinite.mp3"),
        row("seed", "Duplicate", seed, "duplicate.mp3", path=str(music / "0compilations" / "duplicate.mp3")),
        row(None, "No hash", seed, "nohash.mp3"),
    ]
    return pd.DataFrame(records)


class SimilarityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.frame = _fixture(self.root)
        self.source = self.root / "features.pkl"
        self.output = self.root / "analytics.sqlite3"
        self.frame.to_pickle(self.source)
        self.runners = _runners()

    def build(self, output: Path | None = None) -> dict:
        with patch("lib.descriptors.load_head_sessions", return_value=self.runners):
            return build_snapshot(self.source, output or self.output, dry_run=False)

    def client(self) -> TestClient:
        return TestClient(create_app(
            db_path=self.root / "state.db",
            music_root=self.music,
            features_path=self.source,
            analytics_path=self.output,
            logs_dir=self.root / "logs",
        ), base_url="http://127.0.0.1:8765")

    def test_preview_counts_eligibility_without_running_heads_or_writing(self) -> None:
        source = self.source.read_bytes()
        with patch("lib.descriptors.load_head_sessions", side_effect=AssertionError("unexpected inference")):
            result = build_snapshot(self.source, self.output)
        self.assertEqual(
            {key: result[key] for key in (
                "source_rows", "unique_tracks", "duplicate_rows", "unhashed_rows",
                "similarity_tracks", "similarity_excluded", "similarity_invalid",
            )},
            {
                "source_rows": 11, "unique_tracks": 9, "duplicate_rows": 1, "unhashed_rows": 1,
                "similarity_tracks": 4, "similarity_excluded": 5, "similarity_invalid": 1,
            },
        )
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), source)

    def test_snapshot_vectors_and_descriptors_match_the_player_export(self) -> None:
        original = self.source.read_bytes()
        result = self.build()
        self.assertEqual(result["similarity_tracks"], 4)
        self.assertEqual(self.source.read_bytes(), original)
        export = self.root / "export.json"
        export_features(self.frame.drop(index=6), export, descriptor_runners=self.runners)
        payload = json.loads(export.read_text())
        self.assertEqual(payload["track_count"], 4)
        self.assertEqual(payload["dims"], SIMILARITY_DIMS)
        with closing(sqlite3.connect(self.output)) as conn:
            rows = conn.execute("SELECT audio_hash, vector, energy, valence FROM similarity_vectors").fetchall()
        snapshot = {key: (np.frombuffer(blob, dtype="<f4"), energy, valence) for key, blob, energy, valence in rows}
        self.assertEqual(set(snapshot), {item["audio_hash"] for item in payload["tracks"]})
        for item in payload["tracks"]:
            vector, energy, valence = snapshot[item["audio_hash"]]
            np.testing.assert_allclose(vector, item["vector"], atol=5.1e-6, rtol=0)
            self.assertAlmostEqual(float(np.linalg.norm(vector)), 1.0, places=5)
            self.assertAlmostEqual(energy, item["energy"], delta=0.00051)
            self.assertAlmostEqual(valence, item["valence"], delta=0.00051)

    def test_inference_failure_does_not_replace_snapshot(self) -> None:
        self.output.write_bytes(b"existing snapshot")
        with patch("cratedigger.analytics.attach_descriptors", side_effect=RuntimeError("model failed")):
            with self.assertRaisesRegex(RuntimeError, "model failed"):
                build_snapshot(self.source, self.output, dry_run=False)
        self.assertEqual(self.output.read_bytes(), b"existing snapshot")
        self.assertEqual(list(self.root.glob(".analytics-*")), [])

    def test_missing_inference_dependency_is_reported_without_publication(self) -> None:
        missing = ModuleNotFoundError("No module named onnxruntime", name="onnxruntime")
        with patch("cratedigger.analytics.attach_descriptors", side_effect=missing):
            with self.assertRaisesRegex(RuntimeError, "requires onnxruntime"):
                build_snapshot(self.source, self.output, dry_run=False)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".analytics-*")), [])

    def test_unrepresentable_embedding_is_excluded(self) -> None:
        self.frame.at[6, "embedding"] = np.full(400, 1e38, dtype=np.float32)
        self.frame.to_pickle(self.source)
        with np.errstate(over="ignore"):
            result = build_snapshot(self.source, self.output)
        self.assertEqual((result["similarity_tracks"], result["similarity_invalid"]), (4, 1))

    def test_snapshot_search_and_neighbors(self) -> None:
        self.build()
        with patch("pandas.read_pickle", side_effect=AssertionError("request read the feature store")):
            index = SimilarityCache().get(self.output)
        assert index is not None
        hits, total = index.search("artist close")
        self.assertEqual((total, [item.audio_hash for item in hits]), (1, ["close"]))
        self.assertEqual(index.search(""), ([], 0))
        seed = index.by_hash["seed"]
        neighbors = index.neighbors(seed)
        self.assertEqual(neighbors[0][0].audio_hash, "close")
        self.assertEqual({track.audio_hash for track, _ in neighbors}, {"close", "boundary", "far"})
        self.assertTrue(all(-1 <= score <= 1 for _, score in neighbors))
        self.assertGreater(neighbors[0][1], neighbors[-1][1])
        self.assertEqual(index.neighbors(index.by_hash["silent"]), [])
        self.assertFalse(index.vectors.flags.writeable)
        with self.assertRaisesRegex(ValueError, "limit"):
            index.neighbors(seed, limit=201)
        self.assertEqual(len(index.neighbors(seed, limit=42)), 3)

    def test_cache_invalidates_when_snapshot_is_atomically_replaced(self) -> None:
        self.build()
        cache = SimilarityCache()
        first = cache.get(self.output)
        assert first is not None
        self.assertIs(cache.get(self.output), first)
        self.frame.loc[0, "title_tag"] = "New & Song"
        self.frame.to_pickle(self.source)
        replacement = self.root / "replacement.sqlite3"
        self.build(replacement)
        old_stat = self.output.stat()
        os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        os.replace(replacement, self.output)
        second = cache.get(self.output)
        assert second is not None
        self.assertIsNot(first, second)
        self.assertEqual(second.by_hash["seed"].title, "New & Song")
        self.assertNotEqual(first.by_hash["seed"].title, second.by_hash["seed"].title)

    def test_cache_retries_if_snapshot_changes_during_loading(self) -> None:
        self.build()
        identity = source_file_state(self.output)
        changed = (identity[0], identity[1] + 1, identity[2], identity[3])
        with patch("cratedigger.web.similarity.source_file_state", side_effect=[identity, changed, identity, identity]), \
                patch("cratedigger.web.similarity._load_index", wraps=similarity_module._load_index) as load:
            self.assertIsNotNone(SimilarityCache().get(self.output))
        self.assertEqual(load.call_count, 2)

    def test_corrupt_vectors_counts_and_memory_limits_fail_explicitly(self) -> None:
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE similarity_vectors SET vector=? WHERE audio_hash='seed'", (b"bad",))
            conn.commit()
        with self.assertRaisesRegex(ValueError, "invalid similarity vector"):
            SimilarityCache().get(self.output)
        self.build()
        invalid = np.zeros(SIMILARITY_DIMS, dtype="<f4")
        invalid[0] = np.nan
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE similarity_vectors SET vector=? WHERE audio_hash='seed'", (invalid.tobytes(),))
            conn.commit()
        with self.assertRaisesRegex(ValueError, "non-finite or unnormalized"):
            SimilarityCache().get(self.output)
        self.build()
        with patch("cratedigger.web.similarity.MAX_VECTOR_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "in-memory limit"):
                SimilarityCache().get(self.output)
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("DELETE FROM similarity_vectors WHERE audio_hash='seed'")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "counts do not match"):
            SimilarityCache().get(self.output)

    def test_old_snapshots_keep_dashboard_but_ask_to_rebuild_explorer(self) -> None:
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE metadata SET value='2' WHERE key='snapshot_version'")
            conn.execute("DROP TABLE similarity_vectors")
            conn.commit()
        data = load_insights(self.output)
        assert data is not None
        self.assertEqual(data["tracks"], 9)
        client = self.client()
        self.assertEqual(client.get("/insights").status_code, 200)
        response = client.get("/insights/similarity")
        self.assertEqual(response.status_code, 200)
        self.assertIn("older snapshot does not contain similarity vectors", response.text)

    def test_pages_search_rank_and_handle_unavailable_audio(self) -> None:
        self.build()
        client = self.client()
        with patch("pandas.read_pickle", side_effect=AssertionError("page loaded the feature store")):
            response = client.get("/insights/similarity", params={"q": "artist close"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("1 matching track", response.text)
            self.assertIn("Close &lt;script&gt;alert(1)&lt;/script&gt;", response.text)
            self.assertNotIn("<script>alert(1)</script>", response.text)
            response = client.get("/insights/similarity", params={"seed": "seed"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("More like this", response.text)
        self.assertIn("Cosine", response.text)
        self.assertIn("Audio path is outside the configured library", response.text)
        self.assertIn("Audio file is unavailable", response.text)
        self.assertIn('preload="none"', response.text)
        self.assertIn("/media/audio?path=", response.text)
        self.assertIn("current", response.text)
        self.assertEqual(client.get("/insights/similarity", params={"q": "x" * 201}).status_code, 422)

    def test_library_link_opens_the_corresponding_snapshot_track(self) -> None:
        self.build()
        db = self.root / "state.db"
        with open_db(db) as conn:
            conn.execute(
                "INSERT INTO tracks (audio_hash, title, updated_at) VALUES (?, ?, ?)",
                ("seed", "Seed & Song", "2026-01-01"),
            )
            conn.commit()
        client = self.client()
        library = client.get("/library")
        self.assertIn('href="/insights/similarity?seed=seed"', library.text)
        explorer = client.get("/insights/similarity", params={"seed": "seed"})
        self.assertIn("Seed &amp; Song", explorer.text)
        self.assertIn("More like this", explorer.text)

    def test_search_reports_truncated_results_and_older_recipe(self) -> None:
        first = self.frame.iloc[0].to_dict()
        bulk = [{**first, "audio_hash": f"bulk-{i}", "title_tag": f"Bulk {i}"} for i in range(35)]
        pd.DataFrame(bulk).to_pickle(self.source)
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute(
                "UPDATE metadata SET value=? WHERE key='similarity_recipe'",
                (json.dumps("old-recipe"),),
            )
            conn.commit()
        response = self.client().get("/insights/similarity", params={"q": "bulk"})
        self.assertIn("35 matching tracks; showing the first 30", response.text)
        self.assertIn("older similarity or descriptor recipe", response.text)

    def test_excluded_unknown_missing_and_corrupt_are_visible(self) -> None:
        client = self.client()
        self.assertIn("No analytics snapshot yet", client.get("/insights/similarity").text)
        self.build()
        response = client.get("/insights/similarity", params={"seed": "silent"})
        self.assertIn("no usable similarity vector", response.text)
        response = client.get("/insights/similarity", params={"seed": "not-present"})
        self.assertIn("This track is not in the snapshot", response.text)
        self.output.write_bytes(b"not sqlite")
        response = client.get("/insights/similarity", params={"seed": "seed"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Cannot read analytics snapshot", response.text)

    def test_source_freshness_detects_replacement_with_same_size_and_timestamp(self) -> None:
        self.build()
        client = self.client()
        self.assertIn("file identity has not changed", client.get("/insights").text)
        stat = self.source.stat()
        replacement = self.root / "replacement.pkl"
        replacement.write_bytes(self.source.read_bytes())
        os.utime(replacement, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(replacement, self.source)
        response = client.get("/insights/similarity")
        self.assertIn("feature store was replaced", response.text)
        self.assertIn("File metadata cannot detect every external change", response.text)

    def test_audio_preview_supports_ranges_and_rejects_outside_paths(self) -> None:
        self.build()
        client = self.client()
        source = self.music / "A" / "Test Artist" / "Album" / "seed.mp3"
        response = client.get("/media/audio", params={"path": str(source)}, headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(client.get("/media/audio", params={"path": str(self.root / "outside.mp3")}).status_code, 403)
        escape = self.music / "A" / "Test Artist" / "Album" / "escape.mp3"
        self.assertEqual(client.get("/media/audio", params={"path": str(escape)}).status_code, 403)
        self.assertEqual(client.get("/media/audio", params={"path": str(source.parent / "absent.mp3")}).status_code, 404)

    def test_standalone_snapshot_refuses_running_backfill(self) -> None:
        args = ["build_analytics.py", "--features", str(self.source), "--output", str(self.output)]
        with patch("sys.argv", args), patch("scripts.build_analytics.external_analysis_running", return_value=False), \
                patch("scripts.build_analytics.external_backfill_running", return_value=True), \
                patch("pandas.read_pickle", side_effect=AssertionError("read while backfill running")):
            stderr = StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as exc:
                build_analytics.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("feature backfill is running", stderr.getvalue())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()

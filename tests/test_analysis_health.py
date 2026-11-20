"""Tests for read-only extraction health evidence."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd

from cratedigger.analytics import build_snapshot
from cratedigger.web.analysis_health import load_analysis_health
from cratedigger.web.app import create_app


def _features() -> pd.DataFrame:
    embedding = np.ones(400, dtype=np.float32)
    common = {
        "artist_tag": "Example Artist",
        "album_tag": "Example Album",
        "decode_coverage": 1.0,
        "signal_status": "ok",
        "error": None,
    }

    def row(audio_hash, title, *, style=embedding, **extra):
        return {
            **common,
            "audio_hash": audio_hash,
            "path": f"/music/A/Artist/{title}.mp3",
            "title_tag": title,
            "embedding": style,
            **extra,
        }

    return pd.DataFrame([
        row("good", "Good"),
        row("error", "Decode error", style=None, error="decode failed <script>alert(1)</script>", signal_status=None),
        row("truncated", "Truncated", signal_status="truncated", decode_coverage=0.75),
        row("silent", "Silent", signal_status="silent"),
        row("missing", "Missing vector", style=None),
        row("overlap", "Overlap", style=None, error="reader failed", signal_status="truncated", decode_coverage=0.4),
    ])


class AnalysisHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.source = self.root / "features.pkl"
        self.output = self.root / "analytics.sqlite3"
        self.frame = _features()
        self.frame.to_pickle(self.source)

    def build(self) -> None:
        build_snapshot(self.source, self.output, dry_run=False)

    def client(self) -> TestClient:
        return TestClient(create_app(
            db_path=self.root / "state.db",
            music_root=self.music,
            features_path=self.source,
            analytics_path=self.output,
            logs_dir=self.root / "logs",
        ), base_url="http://127.0.0.1:8765")

    def test_overlapping_categories_and_search(self) -> None:
        self.build()
        evidence = load_analysis_health(self.output)
        assert evidence is not None
        self.assertEqual(evidence["tracks"], 6)
        self.assertEqual(evidence["counts"], {"error": 2, "truncated": 2, "silent": 1, "excluded": 4, "all": 5})
        self.assertEqual(evidence["total"], 5)
        self.assertEqual({item["audio_hash"] for item in evidence["entries"]}, {"error", "truncated", "silent", "missing", "overlap"})
        self.assertEqual({item["audio_hash"] for item in evidence["entries"][:2]}, {"error", "overlap"})
        for kind, expected in (
            ("error", {"error", "overlap"}),
            ("truncated", {"truncated", "overlap"}),
            ("silent", {"silent"}),
            ("excluded", {"error", "silent", "missing", "overlap"}),
        ):
            result = load_analysis_health(self.output, kind=kind)
            assert result is not None
            self.assertEqual({item["audio_hash"] for item in result["entries"]}, expected)
        result = load_analysis_health(self.output, query="reader FAILED")
        assert result is not None
        self.assertEqual([item["audio_hash"] for item in result["entries"]], ["overlap"])
        result = load_analysis_health(self.output, query="missing")
        assert result is not None
        self.assertEqual([item["audio_hash"] for item in result["entries"]], ["missing"])
        result = load_analysis_health(self.output, query="%")
        assert result is not None
        self.assertEqual(result["total"], 0)
        result = load_analysis_health(self.output, query="' OR 1=1 --")
        assert result is not None
        self.assertEqual(result["total"], 0)

    def test_pagination_is_clamped_and_filters_are_validated(self) -> None:
        self.build()
        first = load_analysis_health(self.output, per_page=2)
        last = load_analysis_health(self.output, page=999, per_page=2)
        assert first is not None and last is not None
        self.assertEqual((first["total"], first["page"], first["pages"], len(first["entries"])), (5, 1, 3, 2))
        self.assertEqual((last["page"], last["pages"], len(last["entries"])), (3, 3, 1))
        for kwargs in ({"kind": "bad"}, {"page": 0}, {"per_page": 0}, {"per_page": 101}, {"query": "x" * 201}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "filters"):
                load_analysis_health(self.output, **kwargs)

    def test_old_snapshots_show_other_evidence_without_similarity_exclusions(self) -> None:
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE metadata SET value='2' WHERE key='snapshot_version'")
            conn.execute("DROP TABLE similarity_vectors")
            conn.commit()
        result = load_analysis_health(self.output)
        assert result is not None
        self.assertFalse(result["has_vectors"])
        self.assertEqual(result["counts"], {"error": 2, "truncated": 2, "silent": 1, "all": 4})
        missing = load_analysis_health(self.output, kind="excluded")
        assert missing is not None
        self.assertTrue(missing["unavailable_filter"])
        self.assertEqual(missing["total"], 0)
        client = self.client()
        self.assertIn("Similarity exclusions unavailable", client.get("/insights/analysis-health").text)
        self.assertIn("does not contain similarity exclusions", client.get("/insights/analysis-health?kind=excluded").text)

    def test_pages_render_and_cross_link_without_reading_feature_pickle(self) -> None:
        self.build()
        original = self.source.read_bytes()
        client = self.client()
        with patch("pandas.read_pickle", side_effect=AssertionError("request read the feature store")):
            response = client.get("/insights/analysis-health", params={"kind": "error"})
            dashboard = client.get("/insights")
            queue = client.get("/health")
            profile = client.get("/insights/profile")
            similarity = client.get("/insights/similarity", params={"seed": "missing"})
        self.assertEqual(response.status_code, 200)
        for text in (
            "Analysis health", "Recorded errors", "Truncated signals", "Silent signals", "No similarity vector",
            "2 matching tracks", "decode failed &lt;script&gt;alert(1)&lt;/script&gt;", "Decode coverage: 40.0%",
            "/ops#op-analyze", "/ops#op-recommend-backfill-embeddings",
        ):
            self.assertIn(text, response.text)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn('href="/insights/analysis-health?kind=error"', dashboard.text)
        self.assertIn('href="/insights/analysis-health"', queue.text)
        self.assertIn('href="/insights/analysis-health"', profile.text)
        self.assertIn('href="/insights/analysis-health?q=missing"', similarity.text)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(client.get("/insights/analysis-health?kind=bad").status_code, 422)
        self.assertEqual(client.get("/insights/analysis-health?page=0").status_code, 422)
        self.assertEqual(client.get("/insights/analysis-health", params={"q": "x" * 201}).status_code, 422)
        self.assertIn("1 matching track", client.get("/insights/analysis-health", params={"q": "reader failed"}).text)

    def test_ui_pagination_preserves_search_and_clamps_page(self) -> None:
        base = self.frame.iloc[4].to_dict()
        extra = [{**base, "audio_hash": f"extra-{index:02}", "title_tag": f"Repeated {index:02}"} for index in range(28)]
        pd.concat([self.frame, pd.DataFrame(extra)], ignore_index=True).to_pickle(self.source)
        self.build()
        client = self.client()
        first = client.get("/insights/analysis-health", params={"kind": "excluded", "q": "Repeated"})
        self.assertIn("28 matching tracks", first.text)
        self.assertIn("Page 1 of 2", first.text)
        self.assertIn("kind=excluded&amp;q=Repeated&amp;page=2", first.text)
        last = client.get("/insights/analysis-health", params={"kind": "excluded", "q": "Repeated", "page": 999})
        self.assertIn("Page 2 of 2", last.text)
        self.assertIn("kind=excluded&amp;q=Repeated&amp;page=1", last.text)

    def test_missing_stale_and_corrupt_snapshots_are_explicit(self) -> None:
        client = self.client()
        response = client.get("/insights/analysis-health")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No analytics snapshot yet", response.text)
        self.build()
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        self.assertIn("feature store changed", client.get("/insights/analysis-health").text)
        self.output.write_bytes(b"not sqlite")
        response = client.get("/insights/analysis-health")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Cannot read analytics snapshot", response.text)

    def test_invalid_coverage_and_similarity_counts_are_reported(self) -> None:
        self.build()
        client = self.client()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE tracks SET decode_coverage=? WHERE audio_hash='truncated'", (float("inf"),))
            conn.commit()
        response = client.get("/insights/analysis-health?kind=truncated")
        self.assertEqual(response.status_code, 503)
        self.assertIn("invalid decode coverage", response.text)
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("DELETE FROM similarity_vectors WHERE audio_hash='good'")
            conn.commit()
        response = client.get("/insights/analysis-health")
        self.assertEqual(response.status_code, 503)
        self.assertIn("similarity counts do not match", response.text)

    def test_empty_snapshot_is_explicit(self) -> None:
        pd.DataFrame(columns=["audio_hash", "embedding"]).to_pickle(self.source)
        self.build()
        result = load_analysis_health(self.output)
        assert result is not None
        self.assertEqual(result["counts"], {"error": 0, "truncated": 0, "silent": 0, "excluded": 0, "all": 0})
        self.assertEqual(result["total"], 0)
        response = self.client().get("/insights/analysis-health")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No snapshot evidence matches", response.text)


if __name__ == "__main__":
    unittest.main()

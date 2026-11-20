"""Tests for snapshot-backed musical feature visualizations."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd

from cratedigger.analytics import build_snapshot
from cratedigger.web.app import create_app
from cratedigger.web.sound_profile import _heatmap_style, _histogram, load_sound_profile


def _features() -> pd.DataFrame:
    bpms = [59.99, 60, 79.99, 80, 100, 120, 140, 160, 180, 200, 0, None, -1]
    loudness = [-30.01, -30, -25, -20, -15, -10, -5, -4, None, None, None, None, None]
    keys = ["C", "C", "G", "F#", "Db", None, "A", "D", "E", "F", "B", "C", "G"]
    modes = ["major", "minor", "MINOR", "", "major", None, "major", "minor",
             "major", "minor", "major", "other", None]
    embedding = np.ones(400, dtype=np.float32)
    return pd.DataFrame([
        {
            "audio_hash": f"track-{index:02}",
            "path": f"/music/A/Artist/track-{index:02}.mp3",
            "embedding": embedding,
            "bpm": bpm,
            "loudness_lufs": lufs,
            "key": key,
            "mode": mode,
        }
        for index, (bpm, lufs, key, mode) in enumerate(zip(bpms, loudness, keys, modes))
    ])


class SoundProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.source = self.root / "features.pkl"
        self.output = self.root / "analytics.sqlite3"
        _features().to_pickle(self.source)

    def build(self) -> None:
        build_snapshot(self.source, self.output, dry_run=False)
        with closing(sqlite3.connect(self.output)) as conn:
            conn.executemany(
                "UPDATE similarity_vectors SET energy=?, valence=? WHERE audio_hash=?",
                [
                    (-100, -100, "track-00"),
                    (0, 0, "track-01"),
                    (100, 100, "track-02"),
                    (2, -2, "track-03"),
                    (None, 0, "track-04"),
                    (0, None, "track-05"),
                ],
            )
            conn.commit()

    def client(self) -> TestClient:
        app = create_app(
            db_path=self.root / "state.db",
            music_root=self.music,
            features_path=self.source,
            analytics_path=self.output,
            logs_dir=self.root / "logs",
        )
        return TestClient(app, base_url="http://127.0.0.1:8765")

    def test_histogram_boundaries_and_coverage(self) -> None:
        self.build()
        profile = load_sound_profile(self.output)
        assert profile is not None
        self.assertEqual(profile["tracks"], 13)
        self.assertEqual((profile["tempo"]["known"], profile["tempo"]["excluded"]), (10, 3))
        self.assertEqual([item["count"] for item in profile["tempo"]["bins"]], [1, 2, 1, 1, 1, 1, 1, 1, 1])
        self.assertEqual([item["percent"] for item in profile["tempo"]["bins"]], [10, 20, 10, 10, 10, 10, 10, 10, 10])
        self.assertEqual([item["width"] for item in profile["tempo"]["bins"]], [50, 100, 50, 50, 50, 50, 50, 50, 50])
        self.assertEqual((profile["loudness"]["known"], profile["loudness"]["excluded"]), (8, 5))
        self.assertEqual([item["count"] for item in profile["loudness"]["bins"]], [1, 1, 1, 1, 1, 1, 2])

    def test_circle_of_fifths_and_unknown_mode_counts(self) -> None:
        self.build()
        profile = load_sound_profile(self.output)
        assert profile is not None
        keys = profile["musical_keys"]
        self.assertEqual((keys["known"], keys["excluded"]), (11, 2))
        self.assertEqual(
            [row["key"] for row in keys["rows"]],
            ["C", "G", "D", "A", "E", "B", "F#", "C#", "G#", "D#", "A#", "F"],
        )
        rows = {row["key"]: row for row in keys["rows"]}
        self.assertEqual(tuple(rows["C"][name] for name in ("major", "minor", "unknown")), (1, 1, 1))
        self.assertEqual(tuple(rows["G"][name] for name in ("major", "minor", "unknown")), (0, 1, 1))
        self.assertAlmostEqual(sum(rows["C"][name] for name in ("major_width", "minor_width", "unknown_width")), 99.99)

    def test_small_nonzero_histogram_percentages_are_not_reported_as_zero(self) -> None:
        histogram = _histogram([1.0] * 1999 + [2.0], 2000, (2,), ("low", "high"))
        self.assertEqual(histogram["bins"][1]["percent_label"], "<0.1%")
        self.assertEqual(histogram["bins"][0]["percent_label"], ">99.9%")

    def test_mood_grid_uses_logits_and_clamps_endpoints(self) -> None:
        self.build()
        profile = load_sound_profile(self.output)
        assert profile is not None
        mood = profile["mood"]
        self.assertTrue(mood["available"])
        self.assertTrue(mood["current_recipe"])
        self.assertEqual((mood["known"], mood["eligible"], mood["missing_from_eligible"], mood["excluded"]), (4, 13, 9, 9))
        rows = mood["rows"]
        self.assertEqual(rows[9]["label"], "0.0-0.1")
        self.assertEqual(rows[9]["cells"][0]["count"], 1)
        self.assertEqual(rows[4]["label"], "0.5-0.6")
        self.assertEqual(rows[4]["cells"][5]["count"], 1)
        self.assertEqual(rows[1]["cells"][1]["count"], 1)
        self.assertEqual(rows[0]["label"], "0.9-1.0")
        self.assertEqual(rows[0]["cells"][9]["count"], 1)
        self.assertEqual(sum(cell["count"] for row in rows for cell in row["cells"]), 4)

    def test_heatmap_text_contrast_meets_wcag_aa(self) -> None:
        def luminance(rgb: tuple[float, float, float]) -> float:
            linear = [
                value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                for value in (component / 255 for component in rgb)
            ]
            return sum(weight * component for weight, component in zip((0.2126, 0.7152, 0.0722), linear))

        for count in range(1001):
            opacity, text_color = _heatmap_style(count, 1000)
            background = tuple(
                opacity * blue + (1 - opacity) * surface
                for blue, surface in zip((93, 169, 255), (23, 26, 34))
            )
            light = luminance(background)
            contrast = (light + 0.05) / 0.05 if text_color == "#000" else 1.05 / (light + 0.05)
            self.assertGreaterEqual(contrast, 4.5, (count, opacity, text_color))

    def test_empty_snapshot_has_zero_coverage_without_division_errors(self) -> None:
        pd.DataFrame(columns=["audio_hash", "embedding"]).to_pickle(self.source)
        build_snapshot(self.source, self.output, dry_run=False)
        profile = load_sound_profile(self.output)
        assert profile is not None
        for section in ("tempo", "loudness", "musical_keys", "mood"):
            self.assertEqual(profile[section]["known"], 0)
            self.assertEqual(profile[section]["excluded"], 0)
        response = self.client().get("/insights/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No usable values", response.text)
        self.assertIn("No tracks have both learned descriptors", response.text)

    def test_old_snapshot_keeps_features_but_explains_missing_mood(self) -> None:
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE metadata SET value='2' WHERE key='snapshot_version'")
            conn.execute("DROP TABLE similarity_vectors")
            conn.commit()
        profile = load_sound_profile(self.output)
        assert profile is not None
        self.assertEqual(profile["tempo"]["known"], 10)
        self.assertFalse(profile["mood"]["available"])
        response = self.client().get("/insights/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn("older snapshot does not contain learned descriptors", response.text)
        self.assertIn("Tempo distribution", response.text)

    def test_profile_and_navigation_render_without_reading_pickle(self) -> None:
        self.build()
        client = self.client()
        with patch("pandas.read_pickle", side_effect=AssertionError("request read the feature store")):
            dashboard = client.get("/insights")
            response = client.get("/insights/profile")
            similarity = client.get("/insights/similarity")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/insights/profile"', dashboard.text)
        self.assertIn('href="/insights/profile"', similarity.text)
        for text in ("Tempo distribution", "Integrated loudness", "Musical keys", "Energy and valence",
                     "10 of 13 tracks available", "4 of 13 tracks have both scores",
                     "Energy 0.9-1.0, valence 0.9-1.0: 1 track",
                     "Energy and valence heatmap, scroll horizontally"):
            self.assertIn(text, response.text)
        self.assertIn("file identity has not changed", response.text)

    def test_missing_stale_and_corrupt_snapshots_are_explicit(self) -> None:
        client = self.client()
        response = client.get("/insights/profile")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No analytics snapshot yet", response.text)
        self.build()
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        self.assertIn("feature store changed", client.get("/insights/profile").text)
        self.output.write_bytes(b"not sqlite")
        response = client.get("/insights/profile")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Cannot read analytics snapshot", response.text)

    def test_corrupt_values_and_counts_do_not_produce_misleading_charts(self) -> None:
        self.build()
        client = self.client()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE tracks SET bpm='bad' WHERE audio_hash='track-00'")
            conn.commit()
        response = client.get("/insights/profile")
        self.assertEqual(response.status_code, 503)
        self.assertIn("invalid tempo value", response.text)
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("UPDATE similarity_vectors SET energy='bad' WHERE audio_hash='track-00'")
            conn.commit()
        response = client.get("/insights/profile")
        self.assertEqual(response.status_code, 503)
        self.assertIn("invalid energy value", response.text)
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute("DELETE FROM similarity_vectors WHERE audio_hash='track-00'")
            conn.commit()
        response = client.get("/insights/profile")
        self.assertEqual(response.status_code, 503)
        self.assertIn("similarity counts do not match", response.text)

    def test_old_descriptor_recipe_is_reported(self) -> None:
        self.build()
        with closing(sqlite3.connect(self.output)) as conn:
            conn.execute(
                "UPDATE metadata SET value=? WHERE key='descriptor_recipe'",
                (json.dumps("old-recipe"),),
            )
            conn.commit()
        response = self.client().get("/insights/profile")
        self.assertIn("descriptors use an older recipe", response.text)


if __name__ == "__main__":
    unittest.main()

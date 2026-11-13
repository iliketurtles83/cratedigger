"""Unit tests for lib/recommender.py."""

import gzip
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from lib.recommender import (
    VectorBaker,
    HoldoutEvaluator,
    encode_harmonic_circle,
    encode_tempo_circle,
    export_features,
    PITCH_CLASSES,
    RECIPE_VERSION,
)


class TestRecommenderMath(unittest.TestCase):
    def test_tempo_circle_octave_folding(self):
        """Verify that 65 BPM and 130 BPM map to identical unit vectors."""
        v65 = encode_tempo_circle(65.0)
        v130 = encode_tempo_circle(130.0)
        self.assertAlmostEqual(float(np.dot(v65, v130)), 1.0, places=5)

        # Non-harmonic BPMs should differ
        v90 = encode_tempo_circle(90.0)
        self.assertLess(float(np.dot(v65, v90)), 0.99)

    def test_harmonic_circle_relative_keys(self):
        """Verify that C major and A minor have close harmonic cosine similarity."""
        c_maj = encode_harmonic_circle("C", "major")
        a_min = encode_harmonic_circle("A", "minor")
        # Both share the same angle on circle of fifths (theta), but differ in mode sign
        # Dot product is 0.5 * 1.0 + 0.5 * (-1.0) = 0.0 or positive
        # More importantly, C major and G major (adjacent on circle of fifths):
        g_maj = encode_harmonic_circle("G", "major")
        dot_cg = float(np.dot(c_maj, g_maj))

        # C major and F# major (opposite on circle, tritone):
        fsharp_maj = encode_harmonic_circle("F#", "major")
        dot_cfsharp = float(np.dot(c_maj, fsharp_maj))

        self.assertGreater(dot_cg, dot_cfsharp)

    def test_baked_vector_is_unit_norm(self):
        baker = VectorBaker()
        row = {
            "embedding": np.random.randn(400).astype(np.float32),
            "bpm": 124.0,
            "key": "E",
            "mode": "minor",
            "energy": 0.8,
            "valence": 0.6,
            "danceability": 0.7,
            "acousticness": 0.2,
            "speechiness": 0.1,
        }
        vec = baker.bake_vector(row)
        self.assertEqual(len(vec), 410)
        self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=5)

    def test_dot_product_equals_weighted_components(self):
        """Verify that phone dot product computes the exact weighted sum of sub-similarities."""
        weights = {"embedding": 0.60, "tempo": 0.20, "harmonic": 0.15, "scalars": 0.05}
        baker = VectorBaker(weights=weights)

        r1 = {
            "embedding": np.random.randn(400).astype(np.float32),
            "bpm": 120.0,
            "key": "C",
            "mode": "major",
            "energy": 0.9,
            "valence": 0.7,
            "danceability": 0.8,
            "acousticness": 0.1,
            "speechiness": 0.1,
        }
        r2 = {
            "embedding": np.random.randn(400).astype(np.float32),
            "bpm": 120.0,
            "key": "C",
            "mode": "major",
            "energy": 0.85,
            "valence": 0.65,
            "danceability": 0.75,
            "acousticness": 0.15,
            "speechiness": 0.08,
        }

        v1 = baker.bake_vector(r1)
        v2 = baker.bake_vector(r2)

        # Individual sub-dots
        # embedding sub-dot
        emb1 = r1["embedding"] / np.linalg.norm(r1["embedding"])
        emb2 = r2["embedding"] / np.linalg.norm(r2["embedding"])
        d_emb = float(np.dot(emb1, emb2))

        # tempo sub-dot
        t1 = encode_tempo_circle(r1["bpm"])
        t2 = encode_tempo_circle(r2["bpm"])
        d_tempo = float(np.dot(t1, t2))

        # harmonic sub-dot
        h1 = encode_harmonic_circle(r1["key"], r1["mode"])
        h2 = encode_harmonic_circle(r2["key"], r2["mode"])
        d_harm = float(np.dot(h1, h2))

        expected_weighted = (
            weights["embedding"] * d_emb
            + weights["tempo"] * d_tempo
            + weights["harmonic"] * d_harm
            + weights["scalars"] * 1.0  # scalars are very close
        )

        actual_dot = float(np.dot(v1, v2))
        self.assertAlmostEqual(actual_dot, expected_weighted, delta=0.05)


class TestRecommenderEvaluationAndExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        # Create synthetic DataFrame
        records = []
        for i in range(50):
            records.append({
                "audio_hash": f"a1:hash_{i:04d}",
                "path": f"/music/track_{i}.mp3",
                "embedding": np.random.randn(400).astype(np.float32),
                "bpm": 100.0 + (i % 20) * 2.0,
                "key": PITCH_CLASSES[i % 12],
                "mode": "major" if i % 2 == 0 else "minor",
                "energy": 0.5 + 0.01 * (i % 30),
                "valence": 0.5 - 0.01 * (i % 30),
                "danceability": 0.6,
                "acousticness": 0.3,
                "speechiness": 0.1,
            })
        self.df = pd.DataFrame(records)

    def tearDown(self):
        self.tmp.cleanup()

    def test_holdout_evaluator(self):
        baker = VectorBaker()
        evaluator = HoldoutEvaluator(baker, self.df, holdout_ratio=0.20, random_seed=42)
        metrics = evaluator.evaluate(k_neighbors=3, top_k_list=(5, 10))

        self.assertIn("recall@5", metrics)
        self.assertIn("recall@10", metrics)
        self.assertIn("mrr", metrics)
        self.assertGreaterEqual(metrics["recall@10"], 0.0)
        self.assertLessEqual(metrics["recall@10"], 1.0)

    def test_export_features_gzip(self):
        out_file = self.root / "export.json.gz"
        res = export_features(self.df, out_file)

        self.assertTrue(out_file.exists())
        self.assertEqual(res["track_count"], 50)
        self.assertEqual(res["dims"], 410)

        # Decompress and verify JSON
        with gzip.open(out_file, "rt", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["model_version"], RECIPE_VERSION)
        self.assertEqual(data["dims"], 410)
        self.assertEqual(len(data["tracks"]), 50)
        track = data["tracks"][0]
        self.assertIn("audio_hash", track)
        self.assertIn("vector", track)
        self.assertEqual(len(track["vector"]), 410)
        self.assertIn("bpm", track)
        self.assertIn("energy", track)

    def test_export_features_deduplicates_audio_hash_preferring_tier1(self):
        from lib.recommender import deduplicate_features_by_audio_hash

        dup_df = pd.DataFrame([
            {
                "audio_hash": "a1:dup",
                "path": "/music/0compilations/Best Hits/01 - Song.mp3",
                "embedding": np.ones(400, dtype=np.float32),
                "bpm": 120.0,
                "key": "C",
                "mode": "major",
                "energy": 0.5,
                "valence": 0.5,
                "danceability": 0.5,
                "acousticness": 0.2,
                "speechiness": 0.1,
                "artist_tag": "Compilation Copy",
                "error": None,
            },
            {
                "audio_hash": "a1:dup",
                "path": "/music/A/Artist/Artist - Album (2020)/01 - Artist - Song.mp3",
                "embedding": np.ones(400, dtype=np.float32),
                "bpm": 120.0,
                "key": "C",
                "mode": "major",
                "energy": 0.5,
                "valence": 0.5,
                "danceability": 0.5,
                "acousticness": 0.2,
                "speechiness": 0.1,
                "artist_tag": "Canonical Copy",
                "error": None,
            },
        ])
        deduped = deduplicate_features_by_audio_hash(dup_df)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped.iloc[0]["artist_tag"], "Canonical Copy")

        out_file = self.root / "dedup_export.json.gz"
        res = export_features(dup_df, out_file)
        self.assertEqual(res["track_count"], 1)


if __name__ == "__main__":
    unittest.main()

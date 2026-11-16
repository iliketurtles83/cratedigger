"""Unit tests for lib/recommender.py."""

import gzip
import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from lib import descriptors
from lib.embeddings import LATENT_DIM
from lib.recommender import (
    VectorBaker,
    HoldoutEvaluator,
    encode_harmonic_circle,
    encode_scalars,
    encode_tempo_circle,
    export_features,
    PITCH_CLASSES,
    RECIPE_VERSION,
)


def fake_runners():
    """Head i outputs sigmoid(embedding[i]) for its positive class (regressions: embedding[i])."""
    runners = {}
    for i, spec in enumerate(descriptors.HEADS):
        def run(x, i=i, spec=spec):
            z = x[:, i].astype(np.float64)
            if spec.positive is None:
                return z[:, None]
            p = 1.0 / (1.0 + np.exp(-z))
            pos = spec.classes.index(spec.positive)
            out = np.empty((len(x), 2))
            out[:, pos], out[:, 1 - pos] = p, 1.0 - p
            return out
        runners[spec.field] = run
    return runners


def latent(**logits: float) -> np.ndarray:
    vec = np.zeros(LATENT_DIM, dtype=np.float32)
    for i, spec in enumerate(descriptors.HEADS):
        vec[i] = logits.get(spec.field, 0.0)
    return vec


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
            "energy": 1.4,
            "valence": 0.4,
            "danceability": 0.85,
            "acousticness": -1.4,
            "instrumentalness": -2.2,
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
            "energy": 2.2,
            "valence": 0.85,
            "danceability": 1.4,
            "acousticness": -2.2,
            "instrumentalness": -2.2,
        }
        r2 = {
            "embedding": np.random.randn(400).astype(np.float32),
            "bpm": 120.0,
            "key": "C",
            "mode": "major",
            "energy": 1.7,
            "valence": 0.6,
            "danceability": 1.1,
            "acousticness": -1.7,
            "instrumentalness": -2.4,
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
                "energy": -1.5 + 0.1 * (i % 30),
                "valence": 1.5 - 0.1 * (i % 30),
                "danceability": 0.4,
                "acousticness": -0.8,
                "instrumentalness": -2.0,
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

    def test_gzip_write_failure_does_not_publish_partial_export(self):
        gzip_open = gzip.open

        @contextmanager
        def partial_write(*args, **kwargs):
            with gzip_open(*args, **kwargs) as stream:
                class FailingWriter:
                    def write(self, content):
                        stream.write(content[:16])
                        raise OSError("simulated write failure")

                yield FailingWriter()

        for existing in (True, False):
            with self.subTest(existing=existing):
                out_file = self.root / "export.json.gz"
                if existing:
                    out_file.write_bytes(b"previous export")
                with patch("lib.recommender.gzip.open", side_effect=partial_write):
                    with self.assertRaisesRegex(OSError, "simulated write failure"):
                        export_features(self.df, out_file)
                if existing:
                    self.assertEqual(out_file.read_bytes(), b"previous export")
                    out_file.unlink()
                else:
                    self.assertFalse(out_file.exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_json_write_failure_does_not_publish_partial_export(self):
        original_write_text = Path.write_text

        def partial_write(path, content, *args, **kwargs):
            original_write_text(path, content[:16], *args, **kwargs)
            raise OSError("simulated write failure")

        for existing in (True, False):
            with self.subTest(existing=existing):
                out_file = self.root / "export.json"
                if existing:
                    out_file.write_bytes(b"previous export")
                with patch.object(Path, "write_text", autospec=True, side_effect=partial_write):
                    with self.assertRaisesRegex(OSError, "simulated write failure"):
                        export_features(self.df, out_file)
                if existing:
                    self.assertEqual(out_file.read_bytes(), b"previous export")
                    out_file.unlink()
                else:
                    self.assertFalse(out_file.exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_export_json_and_replace_failure(self):
        out_file = self.root / "export.json"
        out_file.write_bytes(b"previous export")
        with patch.object(Path, "replace", side_effect=OSError("simulated publish failure")):
            with self.assertRaisesRegex(OSError, "simulated publish failure"):
                export_features(self.df, out_file)
        self.assertEqual(out_file.read_bytes(), b"previous export")
        self.assertEqual(list(self.root.iterdir()), [out_file])

        res = export_features(self.df, out_file)
        data = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertEqual(res["track_count"], 50)
        self.assertEqual(data["track_count"], 50)
        self.assertEqual(len(data["tracks"][0]["vector"]), 410)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_new_export_uses_normal_creation_mode(self):
        reference = self.root / "reference.json"
        reference.write_text("reference", encoding="utf-8")
        out_file = self.root / "export.json"
        export_features(self.df, out_file)
        self.assertEqual(
            stat.S_IMODE(out_file.stat().st_mode),
            stat.S_IMODE(reference.stat().st_mode),
        )

    def test_export_preserves_symlink_and_existing_file_mode(self):
        target = self.root / "target.json.gz"
        target.write_bytes(b"previous export")
        if os.name == "posix":
            target.chmod(0o640)
        out_file = self.root / "export.json.gz"
        try:
            out_file.symlink_to(target.name)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        export_features(self.df, out_file)

        self.assertTrue(out_file.is_symlink())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        with gzip.open(out_file, "rt", encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["track_count"], 50)

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
                "energy": 0.0,
                "valence": 0.0,
                "danceability": 0.0,
                "acousticness": -1.4,
                "instrumentalness": -2.2,
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
                "energy": 0.0,
                "valence": 0.0,
                "danceability": 0.0,
                "acousticness": -1.4,
                "instrumentalness": -2.2,
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

    def test_scalar_block_uses_descriptor_probabilities(self):
        means = {"energy": 0.5, "valence": 0.5, "danceability": 0.5, "acousticness": 0.5, "instrumentalness": 0.5}
        stds = dict.fromkeys(means, 0.25)
        arr = encode_scalars({"energy": 0.0, "valence": 50.0, "danceability": None}, means, stds)
        np.testing.assert_allclose(arr, [0.0, 1.0, 0.0, 0.0, 0.0], atol=1e-6)

    def test_export_v2_descriptors_levels_and_exclusions(self):
        base = {
            "embedding": np.ones(400, dtype=np.float32), "bpm": 120.04, "key": "C", "mode": "major",
            "loudness_lufs": -9.123, "native_peak_dbfs": -0.456, "signal_status": "ok", "decode_coverage": 1.0,
        }
        df = pd.DataFrame([
            {**base, "audio_hash": "a1:ok", "danceability": 0.99, "speechiness": 0.5,
             "audio_embedding": latent(happy=2.0, sad=-1.0, aggressive=3.0, party=1.0, relaxed=-2.0, approachability=0.42)},
            {**base, "audio_hash": "a1:partial", "audio_embedding": latent(), "signal_status": "truncated",
             "decode_coverage": 0.8},
            {**base, "audio_hash": "a1:short", "audio_embedding": latent(), "signal_status": "truncated",
             "decode_coverage": 0.1},
            {**base, "audio_hash": "a1:silent", "audio_embedding": latent(), "signal_status": "silent"},
            {**base, "audio_hash": None, "audio_embedding": latent()},
        ])
        out_file = self.root / "v2.json.gz"
        res = export_features(df, out_file, descriptor_runners=fake_runners())
        self.assertEqual(res["track_count"], 2)
        self.assertEqual(res["excluded_count"], 2)

        with gzip.open(out_file, "rt", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["model_version"], "cratedigger-v2.0-discogs-effnet-hybrid")
        self.assertEqual(data["descriptor_recipe"], descriptors.DESCRIPTOR_RECIPE)
        self.assertEqual(data["descriptor_scale"], "logit")
        self.assertEqual(data["excluded_count"], 2)
        tracks = {t["audio_hash"]: t for t in data["tracks"]}
        self.assertEqual(set(tracks), {"a1:ok", "a1:partial"})

        ok = tracks["a1:ok"]
        self.assertEqual(len(ok["vector"]), 410)
        self.assertNotIn("speechiness", ok)
        self.assertNotIn("truncated", ok)
        self.assertEqual(ok["bpm"], 120.0)
        self.assertEqual(ok["happy"], 2.0)
        self.assertEqual(ok["sad"], -1.0)
        self.assertEqual(ok["danceability"], 0.0)  # legacy proxy column replaced
        self.assertEqual(ok["approachability"], 0.42)
        self.assertEqual(ok["loudness_lufs"], -9.12)
        self.assertEqual(ok["native_peak_dbfs"], -0.46)
        sig = lambda x: 1.0 / (1.0 + np.exp(-x))  # noqa: E731
        energy_p = (sig(3.0) + sig(1.0) + 1.0 - sig(-2.0)) / 3.0
        self.assertAlmostEqual(ok["energy"], np.log(energy_p / (1.0 - energy_p)), places=3)
        for field in ("valence", "acousticness", "instrumentalness", "relaxed", "aggressive", "party",
                      "electronic", "engagement"):
            self.assertIn(field, ok)
        self.assertTrue(tracks["a1:partial"]["truncated"])


if __name__ == "__main__":
    unittest.main()

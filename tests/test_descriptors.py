"""Tests for lib/descriptors.py (learned descriptors from EffNet heads)."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from lib import descriptors as D
from lib.embeddings import LATENT_DIM


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def fake_runners():
    """Head i reads its logit from embedding column i (regressions return column i as-is)."""
    runners = {}
    for i, spec in enumerate(D.HEADS):
        def run(x, i=i, spec=spec):
            z = x[:, i].astype(np.float64)
            if spec.positive is None:
                return z[:, None].astype(np.float32)
            p = _sigmoid(z)
            pos = spec.classes.index(spec.positive)
            out = np.empty((len(x), 2), dtype=np.float32)
            out[:, pos] = p
            out[:, 1 - pos] = 1.0 - p
            return out
        runners[spec.field] = run
    return runners


def _embedding(values: dict[str, float]) -> np.ndarray:
    vec = np.zeros(LATENT_DIM, dtype=np.float32)
    for i, spec in enumerate(D.HEADS):
        vec[i] = values.get(spec.field, 0.0)
    return vec


class TestLogits(unittest.TestCase):
    def test_classifier_logit_respects_class_order(self):
        happy = next(h for h in D.HEADS if h.field == "happy")  # positive first
        sad = next(h for h in D.HEADS if h.field == "sad")  # positive second
        probs = np.array([[0.8, 0.2]])
        self.assertAlmostEqual(D.classifier_logit(probs, happy)[0], np.log(4.0))
        self.assertAlmostEqual(D.classifier_logit(probs, sad)[0], -np.log(4.0))

    def test_saturated_softmax_stays_finite_and_bounded(self):
        happy = next(h for h in D.HEADS if h.field == "happy")
        logits = D.classifier_logit(np.array([[1.0, 0.0], [1.0, 1e-30], [0.0, 1.0]]), happy)
        self.assertTrue(np.all(np.isfinite(logits)))
        self.assertAlmostEqual(logits[0], D.LOGIT_BOUND, places=6)
        self.assertGreater(logits[0], logits[1])
        self.assertAlmostEqual(logits[2], -D.LOGIT_BOUND, places=6)

    def test_mean_probability_logit_is_exact_and_tail_safe(self):
        a, b, c = np.array([0.3, -2.0]), np.array([1.5, 4.0]), np.array([-0.7, 0.0])
        mean_p = (_sigmoid(a) + _sigmoid(b) + _sigmoid(c)) / 3
        np.testing.assert_allclose(D.mean_probability_logit([a, b, c]), np.log(mean_p / (1 - mean_p)))
        extreme = D.mean_probability_logit([np.array([80.0, 85.0]), np.array([80.0, 85.0])])
        self.assertTrue(np.all(np.isfinite(extreme)))
        self.assertLess(extreme[0], extreme[1])

    def test_to_probability(self):
        self.assertAlmostEqual(D.to_probability(0.0), 0.5)
        self.assertAlmostEqual(float(D.to_probability(np.log(3.0))), 0.75)
        self.assertTrue(np.isnan(D.to_probability(np.nan)))
        series = D.to_probability(pd.Series([-1000.0, 1000.0]))
        self.assertEqual(series.tolist(), [0.0, 1.0])


class TestComputeDescriptors(unittest.TestCase):
    def test_columns_values_and_composites(self):
        vals = {"happy": 1.2, "sad": -0.4, "relaxed": 2.0, "aggressive": -3.0, "party": 0.5,
                "instrumentalness": 2.5, "approachability": 0.7, "engagement": 0.9}
        out = D.compute_descriptors(np.stack([_embedding(vals)]), fake_runners())
        self.assertEqual(tuple(out.columns), D.DESCRIPTOR_COLUMNS)
        row = out.iloc[0]
        for field in ("happy", "sad", "relaxed", "instrumentalness"):
            self.assertAlmostEqual(row[field], vals[field], places=5)
        self.assertAlmostEqual(row["approachability"], 0.7, places=6)
        energy_p = (_sigmoid(-3.0) + _sigmoid(0.5) + 1 - _sigmoid(2.0)) / 3
        valence_p = (_sigmoid(1.2) + 1 - _sigmoid(-0.4)) / 2
        self.assertAlmostEqual(D.to_probability(row["energy"]), energy_p, places=5)
        self.assertAlmostEqual(D.to_probability(row["valence"]), valence_p, places=5)

    def test_batches_large_inputs_in_order(self):
        rng = np.random.default_rng(0)
        matrix = rng.normal(size=(7, LATENT_DIM)).astype(np.float32)
        with patch.object(D, "BATCH_ROWS", 3):
            out = D.compute_descriptors(matrix, fake_runners())
        np.testing.assert_allclose(out["happy"], matrix[:, 0], atol=1e-5)

    def test_rejects_wrong_dimension(self):
        with self.assertRaises(ValueError):
            D.compute_descriptors(np.zeros((2, 400)), fake_runners())


class TestDescriptorFrame(unittest.TestCase):
    def test_invalid_embeddings_get_nan_and_index_is_kept(self):
        bad = np.full(LATENT_DIM, np.nan, dtype=np.float32)
        df = pd.DataFrame(
            {"audio_embedding": [_embedding({"happy": 2.0}), None, np.zeros(400), bad, _embedding({"happy": -1.0})]},
            index=[10, 11, 12, 13, 14],
        )
        out = D.descriptor_frame(df, fake_runners())
        self.assertEqual(list(out.index), [10, 11, 12, 13, 14])
        self.assertAlmostEqual(out.loc[10, "happy"], 2.0, places=5)
        self.assertAlmostEqual(out.loc[14, "happy"], -1.0, places=5)
        self.assertTrue(out.loc[[11, 12, 13]].isna().all().all())

    def test_missing_column_or_no_valid_rows(self):
        self.assertTrue(D.descriptor_frame(pd.DataFrame({"x": [1]}), fake_runners()).isna().all().all())
        none_valid = pd.DataFrame({"audio_embedding": [None]})
        with patch.object(D, "load_head_sessions", side_effect=AssertionError("not needed")):
            self.assertTrue(D.descriptor_frame(none_valid).isna().all().all())

    def test_attach_replaces_legacy_proxy_columns(self):
        df = pd.DataFrame({"audio_embedding": [_embedding({"danceability": 3.0}), None], "danceability": [0.1, 0.2]})
        out = D.attach_descriptors(df, fake_runners())
        self.assertAlmostEqual(out.loc[0, "danceability"], 3.0, places=5)
        self.assertTrue(np.isnan(out.loc[1, "danceability"]))
        self.assertEqual(df.loc[0, "danceability"], 0.1)  # input untouched
        self.assertTrue(set(D.DESCRIPTOR_COLUMNS) <= set(out.columns))


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestEnsureHeads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.payload = b"onnx-bytes"
        self.spec = D.HeadSpec("x", "task", "x-discogs-effnet-1", ("a", "b"), "a",
                               hashlib.sha256(self.payload).hexdigest())

    def tearDown(self):
        self.tmp.cleanup()

    def test_downloads_verifies_and_reuses(self):
        with patch.object(D.urllib.request, "urlopen", return_value=_Response(self.payload)) as get:
            paths = D.ensure_heads((self.spec,), self.dir)
            self.assertEqual(paths["x"].read_bytes(), self.payload)
            D.ensure_heads((self.spec,), self.dir)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0].full_url,
                         f"{D.HEADS_BASE_URL}/task/x-discogs-effnet-1.onnx")
        self.assertEqual([p.name for p in self.dir.iterdir()], ["x-discogs-effnet-1.onnx"])

    def test_checksum_mismatch_raises_and_leaves_nothing(self):
        with patch.object(D.urllib.request, "urlopen", return_value=_Response(b"tampered")):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                D.ensure_heads((self.spec,), self.dir)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_altered_local_file_is_replaced(self):
        (self.dir / "x-discogs-effnet-1.onnx").write_bytes(b"stale")
        with patch.object(D.urllib.request, "urlopen", return_value=_Response(self.payload)):
            D.ensure_heads((self.spec,), self.dir)
        self.assertEqual((self.dir / "x-discogs-effnet-1.onnx").read_bytes(), self.payload)

    def test_pinned_specs_are_consistent(self):
        fields = [h.field for h in D.HEADS]
        self.assertEqual(len(fields), len(set(fields)))
        for spec in D.HEADS:
            self.assertEqual(len(spec.sha256), 64)
            if spec.positive:
                self.assertEqual(len(spec.classes), 2)
                self.assertIn(spec.positive, spec.classes)
        for parts in D.COMPOSITES.values():
            for field, sign in parts:
                self.assertIn(field, D.CLASSIFIER_FIELDS)
                self.assertIn(sign, (1, -1))


if __name__ == "__main__":
    unittest.main()

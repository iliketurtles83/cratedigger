"""Unit tests for lib/embeddings.py."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf

from lib import embeddings
from lib.embeddings import (
    DECODE_SAMPLE_RATE,
    EMBEDDING_COLUMNS,
    EMBEDDING_FAILED,
    EMBEDDING_OK,
    EMBEDDING_RECIPE,
    EMBEDDING_SKIPPED_DURATION,
    EMBEDDING_SKIPPED_SILENT,
    EMBEDDING_SKIPPED_TOO_SHORT,
    EMBEDDING_UNAVAILABLE,
    LATENT_DIM,
    MEL_BANDS,
    PATCH_FRAMES,
    STYLE_DIM,
)

PROVENANCE = {"embedding_model_sha256": "model-sha", "style_labels_sha256": "labels-sha"}


def _output(name: str, dim: int) -> MagicMock:
    output = MagicMock()
    output.name = name
    output.shape = [None, dim]
    return output


def _mock_session(style_dim: int = STYLE_DIM, latent_dim: int = LATENT_DIM, run=None) -> MagicMock:
    """Session whose outputs are a deterministic function of the mel patches."""
    session = MagicMock()
    model_input = MagicMock()
    model_input.name = "melspectrogram"
    model_input.shape = [None, PATCH_FRAMES, MEL_BANDS]
    session.get_inputs.return_value = [model_input]
    # Latent listed first: names must be resolved by dimension, not position.
    session.get_outputs.return_value = [_output("embeddings", latent_dim), _output("activations", style_dim)]

    def default_run(names, feed):
        patches = feed["melspectrogram"]
        flat = patches.reshape(patches.shape[0], -1).astype(np.float32)
        outputs = {"activations": flat[:, :style_dim] + 1.0, "embeddings": flat[:, :latent_dim] - 1.0}
        return [outputs[name] for name in names]

    session.run.side_effect = run or default_run
    return session


def _tone(seconds: float, freq: float = 440.0, sr: int = DECODE_SAMPLE_RATE, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _valid_row(**overrides) -> dict:
    row = {
        "audio_hash": "hash-a",
        "embedding": embeddings.normalize_vector(np.ones(STYLE_DIM, dtype=np.float32)),
        "style_activations": np.ones(STYLE_DIM, dtype=np.float32),
        "audio_embedding": np.ones(LATENT_DIM, dtype=np.float32),
        "embedding_patches": 10,
        "embedding_status": EMBEDDING_OK,
        "embedding_error": None,
        "embedding_recipe": EMBEDDING_RECIPE,
        **PROVENANCE,
    }
    row.update(overrides)
    return row


class TestModelAssets(unittest.TestCase):
    def test_ensure_model_downloaded_returns_path(self):
        p = embeddings.ensure_model_downloaded()
        self.assertTrue(p.exists())

    def test_get_style_classes_returns_400(self):
        classes = embeddings.get_style_classes()
        self.assertEqual(len(classes), 400)
        self.assertTrue(any("Rock" in c for c in classes))

    def test_model_provenance_is_stable(self):
        first = embeddings.model_provenance()
        self.assertEqual(len(first["embedding_model_sha256"]), 64)
        self.assertEqual(len(first["style_labels_sha256"]), 64)
        self.assertEqual(first, embeddings.model_provenance())


class TestSignalPreparation(unittest.TestCase):
    def test_sanitize_zeroes_corruption_and_clips_overs(self):
        clean, zeroed = embeddings.sanitize_audio(
            np.array([0.5, np.nan, np.inf, -3.0, 1.5, -1.2], dtype=np.float32)
        )
        self.assertEqual(zeroed, 3)
        np.testing.assert_array_equal(clean, np.array([0.5, 0.0, 0.0, 0.0, 1.0, -1.0], dtype=np.float32))
        self.assertEqual(clean.dtype, np.float32)

    def test_hq_resampling_suppresses_aliasing(self):
        # A 10 kHz tone is above the 8 kHz Nyquist of the model input; it must not fold back.
        tone = _tone(2.0, freq=10_000.0)
        prepared = embeddings.prepare_model_input(tone, DECODE_SAMPLE_RATE)
        self.assertEqual(prepared.dtype, np.float32)
        self.assertAlmostEqual(prepared.size / tone.size, 16_000 / DECODE_SAMPLE_RATE, places=3)
        rms = float(np.sqrt(np.mean(prepared[1000:-1000] ** 2)))
        self.assertLess(rms, 1e-4)

    def test_prepare_downmixes_stereo(self):
        stereo = np.stack([_tone(1.0), _tone(1.0)])
        prepared = embeddings.prepare_model_input(stereo, DECODE_SAMPLE_RATE)
        self.assertEqual(prepared.ndim, 1)


@patch("lib.embeddings.model_provenance", return_value=PROVENANCE)
class TestExtraction(unittest.TestCase):
    @patch("lib.embeddings.get_inference_session")
    def test_one_run_returns_both_outputs(self, getter, _prov):
        session = _mock_session()
        getter.return_value = session

        result = embeddings.extract_embeddings(audio=_tone(30.0), sr=DECODE_SAMPLE_RATE)

        self.assertEqual(result.status, EMBEDDING_OK)
        self.assertEqual(session.run.call_count, 1)
        self.assertEqual(session.run.call_args.args[0], ["activations", "embeddings"])
        self.assertEqual(result.style_activations.shape, (STYLE_DIM,))
        self.assertEqual(result.audio_embedding.shape, (LATENT_DIM,))
        self.assertEqual(result.style_activations.dtype, np.float32)
        self.assertEqual(result.audio_embedding.dtype, np.float32)
        self.assertGreater(result.patches, 1)
        self.assertEqual(result.recipe, EMBEDDING_RECIPE)
        # Raw outputs are stored unnormalized; the compat vector is their L2-normalized style output.
        self.assertGreater(float(np.linalg.norm(result.style_activations)), 1.5)
        fields = embeddings.embedding_fields(result)
        self.assertEqual(set(fields), set(EMBEDDING_COLUMNS))
        self.assertAlmostEqual(float(np.linalg.norm(fields["embedding"])), 1.0, places=5)
        np.testing.assert_allclose(fields["embedding"], embeddings.normalize_vector(result.style_activations))
        self.assertEqual(fields["embedding_model_sha256"], "model-sha")

    @patch("lib.embeddings.get_inference_session")
    def test_path_and_buffer_inputs_agree(self, getter, _prov):
        getter.return_value = _mock_session()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            sf.write(path, _tone(20.0, sr=44_100), 44_100)
            from_path = embeddings.extract_embeddings(path)
            from_buffer = embeddings.extract_embeddings(
                audio=embeddings.decode_audio(path), sr=DECODE_SAMPLE_RATE
            )
            compat = embeddings.extract_embedding(path)
        self.assertEqual(from_path.status, EMBEDDING_OK)
        np.testing.assert_array_equal(from_path.style_activations, from_buffer.style_activations)
        np.testing.assert_array_equal(from_path.audio_embedding, from_buffer.audio_embedding)
        np.testing.assert_allclose(compat, from_path.normalized_style())

    @patch("lib.embeddings.get_inference_session")
    def test_non_default_input_rate_is_recorded_in_recipe(self, getter, _prov):
        getter.return_value = _mock_session()
        result = embeddings.extract_embeddings(audio=_tone(20.0, sr=44_100), sr=44_100)
        self.assertEqual(result.status, EMBEDDING_OK)
        self.assertEqual(result.recipe, f"{EMBEDDING_RECIPE}:input44100")

    @patch("lib.embeddings.get_inference_session")
    def test_too_short_skips_inference(self, getter, _prov):
        session = _mock_session()
        getter.return_value = session
        result = embeddings.extract_embeddings(audio=_tone(2.0), sr=DECODE_SAMPLE_RATE)
        self.assertEqual(result.status, EMBEDDING_SKIPPED_TOO_SHORT)
        session.run.assert_not_called()
        self.assertIsNone(embeddings.embedding_fields(result)["embedding"])

    @patch("lib.embeddings.get_inference_session")
    def test_wrong_output_shape_fails(self, getter, _prov):
        getter.return_value = _mock_session(
            run=lambda names, feed: [np.ones((1, STYLE_DIM)), np.ones((1, LATENT_DIM))]
        )
        result = embeddings.extract_embeddings(audio=_tone(30.0), sr=DECODE_SAMPLE_RATE)
        self.assertEqual(result.status, EMBEDDING_FAILED)
        self.assertIn("shape", result.error)
        self.assertIsNone(embeddings.extract_embedding("unused", audio=_tone(30.0), sr=DECODE_SAMPLE_RATE))

    @patch("lib.embeddings.get_inference_session")
    def test_non_finite_output_fails(self, getter, _prov):
        def run(names, feed):
            n = feed["melspectrogram"].shape[0]
            style = np.ones((n, STYLE_DIM), dtype=np.float32)
            style[0, 0] = np.nan
            return [style, np.ones((n, LATENT_DIM), dtype=np.float32)]

        getter.return_value = _mock_session(run=run)
        result = embeddings.extract_embeddings(audio=_tone(30.0), sr=DECODE_SAMPLE_RATE)
        self.assertEqual(result.status, EMBEDDING_FAILED)
        self.assertIn("non-finite", result.error)

    @patch("lib.embeddings.get_inference_session")
    def test_model_without_both_outputs_is_unavailable(self, getter, _prov):
        session = _mock_session()
        session.get_outputs.return_value = [_output("activations", STYLE_DIM)]
        getter.return_value = session
        result = embeddings.extract_embeddings(audio=_tone(30.0), sr=DECODE_SAMPLE_RATE)
        self.assertEqual(result.status, EMBEDDING_UNAVAILABLE)

    @patch("lib.embeddings.get_inference_session", side_effect=RuntimeError("no model"))
    def test_session_error_is_unavailable(self, _getter, _prov):
        result = embeddings.extract_embeddings(audio=_tone(30.0), sr=DECODE_SAMPLE_RATE)
        self.assertEqual(result.status, EMBEDDING_UNAVAILABLE)
        self.assertIn("no model", result.error)

    @patch("lib.embeddings.decode_audio", side_effect=RuntimeError("decode boom"))
    @patch("lib.embeddings.get_inference_session")
    def test_decode_error_is_failed(self, getter, _decode, _prov):
        getter.return_value = _mock_session()
        result = embeddings.extract_embeddings("broken.flac")
        self.assertEqual(result.status, EMBEDDING_FAILED)
        self.assertIn("decode boom", result.error)


@patch("lib.embeddings.model_provenance", return_value=PROVENANCE)
class TestRefreshRules(unittest.TestCase):
    def test_current_valid_row_is_kept(self, _prov):
        self.assertFalse(embeddings.embedding_needs_refresh(_valid_row()))

    def test_legacy_and_retryable_rows_refresh(self, _prov):
        legacy = {"embedding": np.ones(STYLE_DIM, dtype=np.float32)}
        self.assertTrue(embeddings.embedding_needs_refresh(legacy))
        self.assertTrue(embeddings.embedding_needs_refresh(_valid_row(embedding_status=float("nan"))))
        self.assertTrue(embeddings.embedding_needs_refresh(_valid_row(embedding_status=EMBEDDING_FAILED)))
        self.assertTrue(embeddings.embedding_needs_refresh(_valid_row(embedding_status=EMBEDDING_UNAVAILABLE)))

    def test_recipe_model_or_vector_changes_refresh(self, _prov):
        self.assertTrue(embeddings.embedding_needs_refresh(_valid_row(embedding_recipe="old")))
        self.assertTrue(embeddings.embedding_needs_refresh(_valid_row(embedding_model_sha256="other")))
        self.assertTrue(embeddings.embedding_needs_refresh(_valid_row(audio_embedding=None)))
        bad = np.ones(STYLE_DIM, dtype=np.float32)
        bad[3] = np.inf
        self.assertTrue(embeddings.embedding_needs_refresh(_valid_row(style_activations=bad)))
        self.assertTrue(
            embeddings.embedding_needs_refresh(_valid_row(audio_embedding=np.ones(STYLE_DIM, dtype=np.float32)))
        )

    def test_skip_statuses(self, _prov):
        short = _valid_row(embedding_status=EMBEDDING_SKIPPED_TOO_SHORT, embedding=None)
        self.assertFalse(embeddings.embedding_needs_refresh(short))
        # A different recipe may change the patch count, so short rows are re-evaluated.
        self.assertTrue(embeddings.embedding_needs_refresh({**short, "embedding_recipe": "old"}))
        for status in (EMBEDDING_SKIPPED_SILENT, EMBEDDING_SKIPPED_DURATION):
            self.assertFalse(
                embeddings.embedding_needs_refresh({"embedding_status": status, "embedding_recipe": "old"})
            )

    def test_reuse_requires_matching_hash_and_current_ok_row(self, _prov):
        reused = embeddings.reusable_embedding_fields(_valid_row(), "hash-a")
        self.assertEqual(set(reused), set(EMBEDDING_COLUMNS))
        self.assertIsNone(embeddings.reusable_embedding_fields(_valid_row(), "hash-b"))
        self.assertIsNone(embeddings.reusable_embedding_fields(_valid_row(), None))
        self.assertIsNone(embeddings.reusable_embedding_fields(None, "hash-a"))
        self.assertIsNone(
            embeddings.reusable_embedding_fields(_valid_row(embedding_recipe="old"), "hash-a")
        )
        self.assertIsNone(
            embeddings.reusable_embedding_fields(
                _valid_row(embedding_status=EMBEDDING_SKIPPED_TOO_SHORT), "hash-a"
            )
        )


class TestPreflight(unittest.TestCase):
    @patch("lib.embeddings.get_inference_session")
    def test_valid_session_passes(self, getter):
        getter.return_value = _mock_session()
        self.assertEqual(embeddings.preflight(), [])

    @patch("lib.embeddings.get_inference_session", side_effect=RuntimeError("missing"))
    def test_session_failure_blocks(self, _getter):
        problems = embeddings.preflight()
        self.assertEqual(len(problems), 1)
        self.assertIn("missing", problems[0])

    @patch("lib.embeddings.get_inference_session")
    def test_wrong_input_shape_blocks(self, getter):
        session = _mock_session()
        session.get_inputs.return_value[0].shape = [None, 64, 96]
        getter.return_value = session
        self.assertEqual(len(embeddings.preflight()), 1)

    def test_real_model_passes(self):
        self.assertEqual(embeddings.preflight(), [])


if __name__ == "__main__":
    unittest.main()

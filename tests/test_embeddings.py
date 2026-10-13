"""Unit tests for lib/embeddings.py."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from lib import embeddings


class TestEmbeddings(unittest.TestCase):
    def test_ensure_model_downloaded_returns_path(self):
        p = embeddings.ensure_model_downloaded()
        self.assertTrue(p.exists())

    def test_get_style_classes_returns_400(self):
        classes = embeddings.get_style_classes()
        self.assertEqual(len(classes), 400)
        self.assertTrue(any("Rock" in c for c in classes))

    @patch("lib.embeddings.get_inference_session")
    def test_extract_embedding_mock(self, mock_sess_getter):
        # Mock session that outputs dummy activations
        mock_sess = MagicMock()
        mock_inp = MagicMock()
        mock_inp.name = "melspectrogram"
        mock_sess.get_inputs.return_value = [mock_inp]
        # Return shape (2, 400)
        dummy_act = np.ones((2, 400), dtype=np.float32)
        mock_sess.run.return_value = [dummy_act]
        mock_sess_getter.return_value = mock_sess

        # Test with a mock audio path
        with patch("essentia.standard.MonoLoader") as mock_loader, \
             patch("essentia.standard.TensorflowInputMusiCNN") as mock_tf, \
             patch("essentia.standard.FrameGenerator") as mock_frames:
            mock_loader.return_value.return_value = np.zeros(32000, dtype=np.float32)
            mock_tf.return_value.return_value = np.zeros(96, dtype=np.float32)
            mock_frames.return_value = [np.zeros(512) for _ in range(256)]

            vec = embeddings.extract_embedding("dummy.mp3")
            self.assertIsNotNone(vec)
            self.assertEqual(vec.shape, (400,))
            # Should be L2-normalized
            self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=5)


if __name__ == "__main__":
    unittest.main()

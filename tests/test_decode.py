"""Tests for lib/decode.py (truncation-safe decoding)."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from lib import decode


class FakeLibrosa:
    """Stands in for librosa.load returning a fixed (possibly short) buffer."""

    def __init__(self, samples: np.ndarray, sr: int):
        self.samples = samples
        self.sr = sr
        self.calls = 0

    def load(self, path, sr=None, mono=True):
        self.calls += 1
        return self.samples, self.sr


class TestShortDecodeRule(unittest.TestCase):
    def test_thresholds_need_ratio_and_absolute_shortfall(self):
        self.assertFalse(decode.is_short(9.0, None))
        self.assertFalse(decode.is_short(9.0, 0.0))
        self.assertFalse(decode.is_short(1.0, 2.0))  # 50% but only 1 s short
        self.assertFalse(decode.is_short(197.5, 200.0))  # 2.5 s short but 98.75%
        self.assertTrue(decode.is_short(190.0, 200.0))
        self.assertTrue(decode.is_short(3.66, 252.0))

    def test_coverage_and_truncation_properties(self):
        audio = decode.NativeAudio(np.zeros(22050 * 3, dtype=np.float32), 22050, header_seconds=12.0)
        self.assertAlmostEqual(audio.seconds, 3.0)
        self.assertEqual(audio.coverage, 0.25)
        self.assertTrue(audio.truncated)
        unknown = decode.NativeAudio(np.zeros((2, 100), dtype=np.float32), 100)
        self.assertIsNone(unknown.coverage)
        self.assertFalse(unknown.truncated)
        self.assertAlmostEqual(unknown.seconds, 1.0)

    def test_header_seconds_unreadable_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.mp3"
            junk.write_bytes(b"not audio")
            self.assertIsNone(decode.header_seconds(junk))
            self.assertIsNone(decode.header_seconds(Path(tmp) / "missing.flac"))


class TestDecodeNative(unittest.TestCase):
    def test_healthy_decode_skips_ffmpeg(self):
        fake = FakeLibrosa(np.zeros((2, 1000), dtype=np.float32), 1000)
        with patch.object(decode, "ffmpeg_decode") as ff:
            audio = decode.decode_native("x.mp3", fake, header_s=1.01)
        ff.assert_not_called()
        self.assertEqual(audio.decoder, decode.DECODER_LIBROSA)
        self.assertFalse(audio.truncated)

    def test_short_decode_recovered_by_ffmpeg(self):
        fake = FakeLibrosa(np.zeros((2, 3000), dtype=np.float32), 1000)
        full = np.ones((2, 60000), dtype=np.float32)
        with patch.object(decode, "ffmpeg_decode", return_value=full) as ff:
            audio = decode.decode_native("x.mp3", fake, header_s=60.0)
        ff.assert_called_once_with("x.mp3", 1000, 2)
        self.assertEqual(audio.decoder, decode.DECODER_FFMPEG)
        self.assertIs(audio.samples, full)
        self.assertEqual(audio.coverage, 1.0)
        self.assertFalse(audio.truncated)

    def test_short_decode_kept_when_ffmpeg_is_not_longer(self):
        fake = FakeLibrosa(np.zeros(3000, dtype=np.float32), 1000)
        for retry in (None, np.zeros(2000, dtype=np.float32)):
            with self.subTest(retry=None if retry is None else retry.shape), \
                 patch.object(decode, "ffmpeg_decode", return_value=retry) as ff:
                audio = decode.decode_native("x.mp3", fake, header_s=60.0)
            ff.assert_called_once_with("x.mp3", 1000, 1)
            self.assertEqual(audio.decoder, decode.DECODER_LIBROSA)
            self.assertEqual(audio.coverage, 0.05)
            self.assertTrue(audio.truncated)

    def test_header_read_when_not_supplied(self):
        fake = FakeLibrosa(np.zeros(1000, dtype=np.float32), 1000)
        with patch.object(decode, "header_seconds", return_value=None) as hdr, \
             patch.object(decode, "ffmpeg_decode") as ff:
            audio = decode.decode_native("x.mp3", fake)
        hdr.assert_called_once_with("x.mp3")
        ff.assert_not_called()
        self.assertIsNone(audio.coverage)


class TestFfmpegDecode(unittest.TestCase):
    def test_missing_ffmpeg_returns_none(self):
        with patch.object(decode.shutil, "which", return_value=None):
            self.assertIsNone(decode.ffmpeg_decode("x.mp3", 44100, 2))

    @unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg is not installed")
    def test_matches_soundfile_channels_and_is_writable(self):
        import soundfile as sf

        rng = np.random.default_rng(5)
        data = (0.3 * rng.standard_normal((44100, 2))).astype(np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stereo.flac"
            sf.write(path, data, 44100, subtype="PCM_24")
            ref, _ = sf.read(path, dtype="float32", always_2d=True)
            stereo = decode.ffmpeg_decode(path, 44100, 2)
            mono = decode.ffmpeg_decode(path, 44100, 1)
            self.assertIsNone(decode.ffmpeg_decode(Path(tmp) / "missing.flac", 44100, 2))
        self.assertEqual(stereo.shape, (2, 44100))
        self.assertTrue(stereo.flags.writeable)
        np.testing.assert_allclose(stereo, ref.T, atol=1e-6)
        self.assertEqual(mono.shape, (44100,))
        self.assertTrue(mono.flags.writeable)


if __name__ == "__main__":
    unittest.main()

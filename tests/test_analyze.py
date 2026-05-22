"""Tests for 06_analyze.py (Phase 5 audio analysis)."""

import importlib.util
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("analyze", REPO_ROOT / "06_analyze.py")
analyze = importlib.util.module_from_spec(spec)
sys.modules["analyze"] = analyze
spec.loader.exec_module(analyze)


def _write_sine_wav(path: Path, freq: float = 440.0, duration_s: float = 2.0, sr: int = 22050) -> None:
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    samples = (0.5 * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())


class TestAnalyzeSchema(unittest.TestCase):
    def test_feature_columns_complete(self):
        expected = {
            "path", "mtime", "size",
            "artist_tag", "album_tag", "track_tag", "year_tag", "genre_tag",
            "duration_ms", "bpm", "key", "mode",
            "energy", "loudness_lufs", "spectral_centroid",
            "zero_crossing_rate", "danceability",
            "speechiness", "acousticness", "instrumentalness", "liveness", "valence",
            "schema_version", "analyzed_at", "error",
        }
        self.assertEqual(set(analyze.FEATURE_COLUMNS), expected)

    def test_danceability_proxy_range(self):
        self.assertEqual(analyze._danceability_proxy(0, 1.0), 0.0)
        score = analyze._danceability_proxy(120.0, 0.5)
        self.assertGreater(score, 0.0)
        self.assertLessEqual(score, 1.0)

    def test_key_estimation_returns_valid_pair(self):
        chroma = np.zeros(12)
        chroma[0] = 1.0  # strong C
        key, mode = analyze._estimate_key(chroma)
        self.assertIn(key, analyze._PITCH_CLASSES)
        self.assertIn(mode, ("major", "minor"))


class TestAnalyzeIncremental(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # WAV is not in AUDIO_EXTENSIONS by default — patch it for the test.
        import config
        self._orig_exts = config.AUDIO_EXTENSIONS
        config.AUDIO_EXTENSIONS = self._orig_exts | {".wav"}

        self.audio = self.root / "song.wav"
        _write_sine_wav(self.audio)
        self.output = self.root / "features.pkl"

    def tearDown(self):
        import config
        config.AUDIO_EXTENSIONS = self._orig_exts
        self.tmp.cleanup()

    def test_first_run_extracts_and_writes_pickle(self):
        df = analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        self.assertTrue(self.output.exists())
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row["path"], str(self.audio))
        self.assertEqual(row["schema_version"], analyze.FEATURE_SCHEMA_VERSION)
        self.assertIsNone(row["error"])
        self.assertGreater(row["duration_ms"], 1000)
        self.assertIn(row["mode"], ("major", "minor"))

    def test_second_run_is_incremental(self):
        df1 = analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        ts1 = df1.iloc[0]["analyzed_at"]
        df2 = analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        ts2 = df2.iloc[0]["analyzed_at"]
        # Unchanged file should be skipped: analyzed_at preserved.
        self.assertEqual(ts1, ts2)

    def test_force_reanalyzes(self):
        analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        df2 = analyze.analyze_folder(self.root, self.output, force=True, dry_run=False)
        self.assertEqual(len(df2), 1)

    def test_dry_run_does_not_write_or_extract(self):
        # Dry-run is now cheap: it reports what would be analyzed but does
        # not extract features. With no cache, the returned frame is empty.
        df = analyze.analyze_folder(self.root, self.output, force=False, dry_run=True)
        self.assertEqual(len(df), 0)
        self.assertFalse(self.output.exists())

    def test_dry_run_preserves_cached_rows(self):
        analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        df = analyze.analyze_folder(self.root, self.output, force=False, dry_run=True)
        self.assertEqual(len(df), 1)

    def test_orphan_rows_pruned(self):
        analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        self.audio.unlink()
        df = analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        self.assertEqual(len(df), 0)


if __name__ == "__main__":
    unittest.main()

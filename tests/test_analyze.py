"""Tests for 06_analyze.py (Phase 5 audio analysis)."""

import importlib.util
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

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
            "audio_hash", "path", "mtime", "size",
            "artist_tag", "album_tag", "track_tag", "year_tag", "genre_tag",
            "rating_tag", "rating",
            "duration_ms", "bpm_tag", "bpm", "bpm_confidence", "key", "mode",
            "energy", "loudness_lufs", "spectral_centroid",
            "zero_crossing_rate", "danceability",
            "speechiness", "acousticness", "instrumentalness", "liveness", "valence",
            "embedding",
            "schema_version", "analyzed_at", "error",
        }
        self.assertEqual(set(analyze.FEATURE_COLUMNS), expected)

    def test_danceability_proxy_range(self):
        self.assertEqual(analyze._danceability_proxy(0, 1.0), 0.0)
        self.assertEqual(analyze._danceability_proxy(None, 1.0, dance_raw=2.0, duration_s=30.0), 0.0)
        score = analyze._danceability_proxy(120.0, 0.5)
        self.assertGreater(score, 0.0)
        self.assertLessEqual(score, 1.0)
        es_score = analyze._danceability_proxy(
            120.0, 0.5, dance_raw=2.1, bpm_confidence=0.8, duration_s=30.0
        )
        self.assertGreater(es_score, 0.0)
        self.assertLessEqual(es_score, 1.0)

    def test_sanitize_audio_and_key_normalization(self):
        dirty = np.array([0.5, np.nan, np.inf, -np.inf, 3279.0, -500.0, 1.3], dtype=np.float32)
        clean = analyze._sanitize_audio(dirty)
        self.assertTrue(np.all(np.isfinite(clean)))
        self.assertEqual(clean[0], 0.5)
        self.assertEqual(clean[1], 0.0)
        self.assertEqual(clean[4], 0.0)
        self.assertEqual(clean[5], 0.0)
        self.assertEqual(clean[6], 1.0)
        self.assertEqual(analyze._normalize_key_name("Bb"), "A#")
        self.assertEqual(analyze._normalize_key_name("C#"), "C#")

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
        self.assertGreater(row["energy"], 0.3)  # 0.5 amplitude sine -> RMS ~0.35 (not Hann-weighted ~0.21)
        self.assertIn(row["mode"], ("major", "minor"))

    def test_audio_bpm_ignores_corrupt_or_stale_bpm_tag(self):
        from lib.tempo import TempoResult

        fake_tags = {
            "artist": "Artist",
            "album": "Album",
            "genre": "Hip-Hop",
            "bpm": "12704",
        }
        with patch.object(analyze, "read_tags", return_value=fake_tags), \
             patch.object(
                 analyze,
                 "estimate_tempo",
                 return_value=TempoResult(bpm=88.5, confidence=3.1, method="essentia_rhythm2013_mf"),
             ):
            row = analyze.extract_features(self.audio)

        self.assertIsNone(row["bpm_tag"])
        self.assertEqual(row["bpm"], 88.5)
        self.assertEqual(row["bpm_confidence"], 3.1)

    def test_valid_bpm_tag_preserved_while_audio_bpm_wins(self):
        from lib.tempo import TempoResult

        fake_tags = {
            "artist": "Artist",
            "album": "Album",
            "genre": "Hip-Hop",
            "bpm": "178",
        }
        with patch.object(analyze, "read_tags", return_value=fake_tags), \
             patch.object(
                 analyze,
                 "estimate_tempo",
                 return_value=TempoResult(bpm=88.5, confidence=3.1, method="essentia_rhythm2013_mf"),
             ):
            row = analyze.extract_features(self.audio)

        self.assertEqual(row["bpm_tag"], 178.0)
        self.assertEqual(row["bpm"], 88.5)
        self.assertEqual(row["bpm_confidence"], 3.1)

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

    def test_unreadable_store_stops_without_overwriting(self):
        original = b"unreadable feature store"
        self.output.write_bytes(original)
        with patch.object(analyze, "extract_features") as extract:
            with self.assertRaisesRegex(RuntimeError, "avoid overwriting saved progress"):
                analyze.analyze_folder(self.root, self.output)
        extract.assert_not_called()
        self.assertEqual(self.output.read_bytes(), original)

    def test_incompatible_pickle_stops_without_overwriting(self):
        original = b"existing feature store"
        self.output.write_bytes(original)
        with patch.object(analyze.pd, "read_pickle", side_effect=NotImplementedError("str array")):
            with self.assertRaisesRegex(RuntimeError, "compatible pandas version"):
                analyze.analyze_folder(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), original)

    def test_non_dataframe_store_stops_without_overwriting(self):
        analyze.pd.to_pickle({"not": "a DataFrame"}, self.output)
        original = self.output.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "avoid overwriting saved progress"):
            analyze.analyze_folder(self.root, self.output)
        self.assertEqual(self.output.read_bytes(), original)

    def test_empty_cache_does_not_precompute_hashes(self):
        with patch.object(analyze, "audio_hash") as hash_file:
            analyze.analyze_folder(self.root, self.output, dry_run=True)
        hash_file.assert_not_called()

    def test_schema_bump_dry_run_skips_hashing_known_paths(self):
        analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        df_old = analyze.pd.read_pickle(self.output)
        df_old["schema_version"] = analyze.FEATURE_SCHEMA_VERSION - 1
        df_old["audio_hash"] = "abc123"
        df_old.to_pickle(self.output)
        with patch.object(analyze, "audio_hash") as hash_file:
            analyze.analyze_folder(self.root, self.output, dry_run=True)
        hash_file.assert_not_called()

    def test_corrupt_decoder_spikes_clamped_in_extract_features(self):
        spike = np.full(22050, 5000.0, dtype=np.float32)
        with patch("librosa.load", return_value=(spike, 22050)):
            row = analyze.extract_features(self.audio)
        self.assertEqual(row["energy"], 0.0)
        if row["loudness_lufs"] is not None:
            self.assertLessEqual(row["loudness_lufs"], 0.0)

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

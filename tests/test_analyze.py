"""Tests for 06_analyze.py (Phase 5 audio analysis)."""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from lib.embeddings import (
    EMBEDDING_FAILED,
    EMBEDDING_OK,
    EMBEDDING_RECIPE,
    EMBEDDING_SKIPPED_SILENT,
    EMBEDDING_SKIPPED_TOO_SHORT,
    EMBEDDING_UNAVAILABLE,
    LATENT_DIM,
    STYLE_DIM,
    EmbeddingResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("analyze", REPO_ROOT / "06_analyze.py")
analyze = importlib.util.module_from_spec(spec)
sys.modules["analyze"] = analyze
spec.loader.exec_module(analyze)


def _write_sine_wav(path: Path, freq: float = 440.0, duration_s: float = 2.0, sr: int = 22050, amp: float = 0.5) -> None:
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    samples = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
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
            "bpm_method", "bpm_status", "key_method", "key_strength",
            "dynamic_complexity", "danceability_raw", "mfcc_delta_std",
            "peak_dbfs", "rms_dbfs", "sanitized_fraction", "signal_status",
            "embedding", "style_activations", "audio_embedding", "embedding_patches",
            "embedding_status", "embedding_error", "embedding_recipe",
            "embedding_model_sha256", "style_labels_sha256",
            "schema_version", "analyzed_at", "error",
        }
        self.assertEqual(set(analyze.FEATURE_COLUMNS), expected)
        self.assertEqual(len(analyze.FEATURE_COLUMNS), len(expected))

    def test_signal_quality_flags_silence(self):
        silent = analyze._signal_quality(np.zeros(1000, dtype=np.float32), 0, 1000)
        self.assertEqual(silent["signal_status"], "silent")
        self.assertEqual(silent["sanitized_fraction"], 0.0)
        tone = 0.5 * np.sin(np.linspace(0, 100, 1000)).astype(np.float32)
        audible = analyze._signal_quality(tone, 10, 1000)
        self.assertEqual(audible["signal_status"], "ok")
        self.assertAlmostEqual(audible["peak_dbfs"], -6.02, places=1)
        self.assertAlmostEqual(audible["rms_dbfs"], -9.03, places=0)
        self.assertEqual(audible["sanitized_fraction"], 0.01)

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
        with patch("librosa.load", return_value=(spike, 22050)), \
             patch.object(analyze, "extract_embeddings") as extract:
            row = analyze.extract_features(self.audio)
        self.assertEqual(row["energy"], 0.0)
        if row["loudness_lufs"] is not None:
            self.assertLessEqual(row["loudness_lufs"], 0.0)
        self.assertEqual(row["sanitized_fraction"], 1.0)
        self.assertEqual(row["signal_status"], "silent")
        self.assertEqual(row["embedding_status"], EMBEDDING_SKIPPED_SILENT)
        extract.assert_not_called()

    def test_dry_run_preserves_cached_rows(self):
        analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        df = analyze.analyze_folder(self.root, self.output, force=False, dry_run=True)
        self.assertEqual(len(df), 1)

    def test_orphan_rows_pruned(self):
        analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        self.audio.unlink()
        df = analyze.analyze_folder(self.root, self.output, force=False, dry_run=False)
        self.assertEqual(len(df), 0)


class TestNativeLoudness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.librosa = analyze._load_librosa()
        self.pln = analyze._load_pyloudnorm()
        if self.librosa is None or self.pln is None:
            self.skipTest("librosa and pyloudnorm are required")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_stereo(self, name: str, left: np.ndarray, right: np.ndarray, sr: int = 44100) -> Path:
        import soundfile as sf
        path = self.root / name
        sf.write(path, np.stack([left, right], axis=1).astype(np.float32), sr, subtype="PCM_24")
        return path

    def test_native_decode_matches_analysis_buffer(self):
        rng = np.random.default_rng(3)
        n = 44100 * 3
        path = self._write_stereo("wide.flac", 0.2 * rng.standard_normal(n), 0.2 * rng.standard_normal(n))
        y_ref, sr_ref = analyze._load_analysis_audio(path, self.librosa)
        y, sr, lufs = analyze._load_analysis_audio_with_loudness(path, self.librosa)
        self.assertEqual(sr, sr_ref)
        self.assertTrue(np.array_equal(y, y_ref))
        self.assertIsNotNone(lufs)

    def test_loudness_sums_channels_at_native_rate(self):
        t = np.arange(44100 * 3) / 44100
        tone = 0.25 * np.sin(2 * np.pi * 1000 * t)
        path = self._write_stereo("dual_mono.flac", tone, tone)
        y, sr, lufs = analyze._load_analysis_audio_with_loudness(path, self.librosa)
        mono_lufs = float(self.pln.Meter(sr).integrated_loudness(y))
        self.assertAlmostEqual(lufs, mono_lufs + 3.01, delta=0.1)

        row = analyze.extract_features(path)
        self.assertEqual(row["loudness_lufs"], lufs)

    def test_short_or_silent_audio_has_no_loudness(self):
        self.assertIsNone(analyze._integrated_loudness(np.zeros((2, 44100), dtype=np.float32), 44100))
        self.assertIsNone(analyze._integrated_loudness(np.full(1000, 0.5, dtype=np.float32), 44100))


def _ok_embedding(scale: float = 1.0) -> EmbeddingResult:
    return EmbeddingResult(
        EMBEDDING_OK,
        style_activations=np.full(STYLE_DIM, 0.5 * scale, dtype=np.float32),
        audio_embedding=np.full(LATENT_DIM, 0.1 * scale, dtype=np.float32),
        patches=3,
    )


class TestAnalyzeV10(unittest.TestCase):
    """Embedding status/reuse, scoped pruning, preflight, and interrupt safety."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import config
        self._orig_exts = config.AUDIO_EXTENSIONS
        config.AUDIO_EXTENSIONS = self._orig_exts | {".wav"}
        self.music = self.root / "music"
        self.music.mkdir()
        self.audio = self.music / "song.wav"
        _write_sine_wav(self.audio)
        self.output = self.root / "features.pkl"

    def tearDown(self):
        import config
        config.AUDIO_EXTENSIONS = self._orig_exts
        self.tmp.cleanup()

    def run_analysis(self, folder: Path | None = None, **kwargs):
        return analyze.analyze_folder(folder or self.music, self.output, **kwargs)

    def stored(self):
        df = analyze.pd.read_pickle(self.output)
        return {row["path"]: row for row in df.to_dict("records")}

    def write_store(self, rows: dict) -> None:
        analyze.pd.DataFrame(list(rows.values()), columns=analyze.FEATURE_COLUMNS).to_pickle(self.output)

    def bump_mtime(self, path: Path) -> None:
        stat = path.stat()
        os.utime(path, (stat.st_atime + 100, stat.st_mtime + 100))

    def test_first_run_records_status_and_provenance(self):
        self.run_analysis()
        row = self.stored()[str(self.audio)]
        self.assertIsNone(row["error"])
        self.assertEqual(row["embedding_status"], EMBEDDING_SKIPPED_TOO_SHORT)
        self.assertIsNone(row["embedding"])
        self.assertEqual(row["embedding_recipe"], EMBEDDING_RECIPE)
        self.assertEqual(len(row["embedding_model_sha256"]), 64)
        self.assertEqual(row["signal_status"], "ok")
        self.assertAlmostEqual(row["peak_dbfs"], -6.02, places=1)
        self.assertEqual(row["sanitized_fraction"], 0.0)
        self.assertIsNotNone(row["bpm_status"])
        self.assertIn(row["key_method"], ("essentia_key_extractor", "librosa_ks_chroma"))

        before = self.output.stat().st_mtime_ns
        with patch.object(analyze, "extract_embeddings") as extract:
            self.run_analysis()
        extract.assert_not_called()
        self.assertEqual(self.output.stat().st_mtime_ns, before)

    def test_duration_skip_is_not_retried(self):
        class LongInfo:
            length = 1300.0

        class LongFile:
            info = LongInfo()

        with patch("mutagen.File", return_value=LongFile()):
            self.run_analysis()
        row = self.stored()[str(self.audio)]
        self.assertEqual(row["error"], "duration_exceeds_20min")
        self.assertEqual(row["embedding_status"], "skipped_duration")
        with patch.object(analyze, "_run_task") as run:
            self.run_analysis()
        run.assert_not_called()

    def test_failed_embedding_retried_without_full_analysis(self):
        for status in (EMBEDDING_FAILED, EMBEDDING_UNAVAILABLE):
            with self.subTest(status=status):
                self.output.unlink(missing_ok=True)
                with patch.object(analyze, "extract_embeddings", return_value=EmbeddingResult(status, error="boom")):
                    self.run_analysis()
                first = self.stored()[str(self.audio)]
                self.assertIsNone(first["error"])
                self.assertEqual(first["embedding_status"], status)

                with patch.object(analyze, "extract_embeddings", return_value=_ok_embedding()) as extract, \
                     patch.object(analyze, "extract_features") as full:
                    self.run_analysis()
                full.assert_not_called()
                extract.assert_called_once()
                row = self.stored()[str(self.audio)]
                self.assertEqual(row["embedding_status"], EMBEDDING_OK)
                self.assertEqual(row["analyzed_at"], first["analyzed_at"])
                self.assertEqual(row["audio_embedding"].shape, (LATENT_DIM,))
                self.assertAlmostEqual(float(np.linalg.norm(row["embedding"])), 1.0, places=5)

    def test_stale_recipe_refreshes_embedding_only(self):
        with patch.object(analyze, "extract_embeddings", return_value=_ok_embedding()):
            self.run_analysis()
        rows = self.stored()
        rows[str(self.audio)]["embedding_recipe"] = "old-recipe"
        self.write_store(rows)

        with patch.object(analyze, "extract_embeddings", return_value=_ok_embedding(2.0)) as extract, \
             patch.object(analyze, "extract_features") as full:
            self.run_analysis()
        full.assert_not_called()
        extract.assert_called_once()
        row = self.stored()[str(self.audio)]
        self.assertEqual(row["embedding_recipe"], EMBEDDING_RECIPE)
        self.assertAlmostEqual(float(row["audio_embedding"][0]), 0.2, places=6)

    def test_failed_refresh_keeps_previous_embedding(self):
        with patch.object(analyze, "extract_embeddings", return_value=_ok_embedding()):
            self.run_analysis()
        rows = self.stored()
        rows[str(self.audio)]["embedding_recipe"] = "old-recipe"
        self.write_store(rows)

        with patch.object(analyze, "extract_embeddings", return_value=EmbeddingResult(EMBEDDING_FAILED, error="boom")):
            self.run_analysis()
        row = self.stored()[str(self.audio)]
        self.assertEqual(row["embedding_status"], EMBEDDING_OK)
        self.assertEqual(row["embedding_recipe"], "old-recipe")
        self.assertIsNotNone(row["audio_embedding"])

        # A definitive result under the new recipe replaces the stale embedding.
        with patch.object(analyze, "extract_embeddings", return_value=EmbeddingResult(EMBEDDING_SKIPPED_TOO_SHORT)):
            self.run_analysis()
        row = self.stored()[str(self.audio)]
        self.assertEqual(row["embedding_status"], EMBEDDING_SKIPPED_TOO_SHORT)
        self.assertIsNone(row["audio_embedding"])

    def test_full_reanalysis_reuses_embedding_for_unchanged_audio(self):
        with patch.object(analyze, "extract_embeddings", return_value=_ok_embedding()):
            self.run_analysis()
        first = self.stored()[str(self.audio)]
        self.bump_mtime(self.audio)

        with patch.object(analyze, "extract_embeddings") as extract:
            self.run_analysis()
        extract.assert_not_called()
        row = self.stored()[str(self.audio)]
        self.assertNotEqual(row["analyzed_at"], first["analyzed_at"])
        self.assertEqual(row["embedding_status"], EMBEDDING_OK)
        np.testing.assert_array_equal(row["audio_embedding"], first["audio_embedding"])

    def test_changed_audio_recomputes_embedding(self):
        with patch.object(analyze, "extract_embeddings", return_value=_ok_embedding()):
            self.run_analysis()
        _write_sine_wav(self.audio, freq=880.0)
        self.bump_mtime(self.audio)
        with patch.object(analyze, "extract_embeddings", return_value=_ok_embedding(2.0)) as extract:
            self.run_analysis()
        extract.assert_called_once()
        self.assertAlmostEqual(float(self.stored()[str(self.audio)]["audio_embedding"][0]), 0.2, places=6)

    def test_scoped_run_preserves_rows_outside_folder(self):
        a, b = self.music / "a", self.music / "b"
        a.mkdir()
        b.mkdir()
        self.audio.unlink()
        one, two, three = a / "one.wav", a / "two.wav", b / "three.wav"
        _write_sine_wav(one, freq=330.0)
        _write_sine_wav(two, freq=440.0)
        _write_sine_wav(three, freq=550.0)
        self.run_analysis()
        two.unlink()
        three.unlink()

        self.run_analysis(a)
        self.assertEqual(set(self.stored()), {str(one), str(three)})
        self.run_analysis()
        self.assertEqual(set(self.stored()), {str(one)})

    def test_moved_file_outside_scope_drops_source_row(self):
        a, b = self.music / "a", self.music / "b"
        a.mkdir()
        b.mkdir()
        source = a / "song.wav"
        self.audio.rename(source)
        self.run_analysis()
        target = b / "song.wav"
        shutil.move(source, target)

        with patch.object(analyze, "_run_task") as run:
            self.run_analysis(b)
        run.assert_not_called()
        rows = self.stored()
        self.assertEqual(set(rows), {str(target)})
        self.assertEqual(rows[str(target)]["embedding_status"], EMBEDDING_SKIPPED_TOO_SHORT)

    def test_preflight_failure_leaves_store_unchanged(self):
        self.run_analysis()
        original = self.output.read_bytes()
        self.bump_mtime(self.audio)
        with patch("lib.embeddings.preflight", return_value=["model broken"]), \
             patch.object(analyze, "_run_task") as run:
            with self.assertRaisesRegex(analyze.PreflightError, "model broken"):
                self.run_analysis()
            self.run_analysis(dry_run=True)
        run.assert_not_called()
        self.assertEqual(self.output.read_bytes(), original)

    def test_interrupt_saves_progress_and_resumes(self):
        second = self.music / "z_second.wav"
        _write_sine_wav(second, freq=660.0)
        real_run = analyze._run_task
        calls = []

        def interrupt_on_second(task):
            calls.append(task)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real_run(task)

        with patch.object(analyze, "_run_task", side_effect=interrupt_on_second):
            with self.assertRaises(KeyboardInterrupt):
                self.run_analysis(checkpoint_seconds=3600)
        saved = self.stored()
        self.assertEqual(set(saved), {str(self.audio)})

        with patch.object(analyze, "_run_task", wraps=real_run) as run:
            self.run_analysis()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1], second)
        resumed = self.stored()
        self.assertEqual(set(resumed), {str(self.audio), str(second)})
        self.assertEqual(resumed[str(self.audio)]["analyzed_at"], saved[str(self.audio)]["analyzed_at"])

    def test_checkpoints_follow_interval(self):
        _write_sine_wav(self.music / "second.wav", freq=660.0)
        for seconds, expected_saves in ((0, 3), (3600, 1)):
            with self.subTest(checkpoint_seconds=seconds):
                self.output.unlink(missing_ok=True)
                with patch.object(analyze, "_save_dataframe", wraps=analyze._save_dataframe) as save:
                    self.run_analysis(checkpoint_seconds=seconds)
                self.assertEqual(save.call_count, expected_saves)

    def test_parallel_run_matches_serial(self):
        _write_sine_wav(self.music / "second.wav", freq=660.0)
        self.run_analysis(workers=2)
        rows = self.stored()
        self.assertEqual(len(rows), 2)
        for row in rows.values():
            self.assertIsNone(row["error"])
            self.assertEqual(row["embedding_status"], EMBEDDING_SKIPPED_TOO_SHORT)

    def test_worker_failure_results(self):
        embedding = analyze._worker_failure_result((analyze.EMBEDDING_ONLY, self.audio, None), RuntimeError("x"))
        self.assertEqual(embedding["embedding_status"], EMBEDDING_FAILED)
        self.assertIn("worker_error", embedding["embedding_error"])
        full = analyze._worker_failure_result((analyze.FULL_ANALYSIS, self.audio, None), RuntimeError("x"))
        self.assertIn("worker_error", full["error"])
        self.assertEqual(full["schema_version"], analyze.FEATURE_SCHEMA_VERSION)

    def test_failed_full_recompute_keeps_previous_row(self):
        good = {"path": "p", "error": None, "schema_version": 9}
        rows = {"p": good}
        self.assertTrue(analyze._merge_full_result(rows, "p", {"path": "p", "error": "load_failed: x"}))
        self.assertIs(rows["p"], good)
        rows = {}
        self.assertTrue(analyze._merge_full_result(rows, "p", {"path": "p", "error": "load_failed: x"}))
        self.assertEqual(rows["p"]["error"], "load_failed: x")


if __name__ == "__main__":
    unittest.main()

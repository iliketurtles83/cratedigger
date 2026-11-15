"""Tests for lib/tempo.py and scripts/repair_bpm.py."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

import config
from lib.tempo import (
    TempoResult,
    apply_genre_octave_fold,
    estimate_tempo,
    is_valid_bpm,
    required_confidence_for_genre,
    sanitize_bpm_tag,
)
from scripts import repair_bpm


class TestTempoHelpers(unittest.TestCase):
    def test_is_valid_bpm_bounds(self):
        self.assertFalse(is_valid_bpm(None))
        self.assertFalse(is_valid_bpm(0))
        self.assertFalse(is_valid_bpm(1.0))
        self.assertFalse(is_valid_bpm(34.9))
        self.assertTrue(is_valid_bpm(35.0))
        self.assertTrue(is_valid_bpm(128.5))
        self.assertTrue(is_valid_bpm(220.0))
        self.assertFalse(is_valid_bpm(220.1))
        self.assertFalse(is_valid_bpm(12704.0))
        self.assertFalse(is_valid_bpm(float("nan")))

    def test_sanitize_bpm_tag(self):
        self.assertIsNone(sanitize_bpm_tag(None))
        self.assertIsNone(sanitize_bpm_tag(""))
        self.assertIsNone(sanitize_bpm_tag("1"))
        self.assertIsNone(sanitize_bpm_tag("12704"))
        self.assertIsNone(sanitize_bpm_tag("abc"))
        self.assertEqual(sanitize_bpm_tag("099.74"), 99.74)
        self.assertEqual(sanitize_bpm_tag("140"), 140.0)

    def test_required_confidence_for_genre(self):
        self.assertEqual(required_confidence_for_genre("Rock"), 0.5)
        self.assertEqual(required_confidence_for_genre("Chill / Meditation"), 0.8)
        self.assertEqual(required_confidence_for_genre("Spoken Word"), 0.8)

    def test_apply_genre_octave_fold(self):
        self.assertEqual(apply_genre_octave_fold(86.5, "Electronic / Drum & Bass"), 173.0)
        self.assertEqual(apply_genre_octave_fold(172.0, "Electronic / Drum & Bass"), 172.0)
        self.assertEqual(apply_genre_octave_fold(86.5, "Hip-Hop"), 86.5)

    def test_estimate_tempo_filters_low_confidence(self):
        with patch("lib.tempo._estimate_with_essentia") as mock_es:
            mock_es.return_value = TempoResult(bpm=None, confidence=0.12, method="essentia_rhythm2013_mf")
            res = estimate_tempo(audio=np.zeros(44100 * 3, dtype=np.float32), sr=44100)
        self.assertIsNone(res.bpm)
        self.assertEqual(res.confidence, 0.12)

    def test_estimate_tempo_short_audio_returns_none(self):
        short_sig = np.zeros(44100 // 2, dtype=np.float32)
        res = estimate_tempo(audio=short_sig, sr=44100)
        self.assertIsNone(res.bpm)


class TestRepairBpmScript(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.album = self.root / "A" / "Artist" / "Artist - Album (2020)"
        self.album.mkdir(parents=True)
        self.mp3_corrupt = self.album / "01 - Artist - Corrupt.mp3"
        self.mp3_beatless = self.album / "02 - Artist - Beatless.mp3"
        self.mp3_mismatch = self.album / "03 - Artist - Mismatch.mp3"
        self.mp3_ok = self.album / "04 - Artist - Ok.mp3"
        self.ogg_skip = self.album / "05 - Artist - WholeHash.ogg"
        for p in (self.mp3_corrupt, self.mp3_beatless, self.mp3_mismatch, self.mp3_ok, self.ogg_skip):
            p.write_bytes(b"")

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_skips_ogg_and_identifies_repairs(self):
        tag_map = {
            self.mp3_corrupt: {"bpm": "12704", "genre": "Hip-Hop"},
            self.mp3_beatless: {"bpm": "140", "genre": "Chill / Meditation"},
            self.mp3_mismatch: {"bpm": "172", "genre": "Hip-Hop"},
            self.mp3_ok: {"bpm": "126", "genre": "House"},
            self.ogg_skip: {"bpm": "172", "genre": "Hip-Hop"},
        }
        est_map = {
            self.mp3_corrupt: TempoResult(bpm=88.5, confidence=3.1, method="essentia_rhythm2013_mf"),
            self.mp3_beatless: TempoResult(bpm=None, confidence=-0.1, method="essentia_rhythm2013_mf"),
            self.mp3_mismatch: TempoResult(bpm=85.5, confidence=3.7, method="essentia_rhythm2013_mf"),
            self.mp3_ok: TempoResult(bpm=125.0, confidence=3.0, method="essentia_rhythm2013_mf"),
        }

        with patch.object(config, "MUSIC_ROOT", self.root), \
             patch.object(repair_bpm, "read_tags", side_effect=lambda p: tag_map[p]), \
             patch.object(repair_bpm, "estimate_tempo", side_effect=lambda p, **_: est_map[p]), \
             patch.object(repair_bpm, "write_tags") as mock_write:
            summary = repair_bpm.run_repair(self.root, dry_run=True)

        mock_write.assert_not_called()
        self.assertEqual(summary["skipped_whole_hash"], 1)
        self.assertEqual(summary["scanned"], 4)
        by_name = {Path(c["path"]).name: c for c in summary["changes"]}
        self.assertEqual(by_name["01 - Artist - Corrupt.mp3"]["action"], "replace_corrupt")
        self.assertEqual(by_name["01 - Artist - Corrupt.mp3"]["new_bpm"], "88")
        self.assertEqual(by_name["02 - Artist - Beatless.mp3"]["action"], "clear_beatless")
        self.assertEqual(by_name["02 - Artist - Beatless.mp3"]["new_bpm"], "")
        self.assertEqual(by_name["03 - Artist - Mismatch.mp3"]["action"], "update_mismatch")
        self.assertEqual(by_name["03 - Artist - Mismatch.mp3"]["new_bpm"], "86")
        self.assertNotIn("04 - Artist - Ok.mp3", by_name)

    def test_live_mode_writes_only_non_whole_hash_repairs(self):
        tag_map = {
            self.mp3_corrupt: {"bpm": "1", "genre": "Ambient"},
        }
        with patch.object(config, "MUSIC_ROOT", self.root), \
             patch.object(repair_bpm, "read_tags", side_effect=lambda p: tag_map[p]), \
             patch.object(repair_bpm, "estimate_tempo", return_value=TempoResult(bpm=None, confidence=0.1, method="essentia_rhythm2013_mf")), \
             patch.object(repair_bpm, "write_tags") as mock_write:
            summary = repair_bpm.run_repair(self.root, dry_run=False, limit=1)

        self.assertEqual(summary["counts"].get("clear_corrupt"), 1)
        mock_write.assert_called_once_with(self.mp3_corrupt, {"bpm": ""}, dry_run=False)


@unittest.skipUnless(
    os.environ.get("CRATEDIGGER_RUN_REAL_AUDIO_TESTS") == "1",
    "Set CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1 to test tempo estimation on real library files.",
)
class TestTempoRealAudio(unittest.TestCase):
    def test_known_tracks_tempo_and_beatless_gating(self):
        beastie = Path("/media/jack/buffer/audio/B/Beastie Boys/Beastie Boys - Check Your Head (1992)/07 - Beastie Boys - So What'cha Want.mp3")
        if beastie.exists():
            res = estimate_tempo(beastie, genre="Hip-Hop")
            self.assertIsNotNone(res.bpm)
            self.assertAlmostEqual(res.bpm, 85.5, delta=3.0)
            self.assertGreater(res.confidence or 0.0, 1.5)

        meditation = Path("/media/jack/buffer/audio/H/Headspace/Singles/4 - Good Night/Falling back to sleep/Single - Falling back to sleep 3min.mp3")
        if meditation.exists():
            res = estimate_tempo(meditation, genre="Chill / Meditation")
            self.assertIsNone(res.bpm)


if __name__ == "__main__":
    unittest.main()

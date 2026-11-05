import tempfile
import unittest
from pathlib import Path

from scripts.quarantine_album_duplicates import (
    quarantine_album_duplicates,
    score_track_candidate,
)


class TestQuarantineAlbumDuplicates(unittest.TestCase):
    def test_score_track_candidate(self):
        # Numbered vs unnumbered
        p_num = Path("02 - Artist - Title.mp3")
        p_unnum = Path("Artist - Title.mp3")
        self.assertGreater(score_track_candidate(p_num), score_track_candidate(p_unnum))

        # Placeholder vs valid
        p_placeholder = Path("05 - Artist - AudioTrack 11.mp3")
        p_valid = Path("11 - Artist - Approaching Song.mp3")
        self.assertGreater(score_track_candidate(p_valid), score_track_candidate(p_placeholder))

        # Straight vs curly quote
        p_straight = Path("02 - Artist - You're Right.mp3")
        p_curly = Path("02 - Artist - You’re Right.mp3")
        self.assertGreater(score_track_candidate(p_straight), score_track_candidate(p_curly))

        # Standard feat. vs ft.
        p_feat = Path("03 - Artist feat. Guest - Song.mp3")
        p_ft = Path("03 - Artist ft. Guest - Song.mp3")
        self.assertGreater(score_track_candidate(p_feat), score_track_candidate(p_ft))

    def test_quarantine_simulation_and_execution(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            music_root = Path(tmp_dir) / "audio"
            quarantine_root = music_root / "0review" / "duplicates"

            album_dir = music_root / "A" / "Artist" / "Artist - Album (2020)"
            album_dir.mkdir(parents=True)

            # Two files with identical audio content
            f_keep = album_dir / "01 - Artist - Song.mp3"
            f_dup = album_dir / "Artist - Song.mp3"

            # Create identical audio content (>= 64KB for audiohash)
            dummy_audio = b"\xff\xfb\x90\x64" + b"\x00" * 70000
            f_keep.write_bytes(dummy_audio)
            f_dup.write_bytes(dummy_audio)

            # 1. Dry run
            result_dry = quarantine_album_duplicates(
                music_root,
                quarantine_root,
                dry_run=True,
                fix=False,
            )
            self.assertEqual(result_dry["quarantined_count"], 1)
            self.assertTrue(f_keep.exists())
            self.assertTrue(f_dup.exists())

            # 2. Live execution
            result_live = quarantine_album_duplicates(
                music_root,
                quarantine_root,
                dry_run=False,
                fix=True,
            )
            self.assertEqual(result_live["quarantined_count"], 1)
            self.assertTrue(f_keep.exists())
            self.assertFalse(f_dup.exists())

            # Verify quarantined file exists in quarantine directory
            quarantined_target = quarantine_root / "A" / "Artist" / "Artist - Album (2020)" / "Artist - Song.mp3"
            self.assertTrue(quarantined_target.exists())
            self.assertEqual(quarantined_target.read_bytes(), dummy_audio)


if __name__ == "__main__":
    unittest.main()

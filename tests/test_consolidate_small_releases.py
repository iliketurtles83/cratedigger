import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.consolidate_small_releases import (
    is_complete_release,
    consolidate_small_releases,
)
import config
from lib.tags import read_tags, write_tags


class TestConsolidateSmallReleases(unittest.TestCase):
    def test_complete_tagged_total(self):
        folder = Path("/music/Artist - Release")
        audio_files = [Path("01.mp3"), Path("02.mp3")]
        file_tags = [{"track": "1/2"}, {"track": "2/2"}]
        is_comp, reason = is_complete_release(folder, audio_files, file_tags)
        self.assertTrue(is_comp)
        self.assertIn("complete_tagged_total", reason)

    def test_incomplete_tagged_total(self):
        folder = Path("/music/Artist - Album")
        audio_files = [Path("01.mp3")]
        file_tags = [{"track": "1/12"}]
        is_comp, reason = is_complete_release(folder, audio_files, file_tags)
        self.assertFalse(is_comp)
        self.assertIn("incomplete_tagged_total", reason)

    def test_incomplete_track_numbering(self):
        folder = Path("/music/Artist - Album")
        audio_files = [Path("06.mp3")]
        file_tags = [{"track": "6"}]
        is_comp, reason = is_complete_release(folder, audio_files, file_tags)
        self.assertFalse(is_comp)
        self.assertIn("incomplete_track_numbering", reason)

    def test_ep_keyword_folder(self):
        folder = Path("/music/Artist - Cool Single [EP]")
        audio_files = [Path("01.mp3")]
        file_tags = [{"track": "1"}]
        is_comp, reason = is_complete_release(folder, audio_files, file_tags)
        self.assertTrue(is_comp)
        self.assertEqual(reason, "ep_keyword_in_folder")

    def test_ep_keyword_album_tag(self):
        folder = Path("/music/Artist - Some Release")
        audio_files = [Path("01.mp3")]
        file_tags = [{"track": "1", "album": "Some Release EP"}]
        is_comp, reason = is_complete_release(folder, audio_files, file_tags)
        self.assertTrue(is_comp)
        self.assertEqual(reason, "ep_keyword_in_album_tag")


if __name__ == "__main__":
    unittest.main()

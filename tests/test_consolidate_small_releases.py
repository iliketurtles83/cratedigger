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

    def test_consolidate_strips_trailing_dot_from_artist_folder(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            comp_dir = root / "0compilations" / "Some Sampler (2000)"
            comp_dir.mkdir(parents=True)
            track_path = comp_dir / "01 - A.P.E. - No. 5.mp3"
            track_path.write_bytes(b"fake")

            with patch.object(config, "MUSIC_ROOT", root), patch(
                "scripts.consolidate_small_releases.read_tags",
                return_value={"artist": "A.P.E.", "title": "No. 5", "track": "1/12"},
            ), patch("scripts.consolidate_small_releases.write_tags"), patch(
                "scripts.consolidate_small_releases.update_downstream_paths"
            ):
                consolidate_small_releases(
                    dry_run=False,
                    folder="0compilations",
                )

            expected = root / "A" / "A.P.E" / "0singles" / "A.P.E. - No. 5.mp3"
            self.assertTrue(expected.exists())
            self.assertFalse((root / "A" / "A.P.E.").exists())


if __name__ == "__main__":
    unittest.main()

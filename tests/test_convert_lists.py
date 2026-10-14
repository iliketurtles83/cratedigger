"""Tests for scripts/convert_lists.py."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.convert_lists import convert_list_folder


class ConvertListsTests(unittest.TestCase):
    def test_convert_list_folder_generates_m3u8_and_retags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            list_folder = tmp_root / "0compilations" / "The Pitchfork 500"
            list_folder.mkdir(parents=True)
            pl_dir = tmp_root / "Playlists"

            t1 = list_folder / "01 - Talking Heads - Psycho Killer.mp3"
            t1.touch()

            # Mock audio_hash and read_tags / write_tags
            fake_tags = {
                "artist": "Talking Heads",
                "title": "Psycho Killer",
                "album": "The Pitchfork 500",
                "compilation": "1",
                "albumartist": "Various Artists",
            }
            written_tags: dict[str, str | None] = {}

            def mock_read(p):
                return dict(fake_tags)

            def mock_write(p, updates, dry_run=False):
                written_tags.update(updates)
                return updates

            with patch("scripts.convert_lists.audio_hash", return_value="hash_t1"), \
                 patch("scripts.convert_lists.read_tags", side_effect=mock_read), \
                 patch("scripts.convert_lists.write_tags", side_effect=mock_write):

                res = convert_list_folder(
                    list_folder,
                    pl_dir,
                    tmp_root,
                    dry_run=False,
                    retag=True,
                )

            self.assertIsNotNone(res)
            tracks_count, dupes_count, unique_count = res
            self.assertEqual(tracks_count, 1)
            self.assertEqual(dupes_count, 0)
            self.assertEqual(unique_count, 1)

            # Playlist was written
            pl_file = pl_dir / "The Pitchfork 500.m3u8"
            self.assertTrue(pl_file.exists())
            content = pl_file.read_text(encoding="utf-8")
            self.assertIn("#EXTM3U", content)
            self.assertIn("01 - Talking Heads - Psycho Killer.mp3", content)

            # Tags were cleaned: list album cleared, compilation cleared, albumartist updated
            self.assertIsNone(written_tags.get("album"))
            self.assertEqual(written_tags.get("compilation"), "0")
            self.assertEqual(written_tags.get("albumartist"), "Talking Heads")


if __name__ == "__main__":
    unittest.main()

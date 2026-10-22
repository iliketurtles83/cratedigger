"""Tests for scripts/retag_lists.py."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.retag_lists import retag_list_folder


class RetagListsTests(unittest.TestCase):
    def test_retag_as_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            list_folder = tmp_root / "The Pitchfork 500"
            list_folder.mkdir(parents=True)

            t1 = list_folder / "01 - Talking Heads - Psycho Killer.mp3"
            t1.touch()

            fake_tags = {
                "artist": "Talking Heads",
                "title": "Psycho Killer",
                "album": None,
                "compilation": "0",
                "albumartist": "Talking Heads",
                "track": "1/500",
            }
            written_tags: dict[str, str | None] = {}

            def mock_read(p):
                return dict(fake_tags)

            def mock_write(p, updates, dry_run=False):
                written_tags.update(updates)
                return updates

            with patch("scripts.retag_lists.read_tags", side_effect=mock_read), \
                 patch("scripts.retag_lists.write_tags", side_effect=mock_write):

                total, modified = retag_list_folder(
                    list_folder,
                    {},
                    dry_run=False,
                    as_compilation=True,
                )

            self.assertEqual(total, 1)
            self.assertEqual(modified, 1)
            self.assertEqual(written_tags.get("album"), "The Pitchfork 500")
            self.assertEqual(written_tags.get("albumartist"), "Various Artists")
            self.assertEqual(written_tags.get("compilation"), "1")

    def test_retag_as_compilation_custom_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            list_folder = tmp_root / "Rolling Stone 500"
            list_folder.mkdir(parents=True)

            t1 = list_folder / "01 - Bob Dylan - Like a Rolling Stone.mp3"
            t1.touch()

            fake_tags = {
                "artist": "Bob Dylan",
                "title": "Like a Rolling Stone",
                "album": "Old Album",
                "compilation": "0",
                "albumartist": "Bob Dylan",
            }
            written_tags: dict[str, str | None] = {}

            def mock_read(p):
                return dict(fake_tags)

            def mock_write(p, updates, dry_run=False):
                written_tags.update(updates)
                return updates

            with patch("scripts.retag_lists.read_tags", side_effect=mock_read), \
                 patch("scripts.retag_lists.write_tags", side_effect=mock_write):

                total, modified = retag_list_folder(
                    list_folder,
                    {},
                    dry_run=False,
                    as_compilation=True,
                    album_title="Rolling Stone 500 Greatest Songs",
                )

            self.assertEqual(total, 1)
            self.assertEqual(modified, 1)
            self.assertEqual(written_tags.get("album"), "Rolling Stone 500 Greatest Songs")
            self.assertEqual(written_tags.get("albumartist"), "Various Artists")
            self.assertEqual(written_tags.get("compilation"), "1")


if __name__ == "__main__":
    unittest.main()

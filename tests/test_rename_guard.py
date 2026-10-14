"""Regression tests for 02_rename.py placeholder handling."""

import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

rename_mod = importlib.import_module("02_rename")


class PlaceholderGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.album = Path(self.tempdir.name) / "Artist - Album (2000)"
        self.album.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _rename(self, name: str, tags: dict[str, str | None]) -> list[dict]:
        path = self.album / name
        path.write_bytes(b"dummy")
        review_items: list[dict] = []
        rename_mod.rename_file(
            path,
            tags=tags,
            use_disc=False,
            dry_run=False,
            review_items=review_items,
        )
        return review_items

    def test_placeholder_tags_are_not_written_to_filename(self) -> None:
        review_items = self._rename(
            "track01.mp3",
            {"artist": "New Artist", "title": "Track 01", "track": "1"},
        )

        self.assertTrue((self.album / "track01.mp3").exists())
        self.assertFalse(list(self.album.glob("*New Artist*")))
        self.assertEqual(len(review_items), 1)
        self.assertEqual(review_items[0]["reason"], "placeholder_tags")
        self.assertEqual(sorted(review_items[0]["fields"]), ["artist", "title"])

    def test_placeholder_artist_alone_blocks_rename(self) -> None:
        review_items = self._rename(
            "02 - unknown.mp3",
            {"artist": "Unknown Artist", "title": "Real Song", "track": "2"},
        )

        self.assertTrue((self.album / "02 - unknown.mp3").exists())
        self.assertEqual(review_items[0]["fields"], ["artist"])

    def test_various_artists_is_not_a_placeholder(self) -> None:
        review_items = self._rename(
            "untidy.mp3",
            {"artist": "Various Artists", "title": "Real Song", "track": "4"},
        )

        self.assertEqual(review_items, [])
        self.assertTrue((self.album / "04 - Various Artists - Real Song.mp3").exists())

    def test_clean_tags_still_rename(self) -> None:
        review_items = self._rename(
            "untidy.mp3",
            {"artist": "Real Artist", "title": "Real Song", "track": "3"},
        )

        self.assertEqual(review_items, [])
        self.assertTrue((self.album / "03 - Real Artist - Real Song.mp3").exists())


if __name__ == "__main__":
    unittest.main()

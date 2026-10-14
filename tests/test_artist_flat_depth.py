"""Regression tests for artist_flat folder normalisation depth/bucketing.

An artist_flat folder directly under a letter bucket is an *artist* root, not
an album.  Renaming it in place produced album-shaped folders at artist depth,
bucketed by the album title instead of the artist, e.g.
``R/Easy Star All-Stars - Radiodread (2006)``.
"""

import importlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config

folders_mod = importlib.import_module("03_folders")


def _tags(artist: str, album: str, year: str, title: str) -> dict[str, str | None]:
    return {
        "artist": artist,
        "albumartist": artist,
        "album": album,
        "year": year,
        "title": title,
    }


class ArtistFlatDepthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "audio"
        self.root.mkdir(parents=True)
        self.patcher = patch.object(config, "MUSIC_ROOT", self.root)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tempdir.cleanup()

    def _build(self, relative: str, tags: dict[str, str | None]) -> Path:
        folder = self.root / relative
        folder.mkdir(parents=True)
        (folder / "01 - Song.mp3").write_bytes(b"dummy")
        return folder

    def _handle(self, folder: Path, tags: dict[str, str | None]) -> list[dict]:
        review_items: list[dict] = []
        with patch.object(folders_mod, "read_tags", return_value=tags):
            folders_mod._handle_artist_flat(
                folder, dry_run=False, review_items=review_items
            )
        return review_items

    def test_bucket_level_flat_folder_is_nested_under_artist(self) -> None:
        tags = _tags("Aphex Twin", "Drukqs", "2001", "Vordhosbn")
        folder = self._build("A/Aphex Twin", tags)

        self._handle(folder, tags)

        expected = self.root / "A" / "Aphex Twin" / "Aphex Twin - Drukqs (2001)"
        self.assertTrue(expected.is_dir())
        self.assertTrue((expected / "01 - Song.mp3").exists())
        # No album-shaped folder left at artist depth
        self.assertEqual(
            [p.name for p in (self.root / "A").iterdir()], ["Aphex Twin"]
        )

    def test_misbucketed_flat_folder_is_rebucketed_by_artist(self) -> None:
        # Folder was named after the album, so it landed in the 'R' bucket
        tags = _tags("Easy Star All-Stars", "Radiodread", "2006", "Airbag")
        folder = self._build("R/Radiodread", tags)

        self._handle(folder, tags)

        expected = (
            self.root / "E" / "Easy Star All-Stars"
            / "Easy Star All-Stars - Radiodread (2006)"
        )
        self.assertTrue(expected.is_dir())
        self.assertTrue((expected / "01 - Song.mp3").exists())
        self.assertFalse((self.root / "R" / "Radiodread").exists())

    def test_article_is_stripped_for_bucketing_only(self) -> None:
        tags = _tags("The Prodigy", "Music for the Jilted Generation", "1994", "Voodoo People")
        folder = self._build("T/The Prodigy", tags)

        self._handle(folder, tags)

        expected = (
            self.root / "P" / "The Prodigy"
            / "The Prodigy - Music for the Jilted Generation (1994)"
        )
        self.assertTrue(expected.is_dir())

    def test_album_depth_flat_folder_is_renamed_in_place(self) -> None:
        tags = _tags("Kylesa", "Spiral Shadow", "2010", "Tired Climb")
        folder = self._build("K/Kylesa/spiral shadow", tags)

        self._handle(folder, tags)

        expected = self.root / "K" / "Kylesa" / "Kylesa - Spiral Shadow (2010)"
        self.assertTrue(expected.is_dir())
        self.assertTrue((expected / "01 - Song.mp3").exists())

    def test_already_canonical_bucket_level_folder_is_untouched(self) -> None:
        tags = _tags("Kylesa", "Spiral Shadow", "2010", "Tired Climb")
        folder = self._build("K/Kylesa/Kylesa - Spiral Shadow (2010)", tags)

        review_items = self._handle(folder, tags)

        self.assertEqual(review_items, [])
        self.assertTrue(folder.is_dir())

    def test_relocation_is_recorded_for_downstream_sync(self) -> None:
        tags = _tags("Aphex Twin", "Drukqs", "2001", "Vordhosbn")
        folder = self._build("A/Aphex Twin", tags)
        file_renamed_map: dict[Path, Path] = {}

        with patch.object(folders_mod, "read_tags", return_value=tags):
            folders_mod._handle_artist_flat(
                folder,
                dry_run=False,
                review_items=[],
                file_renamed_map=file_renamed_map,
            )

        expected = (
            self.root / "A" / "Aphex Twin" / "Aphex Twin - Drukqs (2001)"
            / "01 - Song.mp3"
        )
        self.assertEqual(
            file_renamed_map.get(folder / "01 - Song.mp3"), expected
        )

    def test_dry_run_moves_nothing(self) -> None:
        tags = _tags("Aphex Twin", "Drukqs", "2001", "Vordhosbn")
        folder = self._build("A/Aphex Twin", tags)

        review_items: list[dict] = []
        with patch.object(folders_mod, "read_tags", return_value=tags):
            folders_mod._handle_artist_flat(
                folder, dry_run=True, review_items=review_items
            )

        self.assertTrue((folder / "01 - Song.mp3").exists())
        self.assertEqual(review_items[0]["reason"], "artist_flat_rename_proposal")
        self.assertEqual(
            review_items[0]["target"],
            str(self.root / "A" / "Aphex Twin" / "Aphex Twin - Drukqs (2001)"),
        )


if __name__ == "__main__":
    unittest.main()

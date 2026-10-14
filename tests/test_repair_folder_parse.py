"""Tests for scripts/repair_folder_parse.py."""

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config

_SPEC = importlib.util.spec_from_file_location(
    "repair_folder_parse",
    Path(__file__).resolve().parents[1] / "scripts" / "repair_folder_parse.py",
)
repair = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(repair)


class BuggyParseDetectionTests(unittest.TestCase):
    def test_detects_values_the_old_regex_explains(self) -> None:
        self.assertEqual(
            repair._buggy_parse("AC-DC - Stiff Upper Lip (2000)"),
            {"artist": "AC", "album": "DC - Stiff Upper Lip", "year": "2000"},
        )

    def test_unaffected_names_parse_identically(self) -> None:
        bad = repair._buggy_parse("Kylesa - Spiral Shadow (2010)")
        self.assertEqual(bad["artist"], "Kylesa")


class RepairTagsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "audio"
        self.album = self.root / "A" / "AC-DC" / "AC-DC - Stiff Upper Lip (2000)"
        self.album.mkdir(parents=True)
        self.track = self.album / "01 - AC-DC - Meltdown.mp3"
        self.track.write_bytes(b"dummy")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, tags: dict[str, str | None], *, dry_run: bool = False) -> list[tuple]:
        writes: list[tuple] = []
        with patch.object(repair, "read_tags", return_value=tags), \
             patch.object(repair, "write_tags",
                          side_effect=lambda p, t, dry_run: writes.append((p, t))):
            repair.repair_tags(self.root, dry_run=dry_run)
        return writes

    def test_truncated_albumartist_is_restored(self) -> None:
        writes = self._run({"artist": "AC/DC", "albumartist": "AC", "album": "Stiff Upper Lip"})

        self.assertEqual(len(writes), 1)
        # Tag spelling wins over the sanitised folder name
        self.assertEqual(writes[0][1], {"albumartist": "AC/DC"})

    def test_leaked_album_remainder_is_restored(self) -> None:
        writes = self._run({
            "artist": "AC/DC",
            "albumartist": "AC",
            "album": "DC - Stiff Upper Lip",
        })

        self.assertEqual(writes[0][1]["album"], "Stiff Upper Lip")

    def test_unrelated_albumartist_is_left_alone(self) -> None:
        writes = self._run({
            "artist": "AC/DC",
            "albumartist": "Some Other Band",
            "album": "Stiff Upper Lip",
        })

        self.assertEqual(writes, [])

    def test_correct_tags_are_idempotent(self) -> None:
        writes = self._run({
            "artist": "AC/DC",
            "albumartist": "AC/DC",
            "album": "Stiff Upper Lip",
        })

        self.assertEqual(writes, [])

    def test_folder_spelling_used_when_artist_tags_disagree(self) -> None:
        # A guest credit means the artist tag is not the album artist
        writes = self._run({
            "artist": "AC/DC feat. Someone",
            "albumartist": "AC",
            "album": "Stiff Upper Lip",
        })

        self.assertEqual(writes[0][1], {"albumartist": "AC-DC"})


class RepairStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "audio"
        self.root.mkdir(parents=True)
        self.patcher = patch.object(config, "MUSIC_ROOT", self.root)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.tempdir.cleanup()

    def test_stranded_album_is_nested_and_rebucketed(self) -> None:
        stranded = self.root / "R" / "Easy Star All-Stars - Radiodread (2006)"
        stranded.mkdir(parents=True)
        (stranded / "01 - Song.mp3").write_bytes(b"dummy")

        with patch.object(repair, "read_tags",
                          return_value={"albumartist": "Easy Star All-Stars"}):
            repair.repair_structure(self.root, dry_run=False, folder=None)

        expected = (
            self.root / "E" / "Easy Star All-Stars"
            / "Easy Star All-Stars - Radiodread (2006)"
        )
        self.assertTrue((expected / "01 - Song.mp3").exists())
        self.assertFalse(stranded.exists())

    def test_properly_nested_album_is_untouched(self) -> None:
        album = self.root / "K" / "Kylesa" / "Kylesa - Spiral Shadow (2010)"
        album.mkdir(parents=True)
        (album / "01 - Song.mp3").write_bytes(b"dummy")

        with patch.object(repair, "read_tags", return_value={"albumartist": "Kylesa"}):
            moved = repair.repair_structure(self.root, dry_run=False, folder=None)

        self.assertEqual(moved, 0)
        self.assertTrue(album.exists())

    def test_artist_folder_with_album_subdirs_is_not_stranded(self) -> None:
        artist = self.root / "B" / "Bob Dylan - Live"
        album = artist / "Bob Dylan - Blonde on Blonde (1966)"
        album.mkdir(parents=True)
        (album / "01 - Song.mp3").write_bytes(b"dummy")

        self.assertEqual(repair._stranded_album_folders(self.root, None), [])

    def test_dry_run_moves_nothing(self) -> None:
        stranded = self.root / "R" / "Easy Star All-Stars - Radiodread (2006)"
        stranded.mkdir(parents=True)
        (stranded / "01 - Song.mp3").write_bytes(b"dummy")

        with patch.object(repair, "read_tags",
                          return_value={"albumartist": "Easy Star All-Stars"}):
            moved = repair.repair_structure(self.root, dry_run=True, folder=None)

        self.assertEqual(moved, 1)
        self.assertTrue(stranded.exists())
        self.assertFalse((self.root / "E").exists())


if __name__ == "__main__":
    unittest.main()

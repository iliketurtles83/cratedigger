import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.parsers import sanitise_name, validate_albumartist
from lib.tags import read_tags, write_tags

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("tag_module", REPO_ROOT / "01_tag.py")
tag_module = importlib.util.module_from_spec(_spec)
sys.modules["tag_module"] = tag_module
_spec.loader.exec_module(tag_module)
_normalise_albumartist_consistency = tag_module._normalise_albumartist_consistency


class _FakeAudio:
    """Mock Mutagen File object for unit testing read_tags and write_tags."""

    def __init__(self, tags=None):
        self.tags = tags if tags is not None else {}
        self.save_called = False
        self.save_kwargs = {}

    def add_tags(self):
        if self.tags is None:
            self.tags = {}

    def save(self, **kwargs):
        self.save_called = True
        self.save_kwargs = kwargs


class FeedbackStandardsTests(unittest.TestCase):

    def test_validate_albumartist_rejects_numeric_track_leaks(self):
        # Numeric track number leak (like Kylesa where TPE2 = 01)
        self.assertIsNone(validate_albumartist("01", artist="Kylesa", track="1"))
        self.assertIsNone(validate_albumartist("02", artist="Kylesa", track="2"))
        self.assertIsNone(validate_albumartist("7", artist="Rammstein", track="7"))

        # Genuine numeric bands should be preserved
        self.assertEqual(validate_albumartist("666", artist="666"), "666")
        self.assertEqual(validate_albumartist("3", artist="3"), "3")
        self.assertEqual(validate_albumartist("213", artist="213"), "213")

    def test_validate_albumartist_rejects_urls_and_junk(self):
        self.assertIsNone(validate_albumartist("http://www.ansarimusic.net"))
        self.assertIsNone(validate_albumartist("https://bandcamp.com"))
        self.assertIsNone(validate_albumartist("www.artist.com"))
        self.assertIsNone(validate_albumartist("artist"))
        self.assertIsNone(validate_albumartist("-"))
        self.assertIsNone(validate_albumartist("   "))
        self.assertIsNone(validate_albumartist("none"))
        self.assertIsNone(validate_albumartist("n/a"))

        # Valid standard names
        self.assertEqual(validate_albumartist("Radiohead"), "Radiohead")
        self.assertEqual(validate_albumartist("Various Artists"), "Various Artists")
        self.assertEqual(validate_albumartist("Blink-182"), "Blink-182")

    def test_sanitise_name_leading_dots_underscores_escapes(self):
        # Leading dots (hidden folders/files on Linux/Android)
        self.assertEqual(sanitise_name("...Mixing (2004)"), "Mixing (2004)")
        self.assertEqual(sanitise_name(".hidden_track"), "hidden_track")

        # Escaped quotes
        self.assertEqual(sanitise_name(r"B-52\'s"), "B-52's")
        self.assertEqual(sanitise_name(r"Rock \'N\' Roll"), "Rock 'N' Roll")
        self.assertEqual(sanitise_name(r"Rock \"N\" Roll"), "Rock N Roll")

        # Underscores around titles
        self.assertEqual(sanitise_name("_Heroes_"), "Heroes")

    def test_tags_compilation_and_mbids_roundtrip_id3(self):
        audio = _FakeAudio()
        with patch("lib.tags.MutagenFile", return_value=audio):
            written = write_tags(
                Path("test.mp3"),
                {
                    "title": "Airbag",
                    "artist": "Radiohead",
                    "albumartist": "Radiohead",
                    "compilation": "1",
                    "mb_trackid": "e69cfd37-d3ea-4e02-b591-1a60ce5afa68",
                    "mb_albumid": "f5127092-231a-4c28-98e7-ff849fc35ec8",
                    "rating": "1.0",
                    "playcount": "15",
                },
                dry_run=False,
            )

        self.assertEqual(written["compilation"], "1")
        self.assertEqual(written["mb_trackid"], "e69cfd37-d3ea-4e02-b591-1a60ce5afa68")
        self.assertEqual(written["rating"], "1.0")
        self.assertEqual(written["playcount"], "15")
        self.assertEqual(audio.save_kwargs, {"v2_version": 4})

        # Test read_tags from mock
        with patch("lib.tags.MutagenFile", return_value=audio):
            tags = read_tags(Path("test.mp3"))

        self.assertEqual(tags["compilation"], "1")
        self.assertEqual(tags["mb_trackid"], "e69cfd37-d3ea-4e02-b591-1a60ce5afa68")
        self.assertEqual(tags["mb_albumid"], "f5127092-231a-4c28-98e7-ff849fc35ec8")
        self.assertEqual(tags["rating"], "1.0")
        self.assertEqual(tags["playcount"], "15")

    def test_albumartist_consistency_pass_enforces_release_artist(self):
        # 3 tracks on an album: track 1 & 2 have Gorillaz, track 3 has Gorillaz feat. Del with missing or bogus albumartist
        file_tags = [
            (Path("01.mp3"), {"artist": "Gorillaz", "albumartist": "Gorillaz", "compilation": "0"}),
            (Path("02.mp3"), {"artist": "Gorillaz", "albumartist": "Gorillaz", "compilation": "0"}),
            (Path("03.mp3"), {"artist": "Gorillaz feat. Del", "albumartist": "03", "compilation": None}),
        ]

        written_calls = []

        def mock_write(path, updates, dry_run=False):
            written_calls.append((path, updates))
            return updates

        with patch.object(tag_module, "write_tags", side_effect=mock_write):
            _normalise_albumartist_consistency(
                Path("G/Gorillaz/Gorillaz (2001)"),
                file_tags,
                is_compilation=False,
                dry_run=False,
            )

        # Track 3 had bogus albumartist "03", should be updated to "Gorillaz"
        self.assertEqual(len(written_calls), 1)
        self.assertEqual(written_calls[0][0], Path("03.mp3"))
        self.assertEqual(written_calls[0][1]["albumartist"], "Gorillaz")
        self.assertEqual(written_calls[0][1]["compilation"], "0")


if __name__ == "__main__":
    unittest.main()

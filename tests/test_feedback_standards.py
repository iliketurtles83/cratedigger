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


    def test_validate_albumartist_normalises_va_aliases(self):
        self.assertEqual(validate_albumartist("VA"), "Various Artists")
        self.assertEqual(validate_albumartist("V.A."), "Various Artists")
        self.assertEqual(validate_albumartist("v. a."), "Various Artists")
        self.assertEqual(validate_albumartist("various"), "Various Artists")
        self.assertEqual(validate_albumartist("Various Artists"), "Various Artists")

    def test_validate_year(self):
        from lib.parsers import validate_year
        # Valid 4-digit years
        self.assertEqual(validate_year("1998"), "1998")
        self.assertEqual(validate_year("2021"), "2021")
        self.assertEqual(validate_year("2011-05-12"), "2011")
        self.assertEqual(validate_year(1975), "1975")

        # 2-digit years expansion
        self.assertEqual(validate_year("98"), "1998")
        self.assertEqual(validate_year("99"), "1999")
        self.assertEqual(validate_year("04"), "2004")
        self.assertEqual(validate_year("20"), "1920")
        self.assertEqual(validate_year("15"), "2015")

        # 1-digit, 3-digit, future, or garbage years rejected
        self.assertIsNone(validate_year("8"))
        self.assertIsNone(validate_year("198"))
        self.assertIsNone(validate_year("2097"))
        self.assertIsNone(validate_year("3010"))
        self.assertIsNone(validate_year("1800"))
        self.assertIsNone(validate_year("unknown"))
        self.assertIsNone(validate_year(""))
        self.assertIsNone(validate_year(None))

    def test_artist_comparison_key(self):
        from lib.parsers import artist_comparison_key
        self.assertEqual(artist_comparison_key("The Prodigy"), "prodigy")
        self.assertEqual(artist_comparison_key("Prodigy"), "prodigy")
        self.assertEqual(artist_comparison_key("Simon & Garfunkel"), "simon and garfunkel")
        self.assertEqual(artist_comparison_key("Simon and Garfunkel"), "simon and garfunkel")
        self.assertEqual(artist_comparison_key("R.E.M."), "rem")
        self.assertEqual(artist_comparison_key("REM"), "rem")
        self.assertEqual(artist_comparison_key("The Arcade Fire"), "arcade fire")
        self.assertEqual(artist_comparison_key("Arcade Fire"), "arcade fire")

    def test_title_case_clean(self):
        from lib.parsers import title_case_clean
        self.assertEqual(title_case_clean("the passenger"), "The Passenger")
        self.assertEqual(title_case_clean("rage against the machine"), "Rage Against the Machine")
        self.assertEqual(title_case_clean("ABBA feat. cher"), "ABBA feat. Cher")
        self.assertEqual(title_case_clean("sympathy for the devil"), "Sympathy for the Devil")
        self.assertEqual(title_case_clean("AC/DC"), "AC/DC")
        self.assertEqual(title_case_clean("Simon & Garfunkel"), "Simon & Garfunkel")
        self.assertEqual(title_case_clean("Tom Petty And The Heartbreakers"), "Tom Petty and the Heartbreakers")
        self.assertEqual(title_case_clean("Wu-Tang Clan"), "Wu-Tang Clan")
        self.assertEqual(title_case_clean("Paul McCartney"), "Paul McCartney")

    def test_parse_filename_sanitises_tokens(self):
        from lib.parsers import parse_filename
        p1 = Path("01 - _Heroes_.mp3")
        parsed1 = parse_filename(p1)
        self.assertEqual(parsed1["title"], "Heroes")

        p2 = Path(r"01 - B-52\'s - Rock Lobster.mp3")
        parsed2 = parse_filename(p2)
        self.assertEqual(parsed2["artist"], "B-52's")
        self.assertEqual(parsed2["title"], "Rock Lobster")

    def test_artists_tag_roundtrip(self):
        audio = _FakeAudio()
        with patch("lib.tags.MutagenFile", return_value=audio):
            written = write_tags(
                Path("test.mp3"),
                {
                    "title": "Clint Eastwood",
                    "artist": "Gorillaz feat. Del the Funky Homosapien",
                    "artists": "Gorillaz / Del the Funky Homosapien",
                },
                dry_run=False,
            )
        self.assertEqual(written["artists"], "Gorillaz / Del the Funky Homosapien")

        with patch("lib.tags.MutagenFile", return_value=audio):
            tags = read_tags(Path("test.mp3"))
        self.assertEqual(tags["artists"], "Gorillaz / Del the Funky Homosapien")

    def test_rename_file_in_0singles_omits_track_number(self):
        import importlib
        mod = importlib.import_module("02_rename")
        p = Path("/music/A/Amyl and the Sniffers/0singles/05 - Amyl and the Sniffers - Hertz.mp3")
        tags = {"artist": "Amyl and the Sniffers", "title": "Hertz", "track": "5"}
        review_items = []

        with patch.object(Path, "rename") as mock_rename:
            mod.rename_file(p, tags=tags, use_disc=False, dry_run=False, review_items=review_items)
            mock_rename.assert_called_once_with(p.parent / "Amyl and the Sniffers - Hertz.mp3")


if __name__ == "__main__":
    unittest.main()

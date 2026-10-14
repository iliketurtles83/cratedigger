from pathlib import Path
import unittest

from lib.parsers import (
    build_filename,
    is_suspicious_tag,
    parse_compact_disc_track_candidate,
    parse_filename,
    parse_folder_name,
    sanitise_name,
    split_compact_disc_track,
    validate_albumartist,
)


class ParserTests(unittest.TestCase):
    def test_parse_filename_disc_track_with_artist(self) -> None:
        parsed = parse_filename(Path("1-07 - Tycho - Melanine.mp3"))

        self.assertEqual(
            parsed,
            {
                "artist": "Tycho",
                "title": "Melanine",
                "track": "7",
                "disc": "1",
            },
        )

    def test_parse_filename_normalises_zero_padded_disc_number(self) -> None:
        parsed = parse_filename(Path("01-07 - Tycho - Melanine.mp3"))

        self.assertEqual(parsed["disc"], "1")
        self.assertEqual(parsed["track"], "7")

    def test_parse_filename_falls_back_to_title_only(self) -> None:
        parsed = parse_filename(Path("Unstructured filename.mp3"))

        self.assertEqual(
            parsed,
            {
                "artist": None,
                "title": "Unstructured filename",
                "track": None,
                "disc": None,
            },
        )

    def test_parse_filename_keeps_compact_prefix_as_track_by_default(self) -> None:
        parsed = parse_filename(Path("101-gorillaz-68_state.mp3"))

        self.assertEqual(parsed["track"], "101")
        self.assertIsNone(parsed["disc"])

    def test_parse_folder_name(self) -> None:
        parsed = parse_folder_name("Leonard Cohen - Songs From a Room (1969)")

        self.assertEqual(
            parsed,
            {
                "artist": "Leonard Cohen",
                "album": "Songs From a Room",
                "year": "1969",
            },
        )

    def test_build_filename_uses_disc_prefix_when_present(self) -> None:
        filename = build_filename("7/10", "Tycho", "A/Walk", ext=".flac", disc="2/2")

        self.assertEqual(filename, "2-07 - Tycho - A-Walk.flac")

    def test_sanitise_name_strips_ripper_annotations_and_illegal_characters(self) -> None:
        value = sanitise_name("Track Name [raw] [www.example.com] / Demo?:*")

        self.assertEqual(value, "Track Name- Demo")

    def test_split_compact_disc_track(self) -> None:
        self.assertEqual(split_compact_disc_track("101"), ("1", "1"))
        self.assertEqual(split_compact_disc_track("1203"), ("12", "3"))
        self.assertEqual(split_compact_disc_track("021"), (None, None))

    def test_parse_compact_disc_track_candidate(self) -> None:
        parsed = parse_compact_disc_track_candidate(Path("101-gorillaz-68_state.mp3"))

        self.assertEqual(
            parsed,
            {
                "compact": "101",
                "disc": "1",
                "track": "1",
                "title": "gorillaz-68_state",
            },
        )

    def test_parse_compact_disc_track_candidate_requires_three_or_four_digits(self) -> None:
        parsed = parse_compact_disc_track_candidate(Path("21 Carousel.mp3"))

        self.assertEqual(
            parsed,
            {
                "compact": None,
                "disc": None,
                "track": None,
                "title": None,
            },
        )


    def test_is_suspicious_tag(self) -> None:
        # Ripper placeholder defaults
        self.assertTrue(is_suspicious_tag("Album Title", field="album"))
        self.assertTrue(is_suspicious_tag("album", field="album"))
        self.assertTrue(is_suspicious_tag("Unknown Album", field="album"))
        self.assertTrue(is_suspicious_tag("New Artist", field="artist"))
        self.assertTrue(is_suspicious_tag("Unknown Artist", field="artist"))
        self.assertTrue(is_suspicious_tag("Track 01", field="title"))
        self.assertTrue(is_suspicious_tag("Audio Track 05", field="title"))
        self.assertTrue(is_suspicious_tag("Unknown Track", field="title"))
        self.assertTrue(is_suspicious_tag("Untitled 1", field="title"))

        # Legitimate titles / artists
        self.assertFalse(is_suspicious_tag("Fast Car", field="title"))
        self.assertFalse(is_suspicious_tag("Track and Field", field="title"))
        self.assertFalse(is_suspicious_tag("Unknown Legend", field="title"))
        self.assertFalse(is_suspicious_tag("Neil Young", field="artist"))

        # Albumartist Various Artists exception
        self.assertFalse(is_suspicious_tag("Various Artists", field="albumartist"))
        self.assertTrue(is_suspicious_tag("Various Artists", field="artist"))

    def test_validate_albumartist_rejects_ripper_placeholders(self) -> None:
        self.assertIsNone(validate_albumartist("New Artist"))
        self.assertIsNone(validate_albumartist("Album Title"))
        self.assertIsNone(validate_albumartist("Unknown Album"))
        self.assertIsNone(validate_albumartist("01", artist="Kylesa"))
        self.assertEqual(validate_albumartist("666", artist="666"), "666")
        self.assertEqual(validate_albumartist("VA"), "Various Artists")
        self.assertEqual(validate_albumartist("Various Artists"), "Various Artists")

    def test_parse_folder_name_variations(self) -> None:
        # Standard: Artist - Album (Year)
        self.assertEqual(
            parse_folder_name("Leonard Cohen - Songs From a Room (1969)"),
            {"artist": "Leonard Cohen", "album": "Songs From a Room", "year": "1969"},
        )
        # Year - Album (Beets / cratedigger Section 7 layout)
        self.assertEqual(
            parse_folder_name("2010 - Spiral Shadow"),
            {"artist": None, "album": "Spiral Shadow", "year": "2010"},
        )
        # Album (Year)
        self.assertEqual(
            parse_folder_name("Spiral Shadow (2010)"),
            {"artist": None, "album": "Spiral Shadow", "year": "2010"},
        )
        # Artist - Album (no year)
        self.assertEqual(
            parse_folder_name("Kylesa - Spiral Shadow"),
            {"artist": "Kylesa", "album": "Spiral Shadow", "year": None},
        )


if __name__ == "__main__":
    unittest.main()

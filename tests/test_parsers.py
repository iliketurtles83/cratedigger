from pathlib import Path
import unittest

from lib.parsers import (
    build_filename,
    parse_compact_disc_track_candidate,
    parse_filename,
    parse_folder_name,
    sanitise_name,
    split_compact_disc_track,
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


if __name__ == "__main__":
    unittest.main()

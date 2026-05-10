from pathlib import Path
import unittest
from unittest.mock import patch

from mutagen.id3 import ID3, TBPM, TDRC, TIT2, TALB, TPE1, TPE2, TPOS, TRCK, TSRC

from lib.tags import read_tags, write_tags


class _FakeAudio:
    def __init__(self, tags: ID3 | None = None) -> None:
        self.tags = tags
        self.save_kwargs: dict[str, object] | None = None

    def add_tags(self) -> None:
        self.tags = ID3()

    def save(self, **kwargs: object) -> None:
        self.save_kwargs = kwargs


class TagIOIntegrationTests(unittest.TestCase):
    def test_read_tags_mp3_uses_real_id3_frames_and_normalises_values(self) -> None:
        tags = ID3()
        tags.add(TIT2(encoding=3, text=["Song Title"]))
        tags.add(TPE1(encoding=3, text=["Artist Name"]))
        tags.add(TPE2(encoding=3, text=["Album Artist"]))
        tags.add(TALB(encoding=3, text=["Album Title"]))
        tags.add(TDRC(encoding=3, text=["1999"]))
        tags.add(TBPM(encoding=3, text=["099.7"]))
        tags.add(TRCK(encoding=3, text=["04/09"]))
        tags.add(TPOS(encoding=3, text=["02/03"]))
        tags.add(TSRC(encoding=3, text=["USABC1234567"]))
        audio = _FakeAudio(tags)

        with patch("lib.tags.MutagenFile", return_value=audio):
            result = read_tags(Path("example.mp3"))

        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Song Title")
        self.assertEqual(result["artist"], "Artist Name")
        self.assertEqual(result["albumartist"], "Album Artist")
        self.assertEqual(result["album"], "Album Title")
        self.assertEqual(result["year"], "1999")
        self.assertEqual(result["bpm"], "100")
        self.assertEqual(result["track"], "4/9")
        self.assertEqual(result["disc"], "2/3")
        self.assertEqual(result["isrc"], "USABC1234567")

    def test_write_tags_mp3_writes_real_id3_frames_and_uses_v23_save(self) -> None:
        audio = _FakeAudio()

        with patch("lib.tags.MutagenFile", return_value=audio):
            written = write_tags(
                Path("example.mp3"),
                {
                    "title": "Song Title",
                    "artist": "Artist Name",
                    "albumartist": "Album Artist",
                    "album": "Album Title",
                    "year": "1999",
                    "bpm": "099.7",
                    "track": "04/09",
                    "disc": "02",
                    "isrc": "USABC1234567",
                },
                dry_run=False,
            )

        self.assertEqual(written["bpm"], "100")
        self.assertEqual(written["track"], "4/9")
        self.assertEqual(written["disc"], "2")
        self.assertIsNotNone(audio.tags)
        self.assertEqual(audio.tags["TBPM"].text, ["100"])
        self.assertEqual(audio.tags["TRCK"].text, ["4/9"])
        self.assertEqual(audio.tags["TPOS"].text, ["2"])
        self.assertEqual(audio.save_kwargs, {"v2_version": 3})

    def test_read_tags_m4a_uses_tuple_atoms_and_preserves_totals(self) -> None:
        audio = _FakeAudio(
            {
                "\xa9nam": ["Song Title"],
                "\xa9ART": ["Artist Name"],
                "aART": ["Album Artist"],
                "\xa9alb": ["Album Title"],
                "\xa9day": ["1999-05-01"],
                "trkn": [(4, 9)],
                "disk": [(2, 3)],
            }
        )

        with patch("lib.tags.MutagenFile", return_value=audio):
            result = read_tags(Path("example.m4a"))

        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Song Title")
        self.assertEqual(result["artist"], "Artist Name")
        self.assertEqual(result["albumartist"], "Album Artist")
        self.assertEqual(result["album"], "Album Title")
        self.assertEqual(result["year"], "1999-05-01")
        self.assertEqual(result["track"], "4/9")
        self.assertEqual(result["disc"], "2/3")

    def test_write_tags_m4a_preserves_track_and_disc_totals(self) -> None:
        audio = _FakeAudio({})

        with patch("lib.tags.MutagenFile", return_value=audio):
            written = write_tags(
                Path("example.m4a"),
                {
                    "title": "Song Title",
                    "track": "04/09",
                    "disc": "02/03",
                },
                dry_run=False,
            )

        self.assertEqual(written["track"], "4/9")
        self.assertEqual(written["disc"], "2/3")
        self.assertEqual(audio.tags["trkn"], [(4, 9)])
        self.assertEqual(audio.tags["disk"], [(2, 3)])
        self.assertEqual(audio.save_kwargs, {})


if __name__ == "__main__":
    unittest.main()
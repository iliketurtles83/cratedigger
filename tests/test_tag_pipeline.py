from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
import logging
import unittest
from unittest.mock import patch

import config


_TAGGER_PATH = Path(__file__).resolve().parents[1] / "01_tag.py"
_SPEC = spec_from_file_location("tagger_module", _TAGGER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Failed to load 01_tag.py for tests")
_TAGGER = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_TAGGER)


class TagPipelineIdempotenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.music_root = self.root / "music"
        self.music_root.mkdir(parents=True)

        self.album_folder = self.music_root / "rock" / "Artist - Good Album (1999)"
        self.album_folder.mkdir(parents=True)

        self.paths = [
            self.album_folder / "01 - Artist - Song One.mp3",
            self.album_folder / "02 - Artist - Song Two.mp3",
            self.album_folder / "03 - Artist - Song Three.mp3",
        ]
        for path in self.paths:
            path.touch()

        self.tag_store: dict[Path, dict[str, str | None]] = {
            self.paths[0]: {
                "title": "Song One",
                "artist": "Artist",
                "albumartist": None,
                "album": "Good Album",
                "year": "1999",
                "genre": "Rock",
                "bpm": None,
                "track": "1",
                "disc": None,
                "isrc": None,
            },
            self.paths[1]: {
                "title": "Song Two",
                "artist": "Artist",
                "albumartist": None,
                "album": "Wrong Album",
                "year": "2001",
                "genre": "Rock",
                "bpm": None,
                "track": "2",
                "disc": None,
                "isrc": None,
            },
            self.paths[2]: {
                "title": "Song Three",
                "artist": "Artist",
                "albumartist": None,
                "album": "Good Album",
                "year": "1999",
                "genre": "Rock",
                "bpm": None,
                "track": "3",
                "disc": None,
                "isrc": None,
            },
        }

        patchers = [
            patch.object(config, "MUSIC_ROOT", self.music_root),
            patch.object(config, "FOLDER_TO_GENRE", {"rock": "Rock"}),
            patch.object(config, "GENRE_FOLDERS", {"rock"}),
            patch.object(config, "SUBGENRE_BUCKETS", {}),
            patch.object(config, "SPECIAL_FOLDERS", {"0new", "0compilations", "0various"}),
            patch.object(config, "SKIP_FOLDERS", {"0videos"}),
            patch.object(config, "NO_BPM_FOLDERS", {"spoken", "0mixes"}),
            patch.object(config, "ALBUMARTIST_FOLDERS", {"0compilations", "0various", "soundtrack"}),
            patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}),
            patch.object(config, "COMPILATION_ARTIST_THRESHOLD", 3),
            patch.object(_TAGGER, "log", logging.getLogger("test_01_tag")),
            patch.object(_TAGGER, "read_tags", side_effect=self._read_tags),
            patch.object(_TAGGER, "write_tags", side_effect=self._write_tags),
            patch.object(_TAGGER, "detect_bpm", return_value=None),
        ]

        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.addCleanup(self.tempdir.cleanup)

    def _read_tags(self, path: Path) -> dict[str, str | None] | None:
        row = self.tag_store.get(path)
        return dict(row) if row is not None else None

    def _write_tags(
        self,
        path: Path,
        tags: dict[str, str | None],
        *,
        dry_run: bool = True,
    ) -> dict[str, str]:
        current = self.tag_store[path]
        written: dict[str, str] = {}
        for key, value in tags.items():
            if value is None:
                continue
            written[key] = value
            if not dry_run:
                current[key] = value
        return written

    def test_second_pass_has_no_new_review_after_live_normalisation(self) -> None:
        first_review = _TAGGER.walk_folder(
            self.music_root / "rock",
            dry_run=False,
            overwrite=False,
            fix_suspicious=False,
            fix_track_mismatch=False,
            no_bpm=True,
            no_mb=True,
            no_consistency=False,
        )

        self.assertEqual(first_review, [])
        self.assertEqual(self.tag_store[self.paths[1]]["album"], "Good Album")
        self.assertEqual(self.tag_store[self.paths[1]]["year"], "1999")

        second_review = _TAGGER.walk_folder(
            self.music_root / "rock",
            dry_run=True,
            overwrite=False,
            fix_suspicious=False,
            fix_track_mismatch=False,
            no_bpm=True,
            no_mb=True,
            no_consistency=False,
        )

        self.assertEqual(second_review, [])


if __name__ == "__main__":
    unittest.main()

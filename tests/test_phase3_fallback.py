from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from tempfile import TemporaryDirectory
import logging
import unittest
from unittest.mock import patch

import config


_TAGGER_PATH = Path(__file__).resolve().parents[1] / "01_tag.py"
_SPEC = spec_from_file_location("tagger_phase3_module", _TAGGER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("Failed to load 01_tag.py for tests")
_TAGGER = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_TAGGER)


class Phase3FallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.music_root = self.root / "music"
        self.music_root.mkdir(parents=True)

        self.track = self.music_root / "rock" / "Artist - Album (2000)" / "01 - Artist - Song.mp3"
        self.track.parent.mkdir(parents=True)
        self.track.touch()

        self.tags = {
            "title": "Song",
            "artist": "Artist",
            "albumartist": None,
            "album": None,
            "year": None,
            "genre": "Electronic",
            "bpm": None,
            "track": "1",
            "disc": None,
            "isrc": None,
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
            patch.object(config, "ACOUSTID_MIN_SCORE", 0.8),
            patch.object(config, "MB_RATE_LIMIT_SECONDS", 0.0),
            patch.object(config, "MB_MIN_TAG_VOTES", 2),
            patch.object(config, "MB_MAX_GENRES", 5),
            patch.object(_TAGGER, "log", logging.getLogger("test_phase3_fallback")),
            patch.object(_TAGGER, "read_tags", side_effect=self._read_tags),
            patch.object(_TAGGER, "write_tags", return_value={}),
            patch.object(_TAGGER, "detect_bpm", return_value=None),
        ]

        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.addCleanup(self.tempdir.cleanup)

    def _read_tags(self, path: Path) -> dict[str, str | None] | None:
        if path == self.track:
            return dict(self.tags)
        return None

    def test_low_confidence_fingerprint_uses_tag_based_fallback_search(self) -> None:
        with (
            patch.object(
                _TAGGER,
                "fingerprint_lookup",
                return_value={"recording_id": None, "title": None, "artist": None},
            ),
            patch.object(
                _TAGGER,
                "mb_recording_search",
                return_value={"recording_id": "rec123", "title": "Song", "artist": "Artist"},
            ) as mb_search,
            patch.object(
                _TAGGER,
                "mb_recording_metadata",
                return_value={
                    "album": "Album",
                    "year": "2000",
                    "track": "1",
                    "albumartist": None,
                    "isrc": "US-AAA-00-00001",
                },
            ),
            patch.object(_TAGGER, "mb_genres", return_value=["Electronic"]),
        ):
            _TAGGER.walk_folder(
                self.music_root / "rock",
                dry_run=True,
                overwrite=False,
                fix_suspicious=False,
                fix_track_mismatch=False,
                no_bpm=True,
                no_mb=False,
                no_consistency=True,
            )

        self.assertTrue(mb_search.called)
        _, kwargs = mb_search.call_args
        self.assertEqual(kwargs["genre_hints"], ["Rock", "Electronic"])


if __name__ == "__main__":
    unittest.main()

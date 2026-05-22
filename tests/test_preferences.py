from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

import config
from lib.preferences import (
    remap_genre_value,
    resolve_artist_folder_threshold,
    resolve_preference_labels,
    write_preference_labels,
)


class PreferenceHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store_path = Path(self.tempdir.name) / "preference_labels.json"

        self.override_patcher = patch.object(
            config,
            "PREFERENCE_OVERRIDES",
            {
                "global": {
                    "artist_folder_threshold": 3,
                    "preference_labels": ["study"],
                },
                "genre": {
                    "electronic": {
                        "artist_folder_threshold": 4,
                        "preference_labels": ["club"],
                    }
                },
                "artist": {
                    "aphex twin": {
                        "artist_folder_threshold": 6,
                        "preference_labels": ["love"],
                    }
                },
            },
        )
        self.remap_patcher = patch.object(
            config,
            "GENRE_REMAP_RULES",
            {
                "IDM": "Electronic",
                "Drum n Bass": "Drum & Bass",
            },
        )
        self.store_patcher = patch.object(config, "PREFERENCE_LABELS_PATH", self.store_path)

        self.override_patcher.start()
        self.remap_patcher.start()
        self.store_patcher.start()
        self.addCleanup(self.override_patcher.stop)
        self.addCleanup(self.remap_patcher.stop)
        self.addCleanup(self.store_patcher.stop)

    def test_threshold_precedence_artist_over_genre_over_global(self) -> None:
        self.assertEqual(
            resolve_artist_folder_threshold(
                artist="Aphex Twin",
                genre_folder="electronic",
                default=2,
            ),
            6,
        )

        self.assertEqual(
            resolve_artist_folder_threshold(
                artist="Boards of Canada",
                genre_folder="electronic",
                default=2,
            ),
            4,
        )

        self.assertEqual(
            resolve_artist_folder_threshold(
                artist="Boards of Canada",
                genre_folder="jazz",
                default=2,
            ),
            3,
        )

    def test_remap_genre_value_deduplicates_and_preserves_order(self) -> None:
        self.assertEqual(
            remap_genre_value("IDM / Drum n Bass / Electronic"),
            "Electronic / Drum & Bass",
        )

    def test_resolve_preference_labels_combines_scopes(self) -> None:
        self.assertEqual(
            resolve_preference_labels(
                artist="Aphex Twin",
                genre_value="IDM / Ambient",
            ),
            ["study", "club", "love"],
        )

    def test_write_preference_labels_respects_dry_run(self) -> None:
        changed = write_preference_labels(
            Path("/tmp/song.mp3"),
            ["love"],
            dry_run=True,
        )
        self.assertTrue(changed)
        self.assertFalse(self.store_path.exists())

    def test_write_preference_labels_persists_json(self) -> None:
        changed = write_preference_labels(
            Path("/tmp/song.mp3"),
            ["love", "study"],
            dry_run=False,
        )
        self.assertTrue(changed)
        self.assertTrue(self.store_path.exists())

        payload = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["labels"]["/tmp/song.mp3"], ["love", "study"])


if __name__ == "__main__":
    unittest.main()
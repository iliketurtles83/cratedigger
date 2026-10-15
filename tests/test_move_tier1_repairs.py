import importlib.util
import logging
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config


REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("move_module_p1", REPO_ROOT / "04_move.py")
move_module = importlib.util.module_from_spec(_spec)
sys.modules["move_module_p1"] = move_module
_spec.loader.exec_module(move_module)


class Tier1MoveRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        move_module.log = logging.getLogger("test_move_tier1")
        self.patchers = [
            patch.object(config, "MUSIC_ROOT", self.root),
            patch.object(config, "LETTER_BUCKETS", set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
            patch.object(config, "SYMBOL_BUCKET", "#"),
            patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}),
            patch.object(config, "ARTIST_SINGLES_FOLDER", "0singles"),
            patch.object(config, "TOP_LEVEL_SPECIAL", {"0compilations", "0videos"}),
            patch.object(config, "SPECIAL_FOLDERS", {"0compilations"}),
            patch.object(config, "SKIP_FOLDERS", {"0videos"}),
            patch.object(config, "GENRE_FOLDERS", set()),
            patch.object(config, "INCOMING_FOLDER", self.root / "incoming"),
            patch.object(config, "STAGED_TRACKS_FOLDER", self.root / "0new"),
            patch.object(config, "STAGED_ALBUMS_FOLDER", self.root / "0new_albums"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tempdir.cleanup)

    def test_restructure_refuses_to_flatten_letter_bucket(self) -> None:
        review = move_module.run_restructure_mode(dry_run=True, folder="A")

        self.assertEqual(review[0]["reason"], "tier1_use_03_folders")

    def test_relocates_explicit_various_artists_album(self) -> None:
        album = self.root / "A" / "Various Artists" / "Various Artists - Sampler"
        track = album / "01.mp3"
        track.parent.mkdir(parents=True)
        track.touch()
        moves = []

        with patch.object(
            move_module,
            "read_tags",
            return_value={
                "albumartist": "Various Artists",
                "album": "Sampler",
                "year": "2001",
            },
        ), patch.object(
            move_module,
            "_move_path",
            side_effect=lambda source, destination, **kwargs: moves.append(
                (source, destination)
            ),
        ):
            review = move_module.run_relocate_compilations_mode(
                dry_run=True,
                folder="A",
            )

        self.assertEqual(review, [])
        self.assertEqual(
            moves,
            [(album, self.root / "0compilations" / "Sampler (2001)")],
        )

    def test_does_not_move_album_with_conflicting_albumartists(self) -> None:
        album = self.root / "A" / "Artist" / "Artist - Album"
        first = album / "01.mp3"
        second = album / "02.mp3"
        album.mkdir(parents=True)
        first.touch()
        second.touch()

        def tags(path: Path) -> dict[str, str]:
            albumartist = "Various Artists" if path == first else "Artist"
            return {"albumartist": albumartist, "album": "Album", "year": "2001"}

        with patch.object(move_module, "read_tags", side_effect=tags), \
             patch.object(move_module, "_move_path") as move:
            move_module.run_relocate_compilations_mode(dry_run=True, folder="A")

        move.assert_not_called()

    def test_rehomes_various_artist_single_under_primary_artist(self) -> None:
        source = (
            self.root
            / "V"
            / "Various Artists"
            / "0singles"
            / "Akon feat. Eminem - Smack That.mp3"
        )
        source.parent.mkdir(parents=True)
        source.touch()
        (self.root / "A" / "Akon").mkdir(parents=True)

        with patch.object(
            move_module,
            "read_tags",
            return_value={
                "artist": "Akon feat. Eminem",
                "albumartist": "Various Artists",
                "album": "Compilation",
                "compilation": "1",
            },
        ), patch.object(
            move_module,
            "write_tags",
        ) as write_tags, patch.object(
            move_module,
            "update_downstream_paths",
        ) as update_paths:
            review = move_module.run_relocate_compilations_mode(
                dry_run=False,
                folder="V",
            )

        destination = (
            self.root
            / "A"
            / "Akon"
            / "0singles"
            / source.name
        )
        self.assertEqual(review, [])
        self.assertTrue(destination.exists())
        self.assertFalse(source.exists())
        write_tags.assert_called_once_with(
            source,
            {
                "albumartist": "Akon",
                "album": "",
                "compilation": "0",
                "track": "",
                "disc": "",
                "mb_albumid": "",
                "mb_albumartistid": "",
                "mb_releasegroupid": "",
            },
            dry_run=False,
        )
        update_paths.assert_called_once_with(
            {source: destination},
            music_root=self.root,
            logger=move_module.log,
        )

    def test_does_not_move_va_tagged_release_from_named_artist(self) -> None:
        album = self.root / "A" / "Artist" / "Artist - Compilation"
        track = album / "01.mp3"
        album.mkdir(parents=True)
        track.touch()

        with patch.object(
            move_module,
            "read_tags",
            return_value={
                "albumartist": "Various Artists",
                "album": "Compilation",
                "year": "2001",
            },
        ), patch.object(move_module, "_move_path") as move:
            move_module.run_relocate_compilations_mode(dry_run=True, folder="A")

        move.assert_not_called()

    def test_validate_config_reports_unclassified_top_level_folder(self) -> None:
        (self.root / "mystery").mkdir()

        review = move_module.run_validate_config_mode()

        self.assertEqual(
            [item["reason"] for item in review],
            ["unclassified_top_level_folder"],
        )


if __name__ == "__main__":
    unittest.main()

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config
from lib.parsers import build_filename, sanitise_component, sanitise_name
from scripts.repair_windows_paths import repair_windows_paths


class WindowsPathTests(unittest.TestCase):
    def test_sanitise_name_removes_windows_invalid_components(self) -> None:
        self.assertEqual(sanitise_name('Artist: "Live" <1999>?'), "Artist Live 1999")
        self.assertEqual(sanitise_name("Artist. "), "Artist.")
        self.assertEqual(sanitise_name(".Artist"), "Artist")
        self.assertEqual(sanitise_name("...Artist"), "Artist")
        self.assertEqual(sanitise_name("A/B"), "A-B")

    def test_sanitise_name_keeps_meaningful_trailing_period(self) -> None:
        for value in ("R.E.M.", "Dinosaur Jr.", "M.I.A.", "Blake Inc."):
            self.assertEqual(sanitise_name(value), value)

    def test_sanitise_component_enforces_windows_rules(self) -> None:
        self.assertEqual(sanitise_component("R.E.M."), "R.E.M")
        self.assertEqual(sanitise_component("Album "), "Album")
        self.assertEqual(sanitise_component(".hidden"), "hidden")
        self.assertEqual(sanitise_component("CON"), "_CON")
        self.assertEqual(sanitise_component("NUL.mp3"), "_NUL.mp3")
        self.assertEqual(sanitise_component("..."), "_")

    def test_build_filename_preserves_periods_inside_component(self) -> None:
        self.assertEqual(
            build_filename("1", "R.E.M.", "Fall on Me", ".mp3"),
            "01 - R.E.M. - Fall on Me.mp3",
        )
        self.assertEqual(
            build_filename("4", "Korn", "Kick the P.A.", ".mp3"),
            "04 - Korn - Kick the P.A..mp3",
        )

    def test_rename_preserves_alternate_marker_and_case_conflicts(self) -> None:
        import importlib

        rename = importlib.import_module("02_rename")
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            canonical = folder / "03 - Dire Straits - Walk Of Life.mp3"
            alternate = folder / "03 - Dire Straits - Walk Of Life [alternate-96069253].mp3"
            canonical.write_bytes(b"a")
            alternate.write_bytes(b"b")

            review: list[dict] = []
            rename.rename_file(
                alternate,
                tags={"artist": "Dire Straits", "title": "Walk of Life", "track": "3"},
                use_disc=False,
                dry_run=False,
                review_items=review,
            )

            names = sorted(path.name for path in folder.iterdir())
            self.assertEqual(
                names,
                [
                    "03 - Dire Straits - Walk Of Life.mp3",
                    "03 - Dire Straits - Walk of Life [alternate-96069253].mp3",
                ],
            )
            self.assertEqual(review, [])

    def test_rename_refuses_case_only_collision(self) -> None:
        import importlib

        rename = importlib.import_module("02_rename")
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            existing = folder / "03 - Can - Mother Sky.mp3"
            source = folder / "03 - CAN - Mother Sky.mp3"
            existing.write_bytes(b"a")
            source.write_bytes(b"b")

            review: list[dict] = []
            rename.rename_file(
                source,
                tags={"artist": "Can", "title": "Mother Sky", "track": "3"},
                use_disc=False,
                dry_run=False,
                review_items=review,
            )

            self.assertTrue(source.exists())
            self.assertEqual(review[0]["reason"], "rename_conflict")

    def test_repair_merges_case_insensitive_duplicate_files(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "Artist"
            parent.mkdir()
            first = parent / "Song.mp3"
            second = parent / "song.mp3"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            with patch.object(config, "MUSIC_ROOT", root), patch(
                "scripts.repair_windows_paths.audio_hash",
                side_effect=lambda path: None,
            ), patch(
                "scripts.repair_windows_paths.update_downstream_paths"
            ) as update_paths:
                repair_windows_paths(dry_run=False, fix=True)

            self.assertEqual(
                sorted(path.name for path in parent.iterdir()),
                ["Song [alternate-16367aac].mp3", "Song.mp3"],
            )
            update_paths.assert_called_once()

    def test_repair_merges_case_insensitive_cover_art_keeping_higher_quality(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            album = root / "Artist - Album"
            album.mkdir()
            low_res = album / "Cover.jpg"
            high_res = album / "cover.jpg"
            # Simulate low-res vs high-res sizes
            low_res.write_bytes(b"small")
            high_res.write_bytes(b"large_high_resolution_image_data")

            with patch.object(config, "MUSIC_ROOT", root), patch(
                "scripts.repair_windows_paths.update_downstream_paths"
            ):
                repair_windows_paths(dry_run=False, fix=True)

            self.assertEqual(
                [p.name for p in album.iterdir()],
                ["cover.jpg"],
            )
            self.assertEqual(
                (album / "cover.jpg").read_bytes(),
                b"large_high_resolution_image_data",
            )


if __name__ == "__main__":
    unittest.main()

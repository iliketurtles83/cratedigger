import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config
from scripts.reconcile_artists import (
    apply_merge_plan,
    build_merge_plans,
    choose_canonical_spelling,
    find_artist_folder_groups,
)


class ReconcileArtistsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "library.db"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY,
                    path BLOB,
                    artist TEXT,
                    albumartist TEXT,
                    mb_albumartistid TEXT,
                    album_id INTEGER
                )
                """
            )
            connection.execute(
                "CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT)"
            )
        self.patchers = [
            patch.object(config, "MUSIC_ROOT", self.root),
            patch.object(config, "LETTER_BUCKETS", set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
            patch.object(config, "SYMBOL_BUCKET", "#"),
            patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _track(self, relative: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_choose_canonical_spelling_prefers_mb_then_count(self) -> None:
        variants = [
            ("The Smashing Pumpkins", 6, 6),
            ("Smashing Pumpkins", 15, 0),
            ("smashing pumpkins", 12, 0),
        ]
        self.assertEqual(
            choose_canonical_spelling(variants),
            "The Smashing Pumpkins",
        )
        self.assertEqual(
            choose_canonical_spelling([("TOOL", 11, 0), ("Tool", 54, 0)]),
            "Tool",
        )
        self.assertEqual(
            choose_canonical_spelling([("sigur ros", 3, 0)]),
            "Sigur Ros",
        )

    def test_finds_duplicate_tier1_artist_folders(self) -> None:
        (self.root / "R" / "R.E.M.").mkdir(parents=True)
        (self.root / "R" / "REM").mkdir(parents=True)
        (self.root / "R" / "Radiohead").mkdir(parents=True)

        groups = find_artist_folder_groups(self.root)

        self.assertEqual(
            {path.name for path in groups["rem"]},
            {"R.E.M.", "REM"},
        )
        self.assertNotIn("radiohead", groups)

    def test_plan_requires_same_id_in_every_folder(self) -> None:
        first = self._track("C/CAN/CAN - First/01.mp3")
        second = self._track("C/Can/Can - Second/01.mp3")

        def tags(path: Path) -> dict[str, str]:
            return {
                "albumartist": path.parents[1].name,
                "mb_albumartistid": "mb-can" if path == first else "",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(self.root, self.database)[0]

        self.assertEqual(plan.confidence, "partial")
        self.assertFalse(plan.eligible)

    def test_plan_accepts_complete_matching_track_artist_ids(self) -> None:
        first = self._track("B/Bloodhound Gang/Bloodhound Gang - First/01.mp3")
        second = self._track("B/The Bloodhound Gang/The Bloodhound Gang - Second/01.mp3")

        def tags(path: Path) -> dict[str, str]:
            if path == first:
                return {
                    "albumartist": "Bloodhound Gang",
                    "mb_albumartistid": "mb-bloodhound",
                    "mb_artistid": "mb-bloodhound",
                }
            return {
                "albumartist": "The Bloodhound Gang",
                "mb_albumartistid": "",
                "mb_artistid": "mb-bloodhound",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(self.root, self.database)[0]

        self.assertEqual(plan.confidence, "confirmed")
        self.assertIn("complete track artist-ID coverage", plan.reason)
        self.assertTrue(plan.eligible)

    def test_plan_accepts_every_album_in_online_artist_catalog(self) -> None:
        known = self._track("A/Arcade Fire/Arcade Fire - Known/01.mp3")
        unknown = self._track("A/The Arcade Fire/The Arcade Fire - Funeral/01.mp3")

        def tags(path: Path) -> dict[str, str]:
            if path == known:
                return {
                    "albumartist": "Arcade Fire",
                    "album": "Known",
                    "mb_albumartistid": "mb-arcade",
                    "mb_artistid": "mb-arcade",
                }
            return {
                "albumartist": "The Arcade Fire",
                "album": "Funeral",
                "mb_albumartistid": "",
                "mb_artistid": "",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(
                self.root,
                self.database,
                release_catalogs={"mb-arcade": {"funeral"}},
            )[0]

        self.assertEqual(plan.confidence, "confirmed")
        self.assertIn("MusicBrainz catalog", plan.reason)

    def test_online_catalog_requires_every_album_to_match(self) -> None:
        known = self._track("A/Arcade Fire/Arcade Fire - Known/01.mp3")
        unknown = self._track("A/The Arcade Fire/The Arcade Fire - Other/01.mp3")

        def tags(path: Path) -> dict[str, str]:
            if path == known:
                return {
                    "albumartist": "Arcade Fire",
                    "album": "Known",
                    "mb_albumartistid": "mb-arcade",
                }
            return {
                "albumartist": "The Arcade Fire",
                "album": "Not Their Album",
                "mb_albumartistid": "",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(
                self.root,
                self.database,
                release_catalogs={"mb-arcade": {"funeral"}},
            )[0]

        self.assertEqual(plan.confidence, "partial")

    def test_plan_blocks_conflicting_ids(self) -> None:
        self._track("M/M.I.A./M.I.A. - First/01.mp3")
        self._track("M/MIA/MIA - Second/01.mp3")

        def tags(path: Path) -> dict[str, str]:
            artist = path.parents[1].name
            return {
                "albumartist": artist,
                "mb_albumartistid": f"mb-{artist}",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(self.root, self.database)[0]

        self.assertEqual(plan.confidence, "conflict")
        self.assertFalse(plan.eligible)

    def test_plan_blocks_child_destination_collision(self) -> None:
        self._track("R/Ramones/Ramones - Album/01.mp3")
        self._track("R/The Ramones/The Ramones - Album/02.mp3")

        with patch(
            "scripts.reconcile_artists.read_tags",
            return_value={
                "albumartist": "The Ramones",
                "mb_albumartistid": "mb-ramones",
            },
        ):
            plan = build_merge_plans(self.root, self.database)[0]

        self.assertEqual(plan.confidence, "confirmed")
        self.assertEqual(len(plan.collisions), 1)
        self.assertFalse(plan.eligible)

    def test_applies_confirmed_collision_free_merge(self) -> None:
        old_track = self._track("C/CAN/CAN - Other/01.mp3")
        canonical_track = self._track("C/Can/Can - Album/01.mp3")
        destination = self.root / "C" / "Can" / "Can - Other" / "01.mp3"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                """
                INSERT INTO items (
                    path, artist, albumartist, mb_albumartistid, album_id
                )
                VALUES (?, 'Can', 'Can', 'mb-can', 1)
                """,
                (str(canonical_track).encode(),),
            )
            connection.execute(
                """
                INSERT INTO items (
                    path, artist, albumartist, mb_albumartistid, album_id
                )
                VALUES (?, 'CAN', 'CAN', 'mb-can', 1)
                """,
                (str(destination).encode(),),
            )
            connection.execute(
                "INSERT INTO albums (id, albumartist) VALUES (1, 'CAN')"
            )
            connection.execute(
                """
                INSERT INTO items (
                    path, artist, albumartist, mb_albumartistid, album_id
                )
                VALUES ('outside.mp3', 'CAN', 'CAN', 'other-id', 2)
                """
            )
            connection.execute(
                "INSERT INTO albums (id, albumartist) VALUES (2, 'CAN')"
            )

        tag_map = {
            old_track: {
                "artist": "CAN",
                "albumartist": "CAN",
                "mb_albumartistid": "mb-can",
            },
            canonical_track: {
                "artist": "Can",
                "albumartist": "Can",
                "mb_albumartistid": "mb-can",
            },
        }
        writes = []

        with patch(
            "scripts.reconcile_artists.read_tags",
            side_effect=lambda path: tag_map.get(path, tag_map.get(old_track)),
        ), patch(
            "scripts.reconcile_artists.write_tags",
            side_effect=lambda path, updates, dry_run=False: writes.append(
                (path, updates)
            ),
        ), patch(
            "scripts.reconcile_artists.update_downstream_paths"
        ) as update_paths:
            plan = build_merge_plans(self.root, self.database)[0]
            updated, path_map = apply_merge_plan(
                plan,
                database=self.database,
                dry_run=False,
            )

        self.assertEqual(plan.confidence, "confirmed")
        self.assertTrue(destination.exists())
        self.assertFalse((self.root / "C" / "CAN").exists())
        self.assertEqual(path_map[old_track], destination)
        self.assertEqual(updated, 1)
        self.assertEqual(writes[0][1], {"albumartist": "Can"})
        update_paths.assert_called_once()

        with closing(sqlite3.connect(self.database)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT albumartist FROM albums"
                ).fetchone()[0],
                "Can",
            )
            self.assertEqual(
                set(connection.execute(
                    "SELECT artist, albumartist FROM items WHERE id IN (1, 2)"
                ).fetchall()),
                {("Can", "Can"), ("CAN", "Can")},
            )
            self.assertEqual(
                connection.execute(
                    "SELECT artist, albumartist FROM items WHERE album_id = 2"
                ).fetchone(),
                ("CAN", "CAN"),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT albumartist FROM albums WHERE id = 2"
                ).fetchone()[0],
                "CAN",
            )


if __name__ == "__main__":
    unittest.main()

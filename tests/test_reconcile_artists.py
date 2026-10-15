import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config
from scripts.reconcile_artists import (
    _analyse_collision,
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

    def test_release_catalog_normalises_multiple_tag_spellings_in_one_unit(self) -> None:
        known = self._track("G/Gotan Project/Gotan Project - Known/01.mp3")
        first = self._track(
            "G/gotan project/gotan project - Revancha/01.mp3"
        )
        second = self._track(
            "G/gotan project/gotan project - Revancha/02.mp3"
        )

        def tags(path: Path) -> dict[str, str]:
            if path == known:
                return {
                    "title": "Known",
                    "albumartist": "Gotan Project",
                    "album": "Known",
                    "mb_albumartistid": "mb-gotan",
                }
            return {
                "title": path.stem,
                "albumartist": "gotan project",
                "album": (
                    "La Revancha Del Tango"
                    if path == first
                    else "la revancha del tango"
                ),
                "mb_albumartistid": "",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(
                self.root,
                self.database,
                release_catalogs={"mb-gotan": {"la revancha del tango"}},
            )[0]

        self.assertEqual(plan.confidence, "confirmed")

    def test_recording_catalog_corroborates_compilation_tracks(self) -> None:
        known = self._track("R/Roots Manuva/Roots Manuva - Known/01.mp3")
        compilation = self._track(
            "R/roots manuva/roots manuva - Compilation/01.mp3"
        )

        def tags(path: Path) -> dict[str, str]:
            if path == known:
                return {
                    "title": "Known",
                    "albumartist": "Roots Manuva",
                    "album": "Known",
                    "mb_albumartistid": "mb-roots",
                }
            return {
                "title": "Witness",
                "albumartist": "roots manuva",
                "album": "Various Artists Compilation",
                "mb_albumartistid": "",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(
                self.root,
                self.database,
                release_catalogs={"mb-roots": set()},
                recording_catalogs={"mb-roots": {"witness"}},
            )[0]

        self.assertEqual(plan.confidence, "confirmed")
        self.assertIn("MusicBrainz catalogs", plan.reason)

    def test_recording_catalog_requires_every_unproven_track(self) -> None:
        known = self._track("R/Roots Manuva/Roots Manuva - Known/01.mp3")
        self._track("R/roots manuva/roots manuva - Other/01.mp3")
        self._track("R/roots manuva/roots manuva - Other/02.mp3")

        def tags(path: Path) -> dict[str, str]:
            if path == known:
                return {
                    "title": "Known",
                    "albumartist": "Roots Manuva",
                    "album": "Known",
                    "mb_albumartistid": "mb-roots",
                }
            return {
                "title": "Witness" if path.name == "01.mp3" else "Unknown",
                "albumartist": "roots manuva",
                "album": "Compilation",
                "mb_albumartistid": "",
            }

        with patch("scripts.reconcile_artists.read_tags", side_effect=tags):
            plan = build_merge_plans(
                self.root,
                self.database,
                recording_catalogs={"mb-roots": {"witness"}},
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

    def test_collision_analysis_accepts_matching_title_duration(self) -> None:
        source = self.root / "R" / "Ramones" / "Ramones - Album"
        destination = self.root / "R" / "The Ramones" / "The Ramones - Album"
        source_track = self._track(
            "R/Ramones/Ramones - Album/01 - Ramones - Song.mp3"
        )
        destination_track = self._track(
            "R/The Ramones/The Ramones - Album/01 - Ramones - Song.mp3"
        )

        def tags(path: Path) -> dict[str, str]:
            return {
                "title": "Song",
                "mb_trackid": "recording-id" if path == destination_track else "",
                "mb_albumid": "release-id",
                "mb_releasegroupid": "release-group-id",
            }

        with patch(
            "scripts.reconcile_artists.read_tags",
            side_effect=tags,
        ), patch(
            "scripts.reconcile_artists.audio_hash",
            side_effect=lambda path: f"hash-{Path(path).parent.parent.name}",
        ), patch(
            "scripts.reconcile_artists._audio_duration",
            return_value=180.0,
        ):
            analysis = _analyse_collision(source, destination)

        self.assertTrue(analysis.resolvable)
        self.assertEqual(analysis.matching_tracks, 1)
        self.assertEqual(analysis.alternate_audio, 1)

    def test_collision_analysis_blocks_conflicting_release_ids(self) -> None:
        source = self.root / "R" / "Ramones" / "Ramones - Album"
        destination = self.root / "R" / "The Ramones" / "The Ramones - Album"
        source_track = self._track("R/Ramones/Ramones - Album/01.mp3")
        self._track("R/The Ramones/The Ramones - Album/01.mp3")

        with patch(
            "scripts.reconcile_artists.read_tags",
            side_effect=lambda path: {
                "title": "Song",
                "mb_albumid": "source-release"
                if path == source_track
                else "destination-release",
            },
        ), patch(
            "scripts.reconcile_artists.audio_hash",
            return_value="hash",
        ), patch(
            "scripts.reconcile_artists._audio_duration",
            return_value=180.0,
        ):
            analysis = _analyse_collision(source, destination)

        self.assertFalse(analysis.resolvable)
        self.assertIn("conflicting MusicBrainz release IDs", analysis.reason)

    def test_resolves_album_collision_and_preserves_alternate_audio(self) -> None:
        source_track = self._track(
            "R/Ramones/Ramones - Album/01 - Ramones - Song.mp3"
        )
        destination_track = self._track(
            "R/The Ramones/The Ramones - Album/01 - Ramones - Song.mp3"
        )
        source_track.write_bytes(b"source audio")
        destination_track.write_bytes(b"destination audio")

        def tags(path: Path) -> dict[str, str]:
            return {
                "title": "Song",
                "albumartist": path.parents[1].name,
                "mb_albumartistid": "mb-ramones",
                "mb_albumid": "release-id",
                "mb_releasegroupid": "release-group-id",
            }

        with patch(
            "scripts.reconcile_artists.read_tags",
            side_effect=tags,
        ), patch(
            "scripts.reconcile_artists.audio_hash",
            side_effect=lambda path: f"a1:{Path(path).read_bytes().hex()}",
        ), patch(
            "scripts.reconcile_artists._audio_duration",
            return_value=180.0,
        ), patch(
            "scripts.reconcile_artists.write_tags",
        ), patch(
            "scripts.reconcile_artists.update_downstream_paths"
        ) as update_paths:
            plan = build_merge_plans(
                self.root,
                self.database,
                analyze_collisions=True,
            )[0]
            updated, path_map = apply_merge_plan(
                plan,
                database=self.database,
                dry_run=False,
                resolve_collisions=True,
            )

        alternates = [
            path
            for path in destination_track.parent.iterdir()
            if "[alternate-" in path.name
        ]
        self.assertEqual(len(alternates), 1)
        self.assertEqual(alternates[0].read_bytes(), b"source audio")
        self.assertEqual(path_map[source_track], alternates[0])
        self.assertEqual(updated, 0)
        self.assertFalse((self.root / "R" / "Ramones").exists())
        update_paths.assert_called_once()

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
                connection.execute("SELECT albumartist FROM albums WHERE id = 1")
                .fetchone()[0],
                "CAN",
            )
            self.assertEqual(
                set(
                    connection.execute(
                        "SELECT artist, albumartist FROM items WHERE id IN (1, 2)"
                    ).fetchall()
                ),
                {("Can", "Can"), ("CAN", "CAN")},
            )


if __name__ == "__main__":
    unittest.main()

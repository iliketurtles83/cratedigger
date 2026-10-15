from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import config
from scripts.audit_beets_matches import (
    MatchUnit,
    _ANSI_ESCAPE,
    _MATCH_CONFIDENCE,
    _beets_command,
    _catalog_items,
    collect_unmatched_units,
)


class AuditBeetsMatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.patchers = [
            patch.object(config, "MUSIC_ROOT", self.root),
            patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}),
            patch.object(config, "ARTIST_SINGLES_FOLDER", "0singles"),
            patch.object(config, "ROOT_SINGLES_FOLDER", "0singles"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _track(self, relative: str, mb_trackid: str | None = None) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        path.with_suffix(".mbid").write_text(mb_trackid or "", encoding="utf-8")
        return path

    @staticmethod
    def _read_tags(path: Path) -> dict[str, str | None]:
        value = path.with_suffix(".mbid").read_text(encoding="utf-8")
        return {"mb_trackid": value or None}

    def test_collects_only_units_with_missing_track_ids(self) -> None:
        album = self.root / "Q" / "Quantic" / "Quantic - Album (2002)"
        self._track(str(album.relative_to(self.root) / "01.mp3"), "known")
        self._track(str(album.relative_to(self.root) / "02.mp3"))
        known = self.root / "Q" / "Queen" / "Queen - Album (1974)"
        self._track(str(known.relative_to(self.root) / "01.mp3"), "known")

        with patch(
            "scripts.audit_beets_matches.read_tags",
            side_effect=self._read_tags,
        ):
            units, unreadable = collect_unmatched_units(self.root / "Q")

        self.assertEqual(unreadable, 0)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].path, album)
        self.assertEqual(units[0].tracks, 2)
        self.assertEqual(units[0].missing_track_ids, 1)

    def test_disc_subfolders_form_one_album_unit(self) -> None:
        album = self.root / "Q" / "Artist" / "Artist - Album (2002)"
        self._track(str(album.relative_to(self.root) / "CD1" / "01.mp3"))
        self._track(str(album.relative_to(self.root) / "CD2" / "01.mp3"))

        with patch(
            "scripts.audit_beets_matches.read_tags",
            side_effect=self._read_tags,
        ):
            units, _ = collect_unmatched_units(self.root / "Q")

        self.assertEqual([unit.path for unit in units], [album])
        self.assertEqual(units[0].tracks, 2)

    def test_beets_command_is_isolated_and_read_only(self) -> None:
        unit = MatchUnit(
            path=self.root / "Q" / "Quantic" / "Album",
            singleton=False,
            tracks=10,
            missing_track_ids=10,
        )
        database = self.root / "temporary.db"
        command = _beets_command(unit, database, self.root / "audit.log")

        self.assertIn(str(database), command)
        self.assertIn("--nowrite", command)
        self.assertIn("--nomove", command)
        self.assertIn("--nocopy", command)
        self.assertIn("--noincremental", command)
        self.assertIn("skip", command)
        self.assertNotIn("--singletons", command)

    def test_beets_command_marks_singles(self) -> None:
        unit = MatchUnit(
            path=self.root / "Q" / "Q-Bert" / "0singles",
            singleton=True,
            tracks=1,
            missing_track_ids=1,
        )
        command = _beets_command(
            unit,
            self.root / "temporary.db",
            self.root / "audit.log",
        )
        self.assertIn("--singletons", command)

    def test_catalog_items_reads_and_closes_temporary_library(self) -> None:
        database = self.root / "catalog.db"
        item = type(
            "Item",
            (),
            {
                "path": b"/music/song.mp3",
                "artist": "Artist",
                "title": "Title",
                "albumartist": "Artist",
                "album": "Album",
                "year": 2002,
                "track": 1,
                "tracktotal": 1,
                "disc": 1,
                "disctotal": 1,
                "isrc": "USABC0200001",
                "comp": False,
                "mb_trackid": "track-id",
                "mb_albumid": "album-id",
                "mb_artistid": "artist-id",
                "mb_albumartistid": "artist-id",
                "mb_releasegroupid": "group-id",
            },
        )()
        library = MagicMock()
        library.items.return_value = [item]
        with patch("beets.library.Library", return_value=library), patch.object(
            Path,
            "stat",
            return_value=type("Stat", (), {"st_size": 123, "st_mtime_ns": 456})(),
        ):
            result = _catalog_items(database)

        self.assertEqual(result[0]["mb_trackid"], "track-id")
        self.assertEqual(result[0]["path"], "/music/song.mp3")
        self.assertEqual(result[0]["track"], "1/1")
        self.assertEqual(result[0]["source_size"], 123)
        library._close.assert_called_once_with()

    def test_confidence_parser_accepts_colored_beets_output(self) -> None:
        output = "  Match (\x1b[1;32m87.9%\x1b[39;49;00m):"
        clean = _ANSI_ESCAPE.sub("", output)
        self.assertEqual(_MATCH_CONFIDENCE.findall(clean), ["87.9"])


if __name__ == "__main__":
    unittest.main()

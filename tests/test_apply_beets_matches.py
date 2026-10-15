from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

import config
from scripts.apply_beets_matches import apply_report, plan_writes


class ApplyBeetsMatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.track = self.root / "Q" / "Artist" / "Album" / "01.mp3"
        self.track.parent.mkdir(parents=True)
        self.track.write_bytes(b"audio")
        self.patchers = [
            patch.object(config, "MUSIC_ROOT", self.root),
            patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tempdir.cleanup)

    def _item(self) -> dict[str, object]:
        stat = self.track.stat()
        return {
            "path": str(self.track),
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "artist": "Artist",
            "title": "Title",
            "albumartist": "Artist",
            "album": "Album",
            "year": 2002,
            "track": "1/1",
            "disc": "1/1",
            "compilation": "0",
            "mb_trackid": "track-id",
            "mb_albumid": "release-id",
            "mb_artistid": "artist-id",
            "mb_albumartistid": "artist-id",
            "mb_releasegroupid": "group-id",
        }

    def _report(self) -> dict[str, object]:
        return {
            "online": True,
            "units": [
                {
                    "path": "Q/Artist/Album",
                    "status": "matched",
                    "tracks": 1,
                    "items": [self._item()],
                }
            ],
        }

    def test_plan_requires_explicit_matching_release(self) -> None:
        with self.assertRaisesRegex(ValueError, "absent"):
            plan_writes(self._report(), {"other-release"})

    def test_plan_rejects_source_changed_after_audit(self) -> None:
        report = self._report()
        self.track.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            plan_writes(report, {"release-id"})

    def test_dry_run_writes_application_record_without_tags(self) -> None:
        report_path = self.root / "report.json"
        result_path = self.root / "result.json"
        report_path.write_text(json.dumps(self._report()), encoding="utf-8")
        current = {
            "artist": "Artist",
            "title": "Old Title",
            "albumartist": "Artist",
            "album": "Album",
        }
        with patch(
            "scripts.apply_beets_matches.read_tags",
            return_value=current,
        ), patch(
            "scripts.apply_beets_matches.write_tags",
            side_effect=lambda path, tags, dry_run: dict(tags),
        ) as writer:
            result = apply_report(
                report_path,
                {"release-id"},
                dry_run=True,
                result_path=result_path,
            )

        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["changed_tracks"], 1)
        writer.assert_called_once()
        self.assertTrue(writer.call_args.kwargs["dry_run"])
        self.assertTrue(result_path.exists())

    def test_plan_rejects_conflicting_existing_release(self) -> None:
        with patch(
            "scripts.apply_beets_matches.read_tags",
            return_value={"mb_albumid": "different-release"},
        ):
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                plan_writes(self._report(), {"release-id"})


if __name__ == "__main__":
    unittest.main()

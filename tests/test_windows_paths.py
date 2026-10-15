from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config
from lib.parsers import sanitise_name
from scripts.repair_windows_paths import repair_windows_paths


class WindowsPathTests(unittest.TestCase):
    def test_sanitise_name_removes_windows_invalid_components(self) -> None:
        self.assertEqual(sanitise_name('Artist: "Live" <1999>?'), "Artist Live 1999")
        self.assertEqual(sanitise_name("Artist. "), "Artist")
        self.assertEqual(sanitise_name("CON"), "_CON")
        self.assertEqual(sanitise_name("A/B"), "A-B")

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


if __name__ == "__main__":
    unittest.main()

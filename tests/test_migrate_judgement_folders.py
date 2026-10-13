import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.migrate_judgement_folders import migrate_folder, update_playlist_entry


class MigrateJudgementFoldersTests(unittest.TestCase):

    def test_update_playlist_entry(self):
        with tempfile.NamedTemporaryFile(suffix=".m3u8", mode="w+", delete=False) as tmp:
            playlist_path = Path(tmp.name)
            playlist_path.write_text("#EXTM3U\n../0faves/Song.mp3\n", encoding="utf-8")

        success = update_playlist_entry(playlist_path, "../0faves/Song.mp3", "../S/Song/0singles/Song.mp3")
        self.assertTrue(success)
        self.assertIn("../S/Song/0singles/Song.mp3", playlist_path.read_text(encoding="utf-8"))
        playlist_path.unlink()

    def test_migrate_folder_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lib_root = Path(tmp_dir)
            faves_dir = lib_root / "0faves"
            faves_dir.mkdir()
            sample_song = faves_dir / "Test Artist - Test Song.mp3"
            sample_song.write_bytes(b"dummy audio content")

            features_cache = {}
            with patch("scripts.migrate_judgement_folders.audio_hash", return_value="a1:12345"):
                stats = migrate_folder("0faves", lib_root, features_cache, dry_run=True, fix=False)

            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["moved"], 1)
            self.assertEqual(stats["duplicates"], 0)
            self.assertTrue(sample_song.exists())  # Not moved in dry-run


if __name__ == "__main__":
    unittest.main()

import importlib
import tempfile
import unittest
from pathlib import Path

from lib.parsers import validate_albumartist

folders_mod = importlib.import_module("03_folders")
_resolve_album_artist = folders_mod._resolve_album_artist
_record_folder_rename = folders_mod._record_folder_rename
_update_downstream = folders_mod._update_downstream


class TestFolders(unittest.TestCase):
    def test_resolve_album_artist_parent_anchors(self):
        # Inside an artist folder, parent_artist anchors the artist name
        tag_values = {"artist": "Cassidy", "albumartist": "ATB"}
        artist = _resolve_album_artist(parent_artist="ATB", tag_values=tag_values, parsed_artist="ATB")
        self.assertEqual(artist, "ATB")

    def test_resolve_album_artist_albumartist_priority(self):
        # Standalone album folder with albumartist tag
        tag_values = {"artist": "2Pac feat. R.L.", "albumartist": "2Pac"}
        artist = _resolve_album_artist(parent_artist=None, tag_values=tag_values, parsed_artist="2Pac")
        self.assertEqual(artist, "2Pac")

    def test_resolve_album_artist_strips_feat(self):
        # Standalone album folder without albumartist tag strips feat.
        tag_values = {"artist": "OutKast feat. Killer Mike", "albumartist": None}
        artist = _resolve_album_artist(parent_artist=None, tag_values=tag_values, parsed_artist="OutKast")
        self.assertEqual(artist, "OutKast")

    def test_record_folder_rename(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            old_album = root / "Old Artist - Old Album (2000)"
            old_album.mkdir()
            old_song = old_album / "01 - Song.mp3"
            old_song.write_bytes(b"dummy")

            new_album = root / "New Artist - New Album (2000)"
            file_renamed_map = {}
            _record_folder_rename(old_album, new_album, file_renamed_map)

            expected_new_song = new_album / "01 - Song.mp3"
            self.assertEqual(file_renamed_map.get(old_song), expected_new_song)


if __name__ == "__main__":
    unittest.main()

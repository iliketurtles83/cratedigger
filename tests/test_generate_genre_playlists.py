import tempfile
import unittest
from pathlib import Path

from scripts.generate_genre_playlists import (
    generate_playlists,
    resolve_root_genre,
)


class TestGenerateGenrePlaylists(unittest.TestCase):
    def test_resolve_root_genre(self):
        # Direct root genre
        self.assertEqual(resolve_root_genre("Rock"), "Rock")
        self.assertEqual(resolve_root_genre("Electronic"), "Electronic")

        # Two-tier combination
        self.assertEqual(resolve_root_genre("Rock / Indie Rock"), "Rock")
        self.assertEqual(resolve_root_genre("Electronic / Synthpop"), "Electronic")
        self.assertEqual(resolve_root_genre("Chill / Ambient"), "Chill")

        # Subgenre mapping via alias
        self.assertEqual(resolve_root_genre("Dancehall"), "Reggae")
        self.assertEqual(resolve_root_genre("Drum 'n Bass"), "Electronic")
        self.assertEqual(resolve_root_genre("Grime"), "Hip-Hop")

        # Empty or unknown
        self.assertEqual(resolve_root_genre(""), "Other")
        self.assertEqual(resolve_root_genre(None), "Other")

    def test_generate_playlists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            music_root = Path(tmp_dir) / "audio"
            playlists_dir = music_root / "Playlists"

            # Create test singles
            s1 = music_root / "R" / "Radiohead" / "0singles" / "Radiohead - Creep.mp3"
            s2 = music_root / "D" / "Daft Punk" / "0singles" / "Daft Punk - One More Time.mp3"
            s1.parent.mkdir(parents=True)
            s2.parent.mkdir(parents=True)
            s1.write_bytes(b"\x00" * 100)
            s2.write_bytes(b"\x00" * 100)

            # Generate with min_tracks=1 so all genres get a playlist
            results = generate_playlists(
                music_root,
                playlists_dir,
                dry_run=False,
                fix=True,
                min_tracks=1,
            )

            self.assertIn("All_Singles.m3u8", results)
            self.assertEqual(results["All_Singles.m3u8"], 2)
            self.assertTrue((playlists_dir / "All_Singles.m3u8").exists())


if __name__ == "__main__":
    unittest.main()

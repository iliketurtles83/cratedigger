import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.reconcile_artists import choose_canonical_spelling, find_split_artists


class ReconcileArtistsTests(unittest.TestCase):

    def test_choose_canonical_spelling_prefers_mb_and_title_case(self):
        # 1. MB count wins
        variants_mb = [
            ("The Smashing Pumpkins", 6, 6),
            ("Smashing Pumpkins", 15, 0),
            ("smashing pumpkins", 12, 0),
        ]
        self.assertEqual(choose_canonical_spelling(variants_mb), "The Smashing Pumpkins")

        # 2. Track count wins when MB is equal
        variants_counts = [
            ("TOOL", 11, 0),
            ("Tool", 54, 0),
        ]
        self.assertEqual(choose_canonical_spelling(variants_counts), "Tool")

        # 3. Clean title case when all are lowercase
        variants_lower = [
            ("sigur ros", 3, 0),
        ]
        self.assertEqual(choose_canonical_spelling(variants_lower), "Sigur Ros")

        # 4. Trailing whitespace is stripped
        variants_ws = [
            ("Wu-Tang Clan  ", 17, 0),
            ("Wu-Tang Clan", 35, 0),
        ]
        self.assertEqual(choose_canonical_spelling(variants_ws), "Wu-Tang Clan")

    def test_find_split_artists(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db_path = Path(tmp.name)
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                artist TEXT,
                mb_artistid TEXT
            )
            """)
            c.execute("INSERT INTO items (artist, mb_artistid) VALUES ('R.E.M.', 'mb-rem')")
            c.execute("INSERT INTO items (artist, mb_artistid) VALUES ('REM', NULL)")
            c.execute("INSERT INTO items (artist, mb_artistid) VALUES ('Radiohead', 'mb-radiohead')")
            conn.commit()
            conn.close()

            splits = find_split_artists(db_path)
            self.assertIn("rem", splits)
            self.assertNotIn("radiohead", splits)
            self.assertEqual(len(splits["rem"]), 2)

    def test_reconcile_artists_updates_relative_paths_and_db(self):
        from unittest.mock import patch
        from scripts.reconcile_artists import reconcile_artists
        import config

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            db_path = tmp_root / "library.db"
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                path BLOB,
                artist TEXT,
                albumartist TEXT,
                mb_artistid TEXT
            )
            """)
            c.execute("""
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY,
                albumartist TEXT
            )
            """)
            rel_file = Path("W/Wire/01 - Wire - 12XU.mp3")
            abs_file = tmp_root / rel_file
            abs_file.parent.mkdir(parents=True, exist_ok=True)
            abs_file.write_bytes(b"dummy")

            c.execute("INSERT INTO items (path, artist, albumartist, mb_artistid) VALUES (?, 'wire', 'wire', NULL)", (str(rel_file).encode("utf-8"),))
            c.execute("INSERT INTO items (path, artist, albumartist, mb_artistid) VALUES (?, 'Wire', 'Wire', 'mb-wire')", (b'other',))
            c.execute("INSERT INTO albums (albumartist) VALUES ('wire')")
            conn.commit()
            conn.close()

            written_calls = []
            with patch.object(config, "MUSIC_ROOT", tmp_root), \
                 patch("scripts.reconcile_artists.get_beets_db_path", return_value=db_path), \
                 patch("scripts.reconcile_artists.write_tags", side_effect=lambda p, u, dry_run=False: written_calls.append((p, u))):
                reconcile_artists(dry_run=False, fix=True)

            self.assertEqual(len(written_calls), 1)
            self.assertEqual(written_calls[0][0], abs_file)
            self.assertEqual(written_calls[0][1], {"artist": "Wire", "albumartist": "Wire"})

            # Verify DB updated
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            artists = [r[0] for r in c.execute("SELECT artist FROM items").fetchall()]
            albumartists = [r[0] for r in c.execute("SELECT albumartist FROM items").fetchall()]
            alb_artists = [r[0] for r in c.execute("SELECT albumartist FROM albums").fetchall()]
            conn.close()

            self.assertEqual(artists, ["Wire", "Wire"])
            self.assertEqual(albumartists, ["Wire", "Wire"])
            self.assertEqual(alb_artists, ["Wire"])


if __name__ == "__main__":
    unittest.main()

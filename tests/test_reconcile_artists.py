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


if __name__ == "__main__":
    unittest.main()

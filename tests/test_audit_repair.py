"""Tests for scripts/audit_repair.py."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.audit_repair import (
    clean_genre_tag,
    audit_file,
    find_matching_backup_file,
    resolve_fallback_genre,
)


class TestAuditRepair(unittest.TestCase):
    def test_clean_genre_tag_removes_junk_tokens(self):
        cleaned, removed = clean_genre_tag("Electronic / Genre / Ambient / other")
        self.assertEqual(cleaned, "Electronic / Ambient")
        self.assertIn("Genre", removed)
        self.assertIn("other", removed)

    def test_clean_genre_tag_all_junk(self):
        cleaned, removed = clean_genre_tag("unknown / genre / other")
        self.assertIsNone(cleaned)
        self.assertEqual(len(removed), 3)

    def test_clean_genre_tag_already_clean(self):
        cleaned, removed = clean_genre_tag("Rock / Alternative")
        self.assertEqual(cleaned, "Rock / Alternative")
        self.assertEqual(removed, [])

    def test_resolve_fallback_genre(self):
        root = Path("/music")
        self.assertEqual(resolve_fallback_genre(root / "metal" / "track.mp3", root), "Metal")
        self.assertEqual(resolve_fallback_genre(root / "0mixes" / "track.mp3", root), "Electronic")
        self.assertEqual(resolve_fallback_genre(root / "0random_good" / "chill" / "track.mp3", root), "Chill")
        self.assertEqual(resolve_fallback_genre(root / "0random_good" / "hiphop" / "track.mp3", root), "Hip-Hop")
        self.assertEqual(resolve_fallback_genre(root / "0random" / "hard" / "track.mp3", root), "Metal")
        self.assertIsNone(resolve_fallback_genre(root / "0compilations" / "album" / "track.mp3", root))

    def test_find_matching_backup_file(self):
        with TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            target_root = base / "target"
            backup_root = base / "backup"
            target_root.mkdir()
            backup_root.mkdir()

            t_file = target_root / "rock" / "band" / "01.mp3"
            t_file.parent.mkdir(parents=True)
            t_file.write_text("audio")

            b_file = backup_root / "rock" / "band" / "01.mp3"
            b_file.parent.mkdir(parents=True)
            b_file.write_text("audio")

            matched = find_matching_backup_file(t_file, target_root, backup_root)
            self.assertEqual(matched, b_file)


if __name__ == "__main__":
    unittest.main()

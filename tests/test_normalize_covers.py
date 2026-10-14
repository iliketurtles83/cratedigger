"""Tests for scripts/normalize_covers.py."""

from pathlib import Path
import tempfile
import unittest

from scripts.normalize_covers import (
    normalize_cover_in_folder,
    scan_and_normalize_covers,
    select_cover_candidate,
)


class NormalizeCoversTests(unittest.TestCase):
    def test_select_cover_candidate_single_image(self) -> None:
        img = Path("0413. Talking Heads - More Songs About Buildings And Food.jpg")
        cand, reason = select_cover_candidate([img])
        self.assertEqual(cand, img)
        self.assertEqual(reason, "single_image")

    def test_select_cover_candidate_already_canonical(self) -> None:
        cand, reason = select_cover_candidate([Path("cover.jpg"), Path("back.jpg")])
        self.assertIsNone(cand)
        self.assertEqual(reason, "already_canonical")

    def test_select_cover_candidate_extension_fix(self) -> None:
        cand, reason = select_cover_candidate([Path("cover.jpeg")])
        self.assertEqual(cand, Path("cover.jpeg"))
        self.assertEqual(reason, "extension_fix")

    def test_select_cover_candidate_front_pattern(self) -> None:
        front = Path("front.jpg")
        back = Path("back.jpg")
        cand, reason = select_cover_candidate([back, front])
        self.assertEqual(cand, front)
        self.assertEqual(reason, "front_pattern_match")

    def test_select_cover_candidate_ambiguous(self) -> None:
        cand, reason = select_cover_candidate([Path("scan1.jpg"), Path("scan2.jpg")])
        self.assertIsNone(cand)
        self.assertEqual(reason, "ambiguous_multiple_images")

    def test_normalize_cover_in_folder_dry_run_vs_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            album_dir = Path(tmp_dir) / "Talking Heads - More Songs"
            album_dir.mkdir()
            (album_dir / "01.mp3").touch()
            ripper_art = album_dir / "0413. Talking Heads - More Songs About Buildings And Food.jpg"
            ripper_art.touch()

            # Dry run: does not rename
            changed = normalize_cover_in_folder(album_dir, dry_run=True)
            self.assertTrue(changed)
            self.assertTrue(ripper_art.exists())
            self.assertFalse((album_dir / "cover.jpg").exists())

            # Real run: renames to cover.jpg
            changed = normalize_cover_in_folder(album_dir, dry_run=False)
            self.assertTrue(changed)
            self.assertFalse(ripper_art.exists())
            self.assertTrue((album_dir / "cover.jpg").exists())


if __name__ == "__main__":
    unittest.main()

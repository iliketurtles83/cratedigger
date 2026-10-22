"""tests/test_enrich_covers.py — Tests for scripts/enrich_covers.py and CAA operations."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from PIL import Image

import musicbrainzngs

from lib.mb import fetch_caa_cover, fetch_caa_release_group_cover, search_release_mbid
from lib.tags import write_tags
from scripts.enrich_covers import (
    enrich_album_cover,
    enrich_single_track,
    process_image,
    scan_and_enrich,
)


class EnrichCoversTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        # Create a sample 100x100 RGBA PNG in memory
        png_img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        buf = io.BytesIO()
        png_img.save(buf, format="PNG")
        self.sample_png_bytes = buf.getvalue()

        # Create a sample 1600x800 RGB JPEG in memory
        large_img = Image.new("RGB", (1600, 800), (0, 128, 255))
        buf = io.BytesIO()
        large_img.save(buf, format="JPEG", quality=90)
        self.sample_large_jpeg_bytes = buf.getvalue()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_album_dir(self, artist: str = "Arcade Fire", album: str = "Funeral") -> Path:
        album_dir = self.root / f"{artist} - {album}"
        album_dir.mkdir(parents=True, exist_ok=True)
        # Create a dummy mp3 track
        track = album_dir / "01.mp3"
        frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
        track.write_bytes(frame * 10)
        return album_dir

    def test_process_image_png_to_jpeg(self) -> None:
        jpeg_bytes, mime = process_image(self.sample_png_bytes, max_size=1200)
        self.assertEqual(mime, "image/jpeg")

        # Verify decoded image is valid RGB JPEG
        img = Image.open(io.BytesIO(jpeg_bytes))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(img.mode, "RGB")
        self.assertEqual(img.size, (100, 100))

    def test_process_image_resizing(self) -> None:
        jpeg_bytes, mime = process_image(self.sample_large_jpeg_bytes, max_size=1200)
        self.assertEqual(mime, "image/jpeg")

        img = Image.open(io.BytesIO(jpeg_bytes))
        # 1600x800 downsized to max 1200 should become 1200x600
        self.assertEqual(img.size, (1200, 600))

    def test_caa_404_no_retry(self) -> None:
        """Verify that an HTTP 404 ResponseError from CAA returns None immediately without retrying."""
        http_404 = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        response_error_404 = musicbrainzngs.ResponseError(cause=http_404)

        with patch("lib.mb.musicbrainzngs.get_image_front", side_effect=response_error_404) as mock_get, \
             patch("lib.mb.time.sleep") as mock_sleep:
            # When size=None and rate_limit_seconds=0.0, exactly 1 call is made (no fallback, no retries, no sleep)
            result = fetch_caa_cover("some-release-id", size=None, rate_limit_seconds=0.0)
            self.assertIsNone(result)
            self.assertEqual(mock_get.call_count, 1)
            mock_sleep.assert_not_called()

        with patch("lib.mb.musicbrainzngs.get_image_front", side_effect=response_error_404) as mock_get, \
             patch("lib.mb.time.sleep") as mock_sleep:
            # When size="1200" and rate_limit_seconds=0.0, 2 calls are made (1200px attempt + original fallback), both with 0 retries
            result = fetch_caa_cover("some-release-id", size="1200", rate_limit_seconds=0.0)
            self.assertIsNone(result)
            self.assertEqual(mock_get.call_count, 2)
            mock_sleep.assert_not_called()

    def test_caa_retry_on_503(self) -> None:
        """Verify that a transient error (e.g. 503 throttled) triggers retries up to MB_MAX_RETRIES."""
        http_503 = urllib.error.HTTPError("url", 503, "Service Unavailable", {}, None)
        transient_error = musicbrainzngs.WebServiceError(cause=http_503)

        with patch("lib.mb.musicbrainzngs.get_image_front", side_effect=transient_error) as mock_get, \
             patch("lib.mb.time.sleep"):
            result = fetch_caa_cover("some-release-id", size=None)
            self.assertIsNone(result)
            # Retried MB_MAX_RETRIES + 1 times (initial attempt + 3 retries = 4)
            self.assertEqual(mock_get.call_count, 4)

    def test_enrich_album_cover_already_canonical(self) -> None:
        album_dir = self._create_album_dir()
        cover_path = album_dir / "cover.jpg"
        cover_path.write_bytes(self.sample_png_bytes)

        review_items: list[dict] = []
        changed = enrich_album_cover(album_dir, dry_run=False, review_items=review_items)
        self.assertFalse(changed)
        self.assertEqual(len(review_items), 0)

    def test_enrich_album_cover_local_normalize(self) -> None:
        album_dir = self._create_album_dir()
        front_path = album_dir / "front.jpg"
        front_path.write_bytes(self.sample_png_bytes)

        review_items: list[dict] = []
        with patch("scripts.enrich_covers.fetch_caa_cover") as mock_fetch:
            changed = enrich_album_cover(album_dir, dry_run=False, review_items=review_items)
            self.assertTrue(changed)
            # Local front.jpg was renamed to cover.jpg; no CAA calls needed
            self.assertTrue((album_dir / "cover.jpg").exists())
            self.assertFalse(front_path.exists())
            mock_fetch.assert_not_called()

    def test_enrich_album_cover_caa_dry_run(self) -> None:
        album_dir = self._create_album_dir()
        track = album_dir / "01.mp3"
        write_tags(track, {"mb_albumid": "release-uuid-123"}, dry_run=False)

        review_items: list[dict] = []
        with patch("scripts.enrich_covers.fetch_caa_cover", return_value=self.sample_png_bytes) as mock_fetch:
            changed = enrich_album_cover(album_dir, dry_run=True, review_items=review_items)
            self.assertTrue(changed)
            mock_fetch.assert_called_once_with("release-uuid-123", size="1200")
            # Dry run must not create cover.jpg
            self.assertFalse((album_dir / "cover.jpg").exists())

    def test_enrich_album_cover_caa_live(self) -> None:
        album_dir = self._create_album_dir()
        track = album_dir / "01.mp3"
        write_tags(track, {"mb_albumid": "release-uuid-123"}, dry_run=False)

        review_items: list[dict] = []
        with patch("scripts.enrich_covers.fetch_caa_cover", return_value=self.sample_png_bytes):
            changed = enrich_album_cover(album_dir, dry_run=False, review_items=review_items)
            self.assertTrue(changed)
            cover_file = album_dir / "cover.jpg"
            self.assertTrue(cover_file.exists())
            # Image was converted from PNG to valid JPEG
            with Image.open(cover_file) as img:
                self.assertEqual(img.format, "JPEG")

    def test_enrich_missing_mbid_review_item(self) -> None:
        album_dir = self._create_album_dir()
        track = album_dir / "01.mp3"
        # Track has no tags at all
        review_items: list[dict] = []
        with patch("scripts.enrich_covers.search_release_mbid", return_value=(None, None)):
            changed = enrich_album_cover(album_dir, dry_run=False, review_items=review_items)
            self.assertFalse(changed)
            self.assertEqual(len(review_items), 1)
            self.assertEqual(review_items[0]["reason"], "no_mbid_found")

    def test_enrich_single_track_live(self) -> None:
        single_dir = self.root / "0singles"
        single_dir.mkdir(parents=True, exist_ok=True)
        track = single_dir / "The Cure - Boys Don't Cry.mp3"
        frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
        track.write_bytes(frame * 10)
        write_tags(track, {"mb_albumid": "single-release-uuid"}, dry_run=False)

        review_items: list[dict] = []
        with patch("scripts.enrich_covers.fetch_caa_cover", return_value=self.sample_png_bytes):
            changed = enrich_single_track(track, dry_run=False, review_items=review_items)
            self.assertTrue(changed)
            # Re-reading tags should now detect embedded artwork
            from lib.tags import has_embedded_artwork
            self.assertTrue(has_embedded_artwork(track))


if __name__ == "__main__":
    unittest.main()

"""Read-only playlist browsing and path-safety tests."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from cratedigger.web import playlists
from cratedigger.web.app import create_app


class PlaylistBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.playlists = self.music / "Playlists"
        self.artist = self.music / "A" / "Artist"
        self.playlists.mkdir(parents=True)
        self.artist.mkdir(parents=True)
        self.audio = self.artist / "One.mp3"
        self.audio.write_bytes(b"0123456789")
        (self.artist / "notes.txt").write_text("not audio", encoding="utf-8")
        self.outside = self.root / "outside.mp3"
        self.outside.write_bytes(b"outside")
        (self.artist / "link.mp3").symlink_to(self.outside)
        self.playlist = self.playlists / "Example & Friends.m3u8"
        lines = [
            "#EXTM3U",
            "#EXTINF:120,Artist - <script>alert(1)</script>",
            "../A/Artist/One.mp3",
            r"..\A\Artist\One.mp3",
            str(self.audio),
            "../A/Artist/missing.mp3",
            str(self.outside),
            r"C:\Music\track.mp3",
            "https://example.invalid/track.mp3",
            "../A/Artist/link.mp3",
            "../A/Artist/notes.txt",
            "../A/Artist",
            "../A/Artist/\x00.mp3",
            "#EXTINF:-1,Unattached label",
        ]
        self.playlist.write_bytes(("\ufeff" + "\r\n".join(lines) + "\r\n").encode("utf-8"))
        self.original = self.playlist.read_bytes()

    def client(self) -> TestClient:
        return TestClient(create_app(
            db_path=self.root / "state.db",
            music_root=self.music,
            features_path=self.root / "features.pkl",
            analytics_path=self.root / "analytics.sqlite3",
            logs_dir=self.root / "logs",
        ), base_url="http://127.0.0.1:8765")

    def test_listing_finds_nested_files_and_skips_symbolic_links(self) -> None:
        nested = self.playlists / "Nested"
        nested.mkdir()
        (nested / "Mixed.M3U8").write_text("#EXTM3U\n", encoding="utf-8")
        (self.playlists / "outside.m3u8").symlink_to(self.outside)
        (self.playlists / "linked-dir").symlink_to(self.root, target_is_directory=True)
        listing = playlists.list_playlists(self.music)
        self.assertEqual(listing["total"], 2)
        self.assertEqual(listing["skipped_links"], 2)
        self.assertEqual(
            [item["path"] for item in listing["entries"]],
            ["Example & Friends.m3u8", "Nested/Mixed.M3U8"],
        )
        self.assertEqual(playlists.list_playlists(self.music, query="mIxEd")["total"], 1)

    def test_inspection_preserves_order_labels_and_overlapping_duplicates(self) -> None:
        detail = playlists.inspect_playlist(self.music, self.playlist.name)
        self.assertEqual(detail["total"], 11)
        self.assertEqual(detail["counts"], {
            "available": 3, "missing": 1, "blocked": 4, "unsupported": 2, "inaccessible": 1,
        })
        self.assertEqual(detail["duplicates"], 2)
        self.assertEqual(detail["orphaned_labels"], 1)
        self.assertEqual(detail["entries"][0]["title"], "Artist - <script>alert(1)</script>")
        self.assertEqual([entry["number"] for entry in detail["entries"]], list(range(1, 12)))
        self.assertTrue(detail["entries"][1]["duplicate"])
        self.assertTrue(detail["entries"][2]["duplicate"])
        self.assertEqual(self.playlist.read_bytes(), self.original)
        self.assertEqual(self.audio.read_bytes(), b"0123456789")

    def test_filters_search_and_pagination_preserve_original_positions(self) -> None:
        detail = playlists.inspect_playlist(self.music, self.playlist.name, kind="duplicates")
        self.assertEqual([entry["number"] for entry in detail["entries"]], [2, 3])
        detail = playlists.inspect_playlist(self.music, self.playlist.name, kind="missing", query="MISSING")
        self.assertEqual([entry["number"] for entry in detail["entries"]], [4])
        self.playlist.write_text("#EXTM3U\n" + "../A/Artist/One.mp3\n" * 52, encoding="utf-8")
        detail = playlists.inspect_playlist(self.music, self.playlist.name, kind="duplicates", page=999)
        self.assertEqual((detail["total"], detail["duplicates"], detail["matched"]), (52, 51, 51))
        self.assertEqual((detail["page"], detail["pages"], len(detail["entries"])), (2, 2, 1))
        self.assertEqual(detail["entries"][0]["number"], 52)

    def test_listing_pagination_and_missing_directory(self) -> None:
        for number in range(52):
            (self.playlists / f"List {number:02}.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
        listing = playlists.list_playlists(self.music, query="List", page=999)
        self.assertEqual((listing["total"], listing["page"], listing["pages"], len(listing["entries"])), (52, 2, 2, 2))
        empty_music = self.root / "empty"
        empty_music.mkdir()
        self.assertFalse(playlists.list_playlists(empty_music)["exists"])

    def test_playlist_paths_cannot_escape_or_follow_symlinks(self) -> None:
        (self.playlists / "outside.m3u8").symlink_to(self.outside)
        (self.playlists / "linked-dir").symlink_to(self.root, target_is_directory=True)
        for value in ("../outside.mp3", str(self.playlist), "outside.m3u8", "linked-dir/test.m3u8",
                      "missing.m3u8", "folder\\track.m3u8", "notes.txt", "bad\x00.m3u8"):
            with self.subTest(path=value), self.assertRaises(ValueError):
                playlists.inspect_playlist(self.music, value)
        escape_root = self.root / "escape-music"
        escape_root.mkdir()
        (escape_root / "Playlists").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside"):
            playlists.list_playlists(escape_root)

    def test_invalid_encoding_and_inspection_limits_are_explicit(self) -> None:
        self.playlist.write_bytes(b"#EXTM3U\n\xff\n")
        with self.assertRaisesRegex(ValueError, "not valid UTF-8"):
            playlists.inspect_playlist(self.music, self.playlist.name)
        self.playlist.write_text("a\nb\nc\n", encoding="utf-8")
        with patch("cratedigger.web.playlists.MAX_PLAYLIST_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "inspection limit"):
                playlists.inspect_playlist(self.music, self.playlist.name)
        with patch("cratedigger.web.playlists.MAX_PLAYLIST_BYTES", 6):
            self.assertEqual(playlists.inspect_playlist(self.music, self.playlist.name)["total"], 3)
        with patch("cratedigger.web.playlists.MAX_ENTRIES", 2):
            with self.assertRaisesRegex(ValueError, "entry inspection limit"):
                playlists.inspect_playlist(self.music, self.playlist.name)
            self.playlist.write_text("a\nb\n", encoding="utf-8")
            self.assertEqual(playlists.inspect_playlist(self.music, self.playlist.name)["total"], 2)
        self.playlist.write_text("abc\n", encoding="utf-8")
        with patch("cratedigger.web.playlists.MAX_LINE_CHARS", 4):
            self.assertEqual(playlists.inspect_playlist(self.music, self.playlist.name)["total"], 1)
            self.playlist.write_text("abcd\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "line 1 exceeds"):
                playlists.inspect_playlist(self.music, self.playlist.name)

    def test_change_during_read_is_reported_instead_of_showing_partial_results(self) -> None:
        before = self.playlist.stat()
        changed = SimpleNamespace(
            st_dev=before.st_dev, st_ino=before.st_ino,
            st_size=before.st_size, st_mtime_ns=before.st_mtime_ns + 1,
        )
        with patch("cratedigger.web.playlists.os.fstat", side_effect=[before, changed]):
            with self.assertRaisesRegex(ValueError, "changed while reading"):
                playlists.inspect_playlist(self.music, self.playlist.name)

    def test_open_rechecks_file_type_and_does_not_follow_a_replaced_symlink(self) -> None:
        not_regular = SimpleNamespace(st_mode=stat.S_IFIFO)
        with patch("cratedigger.web.playlists.os.fstat", return_value=not_regular):
            with self.assertRaisesRegex(ValueError, "not a regular file"):
                playlists.inspect_playlist(self.music, self.playlist.name)
        if hasattr(os, "O_NOFOLLOW"):
            link = self.playlists / "changed.m3u8"
            link.symlink_to(self.outside)
            with patch("cratedigger.web.playlists._playlist_path", return_value=link):
                with self.assertRaisesRegex(ValueError, "Cannot read playlist"):
                    playlists.inspect_playlist(self.music, "changed.m3u8")

    def test_empty_playlist_and_cr_only_line_endings(self) -> None:
        self.playlist.write_text("#EXTM3U\n", encoding="utf-8")
        self.assertEqual(playlists.inspect_playlist(self.music, self.playlist.name)["total"], 0)
        self.assertIn("No playlist entries match", self.client().get(
            "/playlists/view", params={"path": self.playlist.name}
        ).text)
        self.playlist.write_bytes(b"#EXTM3U\r#EXTINF:2,One\r../A/Artist/One.mp3\r")
        result = playlists.inspect_playlist(self.music, self.playlist.name)
        self.assertEqual((result["total"], result["entries"][0]["title"]), (1, "One"))

    def test_routes_render_safely_without_feature_store_reads_or_mutations(self) -> None:
        client = self.client()
        with patch("pandas.read_pickle", side_effect=AssertionError("unexpected feature-store read")):
            index = client.get("/playlists")
            detail = client.get("/playlists/view", params={"path": self.playlist.name})
            filtered = client.get("/playlists/view", params={"path": self.playlist.name, "kind": "missing"})
        self.assertEqual(index.status_code, 200)
        self.assertIn("Example &amp; Friends.m3u8", index.text)
        self.assertIn("/ops#op-genre-playlists", index.text)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("11 matching entries", detail.text)
        self.assertIn("Artist - &lt;script&gt;alert(1)&lt;/script&gt;", detail.text)
        self.assertNotIn("<script>alert(1)</script>", detail.text)
        self.assertIn("External URLs and paths outside this library are not inspected", detail.text)
        self.assertIn("1 matching entry", filtered.text)
        self.assertIn("missing.mp3", filtered.text)
        self.assertNotIn("notes.txt", filtered.text)
        self.assertEqual(self.playlist.read_bytes(), self.original)
        self.assertEqual(self.audio.read_bytes(), b"0123456789")

    def test_available_audio_supports_ranges_and_outside_audio_is_blocked(self) -> None:
        client = self.client()
        detail = playlists.inspect_playlist(self.music, self.playlist.name)
        response = client.get(detail["entries"][0]["audio_url"], headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"2345")
        self.assertEqual(client.get("/media/audio", params={"path": str(self.outside)}).status_code, 403)

    def test_route_errors_and_filters_are_explicit(self) -> None:
        client = self.client()
        invalid = client.get("/playlists/view", params={"path": "../outside.m3u8"})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("relative .m3u8 file", invalid.text)
        self.assertEqual(client.get("/playlists/view", params={"path": self.playlist.name, "kind": "bad"}).status_code, 422)
        self.assertEqual(client.get("/playlists/view", params={"path": self.playlist.name, "page": 0}).status_code, 422)
        self.assertEqual(client.get("/playlists", params={"q": "x" * 201}).status_code, 422)
        self.playlist.write_bytes(b"\xff")
        invalid_encoding = client.get("/playlists/view", params={"path": self.playlist.name})
        self.assertEqual(invalid_encoding.status_code, 400)
        self.assertIn("not valid UTF-8", invalid_encoding.text)

    def test_route_pagination_preserves_selection_and_search(self) -> None:
        self.playlist.write_text("#EXTM3U\n" + "../A/Artist/One.mp3\n" * 52, encoding="utf-8")
        client = self.client()
        response = client.get(
            "/playlists/view", params={"path": self.playlist.name, "kind": "duplicates", "q": "One"}
        )
        self.assertIn("51 matching entries", response.text)
        self.assertIn("kind=duplicates&amp;q=One&amp;page=2", response.text)
        response = client.get(
            "/playlists/view", params={"path": self.playlist.name, "kind": "duplicates", "q": "One", "page": 999}
        )
        self.assertIn("Page 2 of 2", response.text)
        self.assertIn("52. One", response.text)


if __name__ == "__main__":
    unittest.main()

"""Tests for Cratedigger local FastAPI review & quarantine web UI (Phase 2)."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import pickle
import tempfile
import unittest

from fastapi.testclient import TestClient
import pandas as pd
from PIL import Image
from unittest.mock import patch

from cratedigger.db import open_db
from cratedigger.web.app import create_app
from lib.audiohash import audio_hash
from lib.tags import read_tags, write_tags


def _make_mp3(path: Path, *, fill: int = 0x00) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = b"\xff\xfb\x90\x64" + bytes([fill]) * 413
    path.write_bytes(frame * 12)
    return path


def _make_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color=color)
    im.save(path, format="JPEG")
    return path


class WebUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music_root = self.root / "music"
        self.music_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "cratedigger.db"
        self.features_path = self.root / "features.pkl"
        with open_db(self.db_path):
            pass
        self.app = create_app(
            db_path=self.db_path,
            music_root=self.music_root,
            features_path=self.features_path,
            logs_dir=self.root / "logs",
        )
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.client.get("/")
        self.client.headers["x-csrf-token"] = self.client.cookies["cratedigger_session"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _insert_review(self, *, item_key: str, queue_type: str, reason: str, path: Path, payload: dict) -> int:
        with open_db(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO review_items (item_key, queue_type, reason, path, payload_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', '2026-01-01T00:00:00Z')
                """,
                (item_key, queue_type, reason, str(path), json.dumps(payload)),
            )
            conn.commit()
            return int(cur.lastrowid)

    def test_root_redirects_and_review_renders(self) -> None:
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/review")

        r2 = self.client.get("/review")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("Review Queue", r2.text)

    def test_library_search_filter_and_pagination(self) -> None:
        with open_db(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO tracks (audio_hash, canonical_path, artist, album, title, year, genre, file_format, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (f"a1:{i:04d}", f"/music/track-{i:02d}.mp3", "Artist & Co", "Record / One",
                     f"Song {i:02d}", "2001", "Rock / Alternative", "mp3", "2026-01-01")
                    for i in range(52)
                ] + [
                    ("a1:other", "/music/Fallback.mp3", "Other", "Album", None,
                     None, None, "mp3", "2026-01-01")
                ],
            )
            conn.commit()
        page = self.client.get("/library")
        self.assertIn("53 matching", page.text)
        self.assertIn("Page 1 of 2", page.text)
        self.assertIn("Artist &amp; Co", page.text)
        self.assertEqual(self.client.get("/library", params={"q": "song 12"}).text.count("Song 12"), 1)
        filtered = self.client.get("/library", params={"artist": "Artist & Co", "album": "Record / One", "page": 2})
        self.assertIn("52 matching", filtered.text)
        self.assertIn("Page 2 of 2", filtered.text)
        self.assertNotIn("Fallback", filtered.text)
        fallback = self.client.get("/library", params={"q": "Fallback"})
        self.assertIn("Fallback", fallback.text)
        self.assertNotIn("Song 12", fallback.text)
        literal = self.client.get("/library", params={"q": "%"})
        self.assertIn("No indexed tracks match", literal.text)
        self.assertEqual(self.client.get("/library", params={"page": 0}).status_code, 422)

    def test_operations_catalog_and_queued_preview(self) -> None:
        page = self.client.get("/ops")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Operations", page.text)
        self.assertIn("Process incoming music", page.text)
        self.assertIn("Extract audio features", page.text)

        with patch("cratedigger.web.app.start_job", return_value="job-abc") as start:
            response = self.client.post(
                "/ops/tag/start",
                data={"csrf_token": self.client.cookies["cratedigger_session"], "mode": "preview", "folder": "A"},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("/jobs/job-abc", response.headers["location"])
        self.assertEqual(start.call_args.kwargs["operation_id"], "tag")
        self.assertEqual(start.call_args.kwargs["raw_params"], {"folder": "A"})
        self.assertEqual(start.call_args.kwargs["mode"], "preview")

        bad = self.client.post(
            "/ops/not-registered/start",
            data={"csrf_token": self.client.cookies["cratedigger_session"], "mode": "preview"},
            follow_redirects=False,
        )
        self.assertIn("/ops?err=", bad.headers["location"])

    def test_persistent_job_history_detail_and_terminal_stream(self) -> None:
        log_path = self.root / "logs" / "job.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("$ tool --dry-run\npreview complete\n", encoding="utf-8")
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, status, "
                "exit_code, log_path, created_at, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "job-fixture", "tag", '{"folder":"A"}', "preview", "fingerprint", '["python"]', "[]",
                    "succeeded", 0, str(log_path), "2026-01-01", "2026-01-01", "2026-01-01",
                ),
            )
            conn.commit()
        history = self.client.get("/jobs")
        self.assertEqual(history.status_code, 200)
        self.assertIn("job-fixture", history.text)
        detail = self.client.get("/jobs/job-fixture")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("preview complete", detail.text)
        self.assertIn("Parameters", detail.text)
        stream = self.client.get("/jobs/job-fixture/events")
        self.assertEqual(stream.status_code, 200)
        self.assertIn('"done": true', stream.text)
        self.assertEqual(self.client.get("/jobs/not-found").status_code, 404)

    def test_local_host_and_cross_site_protection(self) -> None:
        client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        for host in ("evil.example:8765", "localhost.evil.example", "127.0.0.1@evil.example", "localhost:99999"):
            with self.subTest(host=host):
                self.assertEqual(client.get("/", headers={"host": host}).status_code, 400)
        self.assertEqual(client.post("/quarantine/empty").status_code, 403)

        page = client.get("/review")
        token = client.cookies["cratedigger_session"]
        self.assertIn(f'name="csrf_token" value="{token}"', page.text)
        self.assertIn("HttpOnly", page.headers["set-cookie"])
        self.assertIn("SameSite=strict", page.headers["set-cookie"])
        for origin in ("http://evil.example", "null", "http://127.0.0.1:8766", "http://localhost:8765"):
            with self.subTest(origin=origin):
                response = client.post(
                    "/review/bulk",
                    data={"action": "dismiss", "csrf_token": token},
                    headers={"origin": origin},
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(
            client.post("/review/bulk", data={"action": "dismiss", "csrf_token": "wrong"}).status_code, 403
        )
        response = client.post(
            "/review/bulk",
            data={"action": "dismiss", "csrf_token": token},
            headers={"origin": "http://127.0.0.1:8765"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        forged = TestClient(self.app, base_url="http://127.0.0.1:8765")
        forged.cookies.set("cratedigger_session", "x" * 43 + "." + "0" * 64)
        self.assertEqual(
            forged.post("/review/bulk", data={"action": "dismiss"}, headers={"x-csrf-token": forged.cookies["cratedigger_session"]}).status_code,
            403,
        )

    def test_review_post_forms_are_not_nested_and_have_csrf_tokens(self) -> None:
        track = _make_mp3(self.music_root / "A" / "Artist" / "Album" / "01 - Track.mp3")
        self._insert_review(item_key="form1", queue_type="actionable", reason="incomplete_metadata", path=track, payload={})
        page = self.client.get("/review")

        class FormParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.in_form = False
                self.post = False
                self.has_token = False
                self.post_forms = 0

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                values = dict(attrs)
                if tag == "form":
                    if self.in_form:
                        raise AssertionError("Nested form")
                    self.in_form = True
                    self.post = values.get("method", "").lower() == "post"
                    self.has_token = False
                if tag == "input" and self.in_form and values.get("name") == "csrf_token":
                    self.has_token = bool(values.get("value"))

            def handle_endtag(self, tag: str) -> None:
                if tag == "form":
                    if self.post:
                        if not self.has_token:
                            raise AssertionError("POST form without CSRF token")
                        self.post_forms += 1
                    self.in_form = False

        parser = FormParser()
        parser.feed(page.text)
        self.assertGreaterEqual(parser.post_forms, 5)

    def test_grouped_no_genre_apply(self) -> None:
        folder = self.music_root / "C" / "Cure" / "Disintegration (1989)"
        t1 = _make_mp3(folder / "01 - Plainsong.mp3", fill=0x11)
        t2 = _make_mp3(folder / "02 - Pictures of You.mp3", fill=0x22)
        id1 = self._insert_review(item_key="ng1", queue_type="actionable", reason="no_genre", path=t1, payload={"reason": "no_genre"})
        id2 = self._insert_review(item_key="ng2", queue_type="actionable", reason="no_genre", path=t2, payload={"reason": "no_genre"})

        page = self.client.get("/review?reason=no_genre&group=folder")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Disintegration (1989)", page.text)

        post = self.client.post(
            "/review/group-genre",
            data={"item_ids_csv": f"{id1},{id2}", "genre_value": "Rock / Post-Punk"},
            follow_redirects=False,
        )
        self.assertEqual(post.status_code, 303)
        self.assertEqual((read_tags(t1) or {}).get("genre"), "Rock / Post-Punk")
        self.assertEqual((read_tags(t2) or {}).get("genre"), "Rock / Post-Punk")

        with open_db(self.db_path) as conn:
            statuses = [r["status"] for r in conn.execute("SELECT status FROM review_items ORDER BY id").fetchall()]
        self.assertEqual(statuses, ["resolved", "resolved"])

    def test_pick_cover_art_promotes_and_quarantines_others(self) -> None:
        album = self.music_root / "D" / "Daft Punk" / "Discovery (2001)"
        _make_mp3(album / "01 - One More Time.mp3")
        img1 = _make_image(album / "front_alt.jpg", (400, 400), (200, 20, 20))
        img2 = _make_image(album / "back_scan.jpg", (300, 300), (20, 20, 200))
        item_id = self._insert_review(
            item_key="cov1",
            queue_type="actionable",
            reason="ambiguous_cover_art",
            path=album,
            payload={"reason": "ambiguous_cover_art", "candidates": ["front_alt.jpg", "back_scan.jpg"]},
        )

        r = self.client.get("/review?reason=ambiguous_cover_art")
        self.assertEqual(r.status_code, 200)
        self.assertIn("front_alt.jpg", r.text)

        post = self.client.post(
            f"/review/{item_id}/pick-cover",
            data={"selected_path": str(img1)},
            follow_redirects=False,
        )
        self.assertEqual(post.status_code, 303)
        self.assertTrue((album / "cover.jpg").is_file())
        self.assertFalse(img1.exists())
        self.assertFalse(img2.exists())
        q_files = list((self.music_root / "0quarantine").rglob("*.jpg"))
        self.assertEqual(len(q_files), 2)

    def test_rename_conflict_keep_both_and_quarantine_source(self) -> None:
        album = self.music_root / "M" / "Miles Davis" / "Kind of Blue (1959)"
        src1 = _make_mp3(album / "Track 01.mp3", fill=0x33)
        dst1 = _make_mp3(album / "01 - So What.mp3", fill=0x44)
        df = pd.DataFrame([{"path": str(src1), "v": 1}, {"path": str(dst1), "v": 2}])
        with self.features_path.open("wb") as fh:
            pickle.dump(df, fh)

        item_id = self._insert_review(
            item_key="rc1",
            queue_type="actionable",
            reason="rename_conflict",
            path=src1,
            payload={"reason": "rename_conflict", "target": str(dst1)},
        )

        page = self.client.get("/review?reason=rename_conflict")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/media/audio", page.text)

        post = self.client.post(
            f"/review/{item_id}/resolve-conflict",
            data={"decision": "keep_both"},
            follow_redirects=False,
        )
        self.assertEqual(post.status_code, 303)
        self.assertFalse(src1.exists())
        self.assertTrue(dst1.exists())
        alt_files = [p for p in album.glob("*[alternate-*") if p.is_file()]
        self.assertEqual(len(alt_files), 1)
        with self.features_path.open("rb") as fh:
            updated_df = pickle.load(fh)
        self.assertIn(str(alt_files[0]), updated_df["path"].tolist())

    def test_mixed_tags_apply_folder_tag(self) -> None:
        album = self.music_root / "R" / "Radiohead" / "Kid A (2000)"
        t1 = _make_mp3(album / "01 - Everything.mp3", fill=0x51)
        t2 = _make_mp3(album / "02 - Kid A.mp3", fill=0x52)
        write_tags(t1, {"album": "Kid A"}, dry_run=False)
        write_tags(t2, {"album": "Kid A (Bonus)"}, dry_run=False)
        item_id = self._insert_review(
            item_key="mt1",
            queue_type="actionable",
            reason="mixed_tags",
            path=album,
            payload={"reason": "mixed_tags", "field": "album"},
        )

        post = self.client.post(
            f"/review/{item_id}/apply-folder-tag",
            data={"field": "album", "value": "Kid A"},
            follow_redirects=False,
        )
        self.assertEqual(post.status_code, 303)
        self.assertEqual((read_tags(t1) or {}).get("album"), "Kid A")
        self.assertEqual((read_tags(t2) or {}).get("album"), "Kid A")

    def test_health_page_and_bulk_dismiss(self) -> None:
        f = self.music_root / "X" / "XTC" / "Skylarking (1986)" / "01 - Summer.mp3"
        hid = self._insert_review(
            item_key="hlth1",
            queue_type="health",
            reason="caa_no_cover",
            path=f,
            payload={"reason": "caa_no_cover"},
        )
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertIn("caa_no_cover", r.text)

        post = self.client.post(
            "/review/bulk",
            data={"action": "dismiss", "item_ids": [str(hid)], "return_url": "/health"},
            follow_redirects=False,
        )
        self.assertEqual(post.status_code, 303)
        with open_db(self.db_path) as conn:
            st = conn.execute("SELECT status FROM review_items WHERE id = ?", (hid,)).fetchone()["status"]
        self.assertEqual(st, "dismissed")

    def test_duplicate_hash_quarantine_relink_rating_restore_empty_and_undo(self) -> None:
        keep = _make_mp3(self.music_root / "A" / "Artist" / "Album (2020)" / "01 - Song.mp3", fill=0x77)
        drop = _make_mp3(self.music_root / "0random" / "01 - Song copy.mp3", fill=0x77)
        write_tags(keep, {"artist": "Artist", "title": "Song", "rating": "0.4"}, dry_run=False)
        write_tags(drop, {"artist": "Artist", "title": "Song", "rating": "1.0"}, dry_run=False)
        ah = audio_hash(str(keep))

        pl_dir = self.music_root / "Playlists"
        pl_dir.mkdir(parents=True, exist_ok=True)
        pl = pl_dir / "Favorites.m3u8"
        pl.write_text("#EXTM3U\n../0random/01 - Song copy.mp3\n", encoding="utf-8")

        with self.features_path.open("wb") as fh:
            pickle.dump(pd.DataFrame([{"path": str(drop), "v": 1}, {"path": str(keep), "v": 1}]), fh)

        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tracks (audio_hash, canonical_path, updated_at) VALUES (?, ?, '2026-01-01T00:00:00Z')",
                (ah, str(keep)),
            )
            conn.execute(
                "INSERT INTO track_paths (path, audio_hash, size, is_canonical, updated_at) VALUES (?, ?, ?, 1, '2026-01-01T00:00:00Z')",
                (str(keep), ah, keep.stat().st_size),
            )
            conn.execute(
                "INSERT INTO track_paths (path, audio_hash, size, is_canonical, updated_at) VALUES (?, ?, ?, 0, '2026-01-01T00:00:00Z')",
                (str(drop), ah, drop.stat().st_size),
            )
            conn.commit()

        dup_page = self.client.get("/quarantine?tab=duplicates")
        self.assertEqual(dup_page.status_code, 200)
        self.assertIn(ah, dup_page.text)

        dedup_post = self.client.post(
            "/quarantine/dedup",
            data={"audio_hash_val": ah, "keep_path": str(keep), "quarantine_path": str(drop)},
            follow_redirects=False,
        )
        self.assertEqual(dedup_post.status_code, 303)
        self.assertFalse(drop.exists())
        self.assertTrue(keep.exists())
        # Higher rating from quarantined duplicate was preserved on canonical copy.
        self.assertEqual((read_tags(keep) or {}).get("rating"), "1.0")
        # Playlist relinked from quarantined copy to canonical copy.
        self.assertIn("A/Artist/Album (2020)/01 - Song.mp3", pl.read_text(encoding="utf-8"))

        # Appears in quarantined files tab and can be undone via /undo.
        files_page = self.client.get("/quarantine?tab=files")
        self.assertEqual(files_page.status_code, 200)
        self.assertIn("01 - Song copy.mp3", files_page.text)

        undo_page = self.client.get("/undo")
        self.assertEqual(undo_page.status_code, 200)
        with open_db(self.db_path) as conn:
            run_id = conn.execute("SELECT run_id FROM undo_journal ORDER BY id DESC LIMIT 1").fetchone()["run_id"]
        undo_post = self.client.post(f"/undo/{run_id}", follow_redirects=False)
        self.assertEqual(undo_post.status_code, 303)
        self.assertTrue(drop.exists())

        # Re-dedup then restore directly and test empty quarantine.
        self.client.post(
            "/quarantine/dedup",
            data={"audio_hash_val": ah, "keep_path": str(keep), "quarantine_path": str(drop)},
        )
        q_item = next((self.music_root / "0quarantine").rglob("*.mp3"))
        restore_post = self.client.post("/quarantine/restore", data={"quarantined_path": str(q_item)}, follow_redirects=False)
        self.assertEqual(restore_post.status_code, 303)
        self.assertTrue(drop.exists())

        self.client.post(
            "/quarantine/dedup",
            data={"audio_hash_val": ah, "keep_path": str(keep), "quarantine_path": str(drop)},
        )
        empty_post = self.client.post("/quarantine/empty", follow_redirects=False)
        self.assertEqual(empty_post.status_code, 303)
        self.assertEqual(list((self.music_root / "0quarantine").rglob("*")), [])

    def test_media_endpoints_and_traversal_guard(self) -> None:
        img = _make_image(self.music_root / "A" / "Artist" / "Album" / "cover.jpg", (50, 50), (0, 255, 0))
        aud = _make_mp3(self.music_root / "A" / "Artist" / "Album" / "01 - Track.mp3")

        r_img = self.client.get("/media/image", params={"path": str(img)})
        self.assertEqual(r_img.status_code, 200)

        r_aud = self.client.get("/media/audio", params={"path": str(aud)})
        self.assertEqual(r_aud.status_code, 200)

        outside = self.root / "secret.jpg"
        _make_image(outside, (10, 10), (1, 2, 3))
        bad_img = self.client.get("/media/image", params={"path": str(outside)})
        self.assertEqual(bad_img.status_code, 403)

        bad_aud = self.client.get("/media/audio", params={"path": "/etc/passwd"})
        self.assertEqual(bad_aud.status_code, 403)


if __name__ == "__main__":
    unittest.main()

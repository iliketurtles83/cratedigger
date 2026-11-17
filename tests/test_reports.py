"""Tests for the read-only report browser and reviewed-evidence handoff."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from cratedigger.db import open_db
from cratedigger.jobs import start_job
from cratedigger.web import reports
from cratedigger.web.app import create_app


class ReportBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.db = self.root / "state.db"
        self.app = create_app(
            db_path=self.db, music_root=self.music, features_path=self.root / "features.pkl",
            analytics_path=self.root / "analytics.sqlite3", logs_dir=self.root / "logs",
        )
        self.app.state.repo_root = self.root
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.client.get("/")
        self.token = self.client.cookies["cratedigger_session"]

    def write_json(self, relative: str, payload: object) -> Path:
        path = self.reports / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_listing_search_pagination_and_missing_directory(self) -> None:
        for index in range(55):
            self.write_json(f"beets/report-{index:02}.json", {"units": []})
        self.write_json("genres/summary.json", {"summary": {}})
        page = self.client.get("/reports")
        self.assertEqual(page.status_code, 200)
        self.assertIn("56 matching report files", page.text)
        self.assertIn("Page 1 of 2", page.text)
        self.assertEqual(page.text.count("/reports/view?path="), 50)
        filtered = self.client.get("/reports", params={"q": "GENRES"})
        self.assertIn("1 matching report files", filtered.text)
        self.assertIn("genres/summary.json", filtered.text)
        second = self.client.get("/reports", params={"page": 2})
        self.assertIn("Page 2 of 2", second.text)
        self.assertEqual(second.text.count("/reports/view?path="), 6)
        self.assertEqual(self.client.get("/reports?page=0").status_code, 422)

    def test_json_sections_sort_paginate_escape_and_offer_raw(self) -> None:
        self.write_json("test/report.json", {
            "summary": {"warning": "<script>alert(1)</script>"},
            "units": [{"name": f"Unit {i}", "score": i, "items": [{"title": "<unsafe>"}]} for i in range(55)],
            "other": [{"note": "Second section"}],
        })
        response = self.client.get("/reports/view", params={"path": "test/report.json", "sort": "score"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Page 1 of 2", response.text)
        self.assertIn("Unit 49", response.text)
        self.assertNotIn("Unit 50", response.text)
        self.assertLess(response.text.index("Unit 2<"), response.text.index("Unit 10<"))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;unsafe&gt;", response.text)
        second = self.client.get("/reports/view", params={
            "path": "test/report.json", "section": "units", "sort": "score", "page": 2,
        })
        self.assertIn("Unit 54", second.text)
        self.assertIn("Page 2 of 2", second.text)
        descending = self.client.get("/reports/view", params={
            "path": "test/report.json", "section": "units", "sort": "score", "order": "desc",
        })
        self.assertLess(descending.text.index("Unit 54<"), descending.text.index("Unit 10<"))
        other = self.client.get("/reports/view", params={"path": "test/report.json", "section": "other"})
        self.assertIn("Second section", other.text)
        raw = self.client.get("/reports/view", params={"path": "test/report.json", "view": "raw"})
        self.assertIn("Raw JSON", raw.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", raw.text)
        self.assertEqual(
            self.client.get("/reports/view", params={"path": "test/report.json", "sort": "missing"}).status_code,
            400,
        )

    def test_path_escape_and_symlinks_are_rejected(self) -> None:
        outside = self.root / "private.txt"
        outside.write_text("private", encoding="utf-8")
        (self.reports / "outside-link.txt").symlink_to(outside)
        (self.reports / "outside-dir").symlink_to(self.root, target_is_directory=True)
        self.write_json("safe/report.json", {"summary": "inside"})
        listing = self.client.get("/reports")
        self.assertNotIn("outside-link.txt", listing.text)
        self.assertNotIn("outside-dir", listing.text)
        for value in ("../private.txt", str(outside), "outside-link.txt", "outside-dir/private.txt"):
            with self.subTest(path=value):
                view = self.client.get("/reports/view", params={"path": value})
                self.assertEqual(view.status_code, 400)
                self.assertNotIn("private", view.text)
                download = self.client.get("/reports/file", params={"path": value})
                self.assertEqual(download.status_code, 404)

    def test_invalid_large_and_binary_files_have_explicit_safe_handling(self) -> None:
        (self.reports / "bad.json").write_text('{"bad":', encoding="utf-8")
        invalid = self.client.get("/reports/view", params={"path": "bad.json"})
        self.assertIn("Invalid JSON", invalid.text)
        self.assertIn("{&#34;bad&#34;:", invalid.text)
        (self.reports / "large.json").write_bytes(b"x" * (reports.MAX_PREVIEW_BYTES + 1))
        large = self.client.get("/reports/view", params={"path": "large.json"})
        self.assertIn("exceeds the 5 MiB preview limit", large.text)
        self.assertIn("Download", large.text)
        (self.reports / "bad.txt").write_bytes(b"\xff")
        non_utf8 = self.client.get("/reports/view", params={"path": "bad.txt"})
        self.assertIn("not valid UTF-8", non_utf8.text)
        (self.reports / "unsafe.html").write_text("<script>bad()</script>", encoding="utf-8")
        download = self.client.get("/reports/file", params={"path": "unsafe.html"})
        self.assertEqual(download.headers["content-type"], "application/octet-stream")
        self.assertIn("attachment", download.headers["content-disposition"])
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            self.client.get("/reports/file", params={"path": "unsafe.html", "inline": "true"}).status_code, 400
        )
        (self.reports / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        image = self.client.get("/reports/file", params={"path": "plot.png", "inline": "true"})
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertEqual(image.headers["x-content-type-options"], "nosniff")

    def test_bpm_evidence_handoff_requires_same_bytes_and_only_starts_preview(self) -> None:
        track = self.music / "a.mp3"
        track.write_bytes(b"fixture")
        stat = track.stat()
        path = self.write_json("bpm/audit.json", {
            "target_root": "A", "scanned": 1, "dry_run": True, "counts": {"update_mismatch": 1},
            "changes": [{
                "path": str(track), "action": "update_mismatch", "old_bpm": "110", "new_bpm": "120",
                "genre": "Rock", "source_dev": stat.st_dev, "source_ino": stat.st_ino,
                "source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
            }],
        })
        response = self.client.get("/reports/view", params={"path": "bpm/audit.json"})
        self.assertIn("Preview BPM repairs", response.text)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        form = {
            "csrf_token": self.token, "path": "bpm/audit.json",
            "digest": digest, "operation": "repair-bpm",
        }
        with patch("cratedigger.web.app.start_job", return_value="bpm-job") as start:
            result = self.client.post("/reports/preview", data=form, follow_redirects=False)
        self.assertEqual(result.status_code, 303)
        self.assertIn("/jobs/bpm-job", result.headers["location"])
        self.assertEqual(start.call_args.kwargs["mode"], "preview")
        self.assertEqual(start.call_args.kwargs["raw_params"], {"apply_report": str(path)})
        self.assertEqual(start.call_args.kwargs["expected_report_digest"], digest)
        path.write_text('{"changes": []}', encoding="utf-8")
        with patch("cratedigger.web.app.start_job") as start:
            changed = self.client.post("/reports/preview", data=form, follow_redirects=False)
        self.assertIn("changed after it was opened", parse_qs(urlsplit(changed.headers["location"]).query)["err"][0])
        start.assert_not_called()

    def test_old_bpm_reports_are_viewable_but_not_actionable(self) -> None:
        self.write_json("bpm/old.json", {
            "target_root": "A", "scanned": 1,
            "changes": [{"path": "/music/a.mp3", "action": "update_mismatch", "new_bpm": "120"}],
        })
        page = self.client.get("/reports/view", params={"path": "bpm/old.json"})
        self.assertIn("not a dry-run audit with source evidence", page.text)
        self.assertNotIn("Preview BPM repairs", page.text)

    def test_beets_release_selection_groups_all_matched_units(self) -> None:
        payload = {
            "online": True,
            "units": [
                {"path": "A/Album 1", "status": "matched", "tracks": 1, "items": [{"mb_albumid": "release-1"}]},
                {"path": "B/Album 2", "status": "matched", "tracks": 1, "items": [{"mb_albumid": "release-1"}]},
                {"path": "C/Bad", "status": "matched", "tracks": 1, "items": [{"mb_albumid": []}]},
            ],
        }
        path = self.write_json("beets/audit.json", payload)
        page = self.client.get("/reports/view", params={"path": "beets/audit.json"})
        self.assertIn("2 unit(s)", page.text)
        self.assertIn("A/Album 1, B/Album 2", page.text)
        self.assertIn("every matched unit", page.text)
        form = {
            "csrf_token": self.token, "path": "beets/audit.json",
            "digest": hashlib.sha256(path.read_bytes()).hexdigest(), "operation": "apply-beets",
            "release_id": "release-1",
        }
        with patch("cratedigger.web.app.start_job", return_value="beets-job") as start:
            result = self.client.post("/reports/preview", data=form, follow_redirects=False)
        self.assertIn("/jobs/beets-job", result.headers["location"])
        self.assertEqual(start.call_args.kwargs["raw_params"], {"report": str(path), "release_id": "release-1"})
        with patch("cratedigger.web.app.start_job") as start:
            invalid = self.client.post(
                "/reports/preview", data={**form, "release_id": "other"}, follow_redirects=False
            )
        self.assertIn("Select a matched release", parse_qs(urlsplit(invalid.headers["location"]).query)["err"][0])
        start.assert_not_called()

    def test_job_detail_links_only_existing_contained_report_artifacts(self) -> None:
        output = self.write_json("genres/ui-result.json", {"summary": {}})
        log_path = self.root / "logs" / "job.log"
        with open_db(self.db) as conn:
            conn.execute(
                "INSERT INTO jobs (id, operation_id, params_json, mode, fingerprint, argv_json, locks_json, status, "
                "log_path, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("report-job", "reconcile-genres", "{}", "preview", "f",
                 json.dumps(["python", "script", "--report", str(output)]), "[]", "succeeded",
                 str(log_path), "2026-01-01", "2026-01-01"),
            )
            conn.commit()
        detail = self.client.get("/jobs/report-job")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("genres/ui-result.json", detail.text)
        self.assertIn("/reports/view?path=genres%2Fui-result.json", detail.text)
        job = {"operation_id": "reconcile-genres", "argv": ["python", "--report", str(self.root / "secret")]}
        self.assertEqual(reports.job_report_artifacts(job, self.reports), [])

    def test_job_checks_digest_again_after_report_page_check(self) -> None:
        path = self.write_json("bpm/audit.json", {"changes": []})
        with patch("cratedigger.jobs.external_analysis_running", return_value=False), patch(
            "cratedigger.jobs.config.MUSIC_ROOT", self.music
        ), patch("cratedigger.jobs.subprocess.Popen") as popen, patch(
            "cratedigger.jobs._fingerprint", return_value="fingerprint"
        ), open_db(self.db) as conn:
            with self.assertRaisesRegex(ValueError, "changed after it was opened"):
                start_job(
                    conn, db_path=self.db, operation_id="repair-bpm",
                    raw_params={"apply_report": str(path)}, mode="preview",
                    repo_root=self.root, logs_dir=self.root / "logs", music_root=self.music,
                    expected_report_digest="0" * 64,
                )
            popen.assert_not_called()
            popen.return_value.pid = 12345
            job_id = start_job(
                conn, db_path=self.db, operation_id="repair-bpm",
                raw_params={"apply_report": str(path)}, mode="preview",
                repo_root=self.root, logs_dir=self.root / "logs", music_root=self.music,
                expected_report_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            row = conn.execute("SELECT argv_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.assertIn("--verify-report-evidence", json.loads(row["argv_json"]))


if __name__ == "__main__":
    unittest.main()

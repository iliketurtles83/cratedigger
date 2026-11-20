"""Seed playlist evidence, create-only publication, jobs, and UI tests."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import numpy as np

from cratedigger.analytics import build_snapshot
from cratedigger.db import open_db
from cratedigger.jobs import get_job, start_job
from cratedigger.ops.registry import REPOSITORY_ROOT
from cratedigger.playlist_builder import create_playlist, playlist_filename, preview_playlist, read_plan, write_plan
from cratedigger.web.app import create_app
from cratedigger.web.playlists import inspect_playlist
from cratedigger.web.similarity import SimilarityCache
from test_similarity import _fixture, _runners


class PlaylistBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.source = self.root / "features.pkl"
        self.snapshot = self.root / "analytics.sqlite3"
        _fixture(self.root).to_pickle(self.source)
        with patch("lib.descriptors.load_head_sessions", return_value=_runners()):
            build_snapshot(self.source, self.snapshot, dry_run=False)
        self.index = SimilarityCache().get(self.snapshot)
        assert self.index is not None
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for name in ("cratedigger", "scripts", "lib", "config.py"):
            (self.repo / name).symlink_to(REPOSITORY_ROOT / name)
        self.db = self.root / "state.db"
        self.logs = self.root / "logs"
        self.app = create_app(
            db_path=self.db, music_root=self.music, features_path=self.source,
            analytics_path=self.snapshot, logs_dir=self.logs,
        )
        self.app.state.repo_root = self.repo
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")

    def plan(self, **kwargs):
        return preview_playlist(self.index, self.music, seed="seed", name="Seed & Friends", **kwargs)

    def report(self):
        return write_plan(self.plan(), self.repo / "reports")

    def job_args(self, report):
        return dict(
            db_path=self.db, operation_id="create-playlist", raw_params={"report": str(report)},
            repo_root=self.repo, logs_dir=self.logs, music_root=self.music,
        )

    def test_preview_freezes_seed_order_and_explicitly_skips_unavailable_neighbors(self) -> None:
        plan = self.plan()
        self.assertEqual(plan["requested"], 42)
        self.assertEqual([track["audio_hash"] for track in plan["tracks"]], ["seed", "close"])
        self.assertEqual({track["audio_hash"] for track in plan["skipped"]}, {"far", "boundary"})
        self.assertEqual(plan["filename"], "Seed & Friends.m3u8")
        self.assertFalse((self.music / "Playlists").exists())
        self.assertEqual(len(self.plan(count=1)["tracks"]), 1)

    def test_default_produces_exactly_42_ordered_tracks_when_enough_are_available(self) -> None:
        template = self.index.by_hash["seed"]
        tracks = []
        for number in range(60):
            path = self.music / f"track-{number}.mp3"
            path.write_bytes(b"audio")
            tracks.append(replace(
                template, audio_hash=f"hash-{number}", path=str(path),
                title=f"Track {number}", vector_index=number,
            ))
        index = replace(
            self.index, tracks=tuple(tracks), by_hash={track.audio_hash: track for track in tracks},
            vector_tracks=tuple(tracks),
            vectors=np.tile(self.index.vectors[template.vector_index], (60, 1)),
        )
        plan = preview_playlist(index, self.music, seed="hash-0", name="Forty Two")
        self.assertEqual([track["audio_hash"] for track in plan["tracks"]], [f"hash-{i}" for i in range(42)])
        self.assertEqual(plan["skipped"], [])
        result = create_playlist(write_plan(plan, self.repo / "reports"), self.music, dry_run=False)
        self.assertEqual(result["tracks"], 42)
        self.assertEqual(inspect_playlist(self.music, "Forty Two.m3u8")["total"], 42)

    def test_names_and_seeds_are_validated_without_silent_normalization(self) -> None:
        for name in ("", "CON", ".hidden", "../escape", "folder/list", r"folder\list", "bad: name", "bad\nname",
                     "bad.m3u8 ", "x" * 116, "\x00"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                playlist_filename(name)
        for seed in ("unknown", "silent", "boundary"):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                preview_playlist(self.index, self.music, seed=seed, name="Test")
        for count in (0, 201, True):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.plan(count=count)

    def test_dry_run_and_apply_preserve_audio_and_feature_store(self) -> None:
        report = self.report()
        audio = {path: path.read_bytes() for path in self.music.rglob("*.mp3") if not path.is_symlink()}
        source = self.source.read_bytes()
        snapshot = self.snapshot.read_bytes()
        preview = create_playlist(report, self.music)
        self.assertTrue(preview["dry_run"])
        self.assertFalse((self.music / "Playlists").exists())
        applied = create_playlist(report, self.music, dry_run=False)
        target = Path(applied["playlist"])
        self.assertFalse(applied["dry_run"])
        text = target.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#EXTM3U\n#EXTINF:-1,Test Artist - Seed & Song\n"))
        self.assertIn("../A/Test Artist/Album/seed.mp3\n", text)
        detail = inspect_playlist(self.music, target.name)
        self.assertEqual((detail["total"], detail["counts"]["available"]), (2, 2))
        self.assertEqual(list(target.parent.glob(".playlist-*")), [])
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(self.snapshot.read_bytes(), snapshot)
        self.assertEqual({path: path.read_bytes() for path in audio}, audio)
        with self.assertRaisesRegex(ValueError, "already exists"):
            create_playlist(report, self.music, dry_run=False)

    def test_collisions_and_symbolic_linked_destinations_are_refused(self) -> None:
        report = self.report()
        directory = self.music / "Playlists"
        directory.mkdir()
        existing = directory / "SEED & FRIENDS.M3U8"
        existing.write_bytes(b"keep")
        with self.assertRaisesRegex(ValueError, "already exists"):
            create_playlist(report, self.music, dry_run=False)
        self.assertEqual(existing.read_bytes(), b"keep")
        existing.unlink()
        directory.rmdir()
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic"):
            create_playlist(report, self.music, dry_run=False)
        self.assertFalse((self.root / "Seed & Friends.m3u8").exists())

    def test_racing_destination_is_not_overwritten(self) -> None:
        report = self.report()
        target = self.music / "Playlists" / "Seed & Friends.m3u8"
        def race(*args, **kwargs):
            target.write_bytes(b"created elsewhere")
            raise FileExistsError("destination appeared")
        with patch("cratedigger.playlist_builder.os.link", side_effect=race):
            with self.assertRaises(FileExistsError):
                create_playlist(report, self.music, dry_run=False)
        self.assertEqual(target.read_bytes(), b"created elsewhere")
        self.assertEqual(list(target.parent.glob(".playlist-*")), [])

    def test_changed_audio_refuses_publication_and_cleans_temporary_files(self) -> None:
        report = self.report()
        track = Path(self.plan()["tracks"][0]["path"])
        track.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            create_playlist(report, self.music, dry_run=False)
        self.assertFalse((self.music / "Playlists").exists())
        report = self.report()
        with patch("cratedigger.playlist_builder._verify_tracks", side_effect=[None, ValueError("changed")]):
            with self.assertRaisesRegex(ValueError, "changed"):
                create_playlist(report, self.music, dry_run=False)
        self.assertEqual(list((self.music / "Playlists").iterdir()), [])

    def test_malformed_reports_and_different_roots_are_refused(self) -> None:
        report = self.report()
        plan = self.plan()
        mutations = [
            {"version": True}, {"music_root": str(self.root)}, {"filename": "../escape.m3u8"},
            {"requested": True}, {"tracks": []}, {"seed": "wrong"},
            {"tracks": [plan["tracks"][0]] * 2}, {"skipped": "invalid"},
        ]
        for changes in mutations:
            bad = copy.deepcopy(plan)
            bad.update(changes)
            report.write_text(json.dumps(bad), encoding="utf-8")
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                create_playlist(report, self.music, dry_run=False)
        report.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "JSON"):
            read_plan(report, self.music)

    def test_outside_and_injected_playlist_paths_cannot_be_published(self) -> None:
        report = self.report()
        plan = self.plan()
        for path in (str(self.root / "outside.mp3"), "../outside.mp3", "bad\n#EXTINF:-1,Injected.mp3",
                     str(self.music / r"back\slash.mp3")):
            altered = copy.deepcopy(plan)
            altered["tracks"][0]["path"] = path
            report.write_text(json.dumps(altered), encoding="utf-8")
            with self.subTest(path=path), self.assertRaises(ValueError):
                create_playlist(report, self.music, dry_run=False)
        self.assertFalse((self.music / "Playlists").exists())

    def test_builder_defaults_navigation_csrf_and_escaped_metadata(self) -> None:
        page = self.client.get("/playlists/build?seed=close")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="count" value="42"', page.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page.text)
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertIn('href="/playlists/build"', self.client.get("/playlists").text)
        self.assertIn('href="/playlists/build?seed=seed"', self.client.get("/insights/similarity?seed=seed").text)
        blocked = self.client.post("/playlists/build/preview", data={"seed": "seed", "name": "Test"})
        self.assertEqual(blocked.status_code, 403)
        self.assertFalse((self.repo / "reports").exists())

    def test_builder_preserves_fields_and_surfaces_name_collisions(self) -> None:
        directory = self.music / "Playlists"
        directory.mkdir()
        (directory / "Taken.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
        self.client.get("/playlists/build")
        response = self.client.post("/playlists/build/preview", data={
            "csrf_token": self.client.cookies["cratedigger_session"],
            "seed": "seed", "name": "Taken", "count": "17",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("already exists", response.text)
        self.assertIn('name="name" value="Taken"', response.text)
        self.assertIn('name="count" value="17"', response.text)
        self.assertFalse((self.repo / "reports").exists())

    def test_builder_starts_reviewed_job_and_job_page_displays_immutable_evidence(self) -> None:
        self.client.get("/playlists/build?seed=seed")
        with patch("cratedigger.jobs.subprocess.Popen") as popen:
            popen.return_value.pid = 12345
            response = self.client.post("/playlists/build/preview", data={
                "csrf_token": self.client.cookies["cratedigger_session"],
                "seed": "seed", "name": "New Playlist", "count": "42",
            }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/jobs/", response.headers["location"])
        job_id = response.headers["location"].split("/jobs/")[1].split("?")[0]
        with open_db(self.db) as conn:
            job = get_job(conn, job_id)
        report = Path(job["params"]["report"])
        report.write_text("changed original", encoding="utf-8")
        with patch("cratedigger.web.app.reconcile_jobs"):
            detail = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Reviewed playlist: New Playlist.m3u8", detail.text)
        self.assertIn("Skipped neighbors", detail.text)
        self.assertIn("Close &lt;script&gt;", detail.text)
        self.assertFalse((self.music / "Playlists").exists())

    def test_preview_gate_binds_report_and_real_cli_creates_the_reviewed_playlist(self) -> None:
        report = self.report()
        kwargs = self.job_args(report)
        with open_db(self.db) as conn:
            with patch("cratedigger.jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 12345
                with self.assertRaisesRegex(ValueError, "successful preview"):
                    start_job(conn, mode="apply", **kwargs)
                preview_id = start_job(conn, mode="preview", **kwargs)
            preview = get_job(conn, preview_id)
            subprocess.run(preview["argv"], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True)
            self.assertFalse((self.music / "Playlists").exists())
            conn.execute("UPDATE jobs SET status='succeeded', finished_at='2026-11-20T10:00:00+00:00' WHERE id=?", (preview_id,))
            conn.execute("DELETE FROM job_locks WHERE job_id=?", (preview_id,))
            conn.commit()
            original = report.read_bytes()
            report.write_bytes(original + b"\n")
            with self.assertRaisesRegex(ValueError, "successful preview"):
                start_job(conn, mode="apply", expected_preview_id=preview_id, **kwargs)
            report.write_bytes(original)
            with patch("cratedigger.jobs.subprocess.Popen") as popen:
                popen.return_value.pid = 12345
                applied_id = start_job(conn, mode="apply", expected_preview_id=preview_id, **kwargs)
            applied = get_job(conn, applied_id)
            result = subprocess.run(applied["argv"], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True)
            self.assertIn("@@playlist-result", result.stdout)
        self.assertEqual(inspect_playlist(self.music, "Seed & Friends.m3u8")["total"], 2)


if __name__ == "__main__":
    unittest.main()

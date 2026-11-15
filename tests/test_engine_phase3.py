"""Integration & unit tests for Cratedigger Phase 3 unified engine."""

from __future__ import annotations

import json
from pathlib import Path
import pickle
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from PIL import Image

from cratedigger.cli import main as cli_main
from cratedigger.config_schema import Settings, apply_preset, load_settings, save_settings
from cratedigger.core import audio_hash, has_embedded_artwork, read_tags, write_tags
from cratedigger.core.artists import build_artist_index, format_artists_tag, snap_artist_name, split_featured_artists
from cratedigger.db import open_db
from cratedigger.engine.executor import execute_plan, undo_plan_run
from cratedigger.engine.metadata import beets_match_album, resolve_genre_4step
from cratedigger.engine.planner import build_plan
from cratedigger.engine.scanner import scan_scope
from cratedigger.engine.transcoder import transcode_to_mp3


def _make_mp3(path: Path, *, fill: int = 0x00) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = b"\xff\xfb\x90\x64" + bytes([fill]) * 413
    path.write_bytes(frame * 12)
    return path


def _make_cover(path: Path, color: tuple[int, int, int] = (20, 120, 220)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (300, 300), color=color).save(path, format="JPEG")
    return path


def _make_ogg_with_ffmpeg(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise unittest.SkipTest("ffmpeg not available")
    proc = subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.25",
            "-c:a",
            "libvorbis",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return path


class Phase3EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.music_root = self.root / "music"
        self.music_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "cratedigger.db"
        self.features_path = self.root / "features.pkl"
        with open_db(self.db_path):
            pass

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_settings_persistence_and_presets(self) -> None:
        with open_db(self.db_path) as conn:
            s = load_settings(conn, music_root=self.music_root)
            self.assertEqual(s.small_release_threshold, 3)
            s = apply_preset(s, "flat_artist_root")
            self.assertFalse(s.letter_bucketing)
            s.genre_remap_rules = {"IDM": "Electronic"}
            save_settings(conn, s)

            reloaded = load_settings(conn, music_root=self.music_root)
            self.assertEqual(reloaded.preset_name, "flat_artist_root")
            self.assertFalse(reloaded.letter_bucketing)
            self.assertEqual(reloaded.genre_remap_rules, {"IDM": "Electronic"})

    def test_featured_split_and_artist_snapping(self) -> None:
        p1, a1 = split_featured_artists("Daft Punk feat. Pharrell Williams")
        self.assertEqual(p1, "Daft Punk")
        self.assertEqual(a1, ["Daft Punk", "Pharrell Williams"])
        self.assertEqual(format_artists_tag(a1), "Daft Punk / Pharrell Williams")

        # Band names with '&' must NOT split when no 'feat.' marker is present
        p2, a2 = split_featured_artists("Simon & Garfunkel")
        self.assertEqual(p2, "Simon & Garfunkel")
        self.assertEqual(a2, ["Simon & Garfunkel"])

        existing = _make_mp3(self.music_root / "A" / "Arcade Fire" / "Arcade Fire - Funeral (2004)" / "01 - Track.mp3")
        write_tags(existing, {"albumartist": "Arcade Fire", "mb_albumartistid": "mbid-af-1"}, dry_run=False)

        with open_db(self.db_path) as conn:
            build_artist_index(conn, self.music_root)
            # Snap by mb_artistid & comparison key ("The Arcade Fire" -> "Arcade Fire")
            name, bucket, path = snap_artist_name(
                conn, "The Arcade Fire", music_root=self.music_root, mb_artistid="mbid-af-1"
            )
            self.assertEqual(name, "Arcade Fire")
            self.assertEqual(bucket, "A")
            self.assertEqual(path, self.music_root / "A" / "Arcade Fire")

            name2, _, _ = snap_artist_name(conn, "the arcade fire", music_root=self.music_root)
            self.assertEqual(name2, "Arcade Fire")

    def test_4step_genre_resolution_consensus_remap_and_effnet(self) -> None:
        t = _make_mp3(self.music_root / "incoming" / "song.mp3")
        with open_db(self.db_path) as conn:
            s = load_settings(conn, music_root=self.music_root)
            s.genre_remap_rules = {"IDM": "Electronic / Ambient"}

            # Step 1: Remap existing tag
            r1 = resolve_genre_4step(t, {"genre": "IDM"}, conn, settings=s, online=False)
            self.assertEqual(r1["source"], "existing")
            self.assertEqual(r1["genre"], "Electronic / Ambient")

            # Step 3: Artist library consensus
            conn.execute(
                """
                INSERT INTO tracks (audio_hash, canonical_path, artist, genre, updated_at)
                VALUES ('h1', '/x.mp3', 'Boards of Canada', 'Electronic', '2026-01-01T00:00:00Z')
                """
            )
            conn.commit()
            r3 = resolve_genre_4step(t, {"artist": "Boards of Canada"}, conn, settings=s, online=False)
            self.assertEqual(r3["source"], "artist_consensus")
            self.assertEqual(r3["genre"], "Electronic")

            # Step 4: EffNet auto-apply vs review fallback
            vec = np.zeros(4, dtype=np.float32)
            vec[2] = 0.52
            with (
                patch("cratedigger.engine.metadata.get_style_classes", return_value=["Rock---Punk", "Jazz---Bebop", "Electronic---Techno", "Soul---Funk"]),
                patch("cratedigger.engine.metadata.extract_embedding", return_value=vec),
            ):
                r4 = resolve_genre_4step(t, {"artist": "Unknown"}, conn, settings=s, online=False, use_effnet=True)
                self.assertEqual(r4["source"], "effnet")
                self.assertEqual(r4["action"], "apply")
                self.assertIn("Electronic", r4["genre"])

                vec[2] = 0.24
                r4_low = resolve_genre_4step(t, {"artist": "Unknown"}, conn, settings=s, online=False, use_effnet=True)
                self.assertEqual(r4_low["action"], "review")

    def test_transcode_ogg_to_mp3_preserves_tags_ratings_and_art(self) -> None:
        src = _make_ogg_with_ffmpeg(self.music_root / "incoming" / "01 - Sine.ogg")
        cover = _make_cover(self.music_root / "incoming" / "cover.jpg")
        write_tags(
            src,
            {
                "artist": "Test Artist",
                "album": "Test Album",
                "title": "Sine Wave",
                "genre": "Electronic",
                "rating": "0.8",
                "playcount": "7",
                "mb_trackid": "11111111-1111-1111-1111-111111111111",
            },
            dry_run=False,
        )
        dst = self.music_root / "T" / "Test Artist" / "0singles" / "Test Artist - Sine Wave.mp3"
        out = transcode_to_mp3(src, dst, bitrate="320k", cover_bytes=cover.read_bytes(), dry_run=False)
        self.assertTrue(out.is_file())
        tags = read_tags(out) or {}
        self.assertEqual(tags.get("artist"), "Test Artist")
        self.assertEqual(tags.get("title"), "Sine Wave")
        self.assertEqual(tags.get("rating"), "0.8")
        self.assertEqual(tags.get("playcount"), "7")
        self.assertEqual(tags.get("mb_trackid"), "11111111-1111-1111-1111-111111111111")
        self.assertTrue(has_embedded_artwork(out))

    def test_scanner_classifications_mixes_soundtracks_random_and_complete_ep(self) -> None:
        # 1. Exempt personal mix under 0mixes & 0shacks
        _make_mp3(self.music_root / "0mixes" / "Late Night Mix.mp3", fill=0x10)
        _make_mp3(self.music_root / "0shacks" / "Shack Session 1.mp3", fill=0x11)
        # 2. Accidental full 6-track tagged album inside 0mixes -> flagged
        for i in range(1, 7):
            p = _make_mp3(self.music_root / "0mixes" / "Full Album (2020)" / f"0{i} - T{i}.mp3", fill=0x20 + i)
            write_tags(p, {"album": "Full Album", "artist": "Some Band"}, dry_run=False)
        # 3. Complete 2-track EP (track="1/2", "2/2") -> kept as album
        ep1 = _make_mp3(self.music_root / "incoming" / "Band - Demo EP (2022)" / "01 - A.mp3", fill=0x31)
        ep2 = _make_mp3(self.music_root / "incoming" / "Band - Demo EP (2022)" / "02 - B.mp3", fill=0x32)
        write_tags(ep1, {"artist": "Band", "album": "Demo EP", "track": "1/2"}, dry_run=False)
        write_tags(ep2, {"artist": "Band", "album": "Demo EP", "track": "2/2"}, dry_run=False)
        # 4. Single-composer soundtrack vs VA soundtrack
        ost1 = _make_mp3(self.music_root / "soundtrack" / "Blade Runner (1982)" / "01 - Main.mp3", fill=0x41)
        write_tags(ost1, {"artist": "Vangelis", "album": "Blade Runner", "year": "1982"}, dry_run=False)
        # 5. 0random loose track -> loose_singles
        rnd = _make_mp3(self.music_root / "0random" / "01 - Gem.mp3", fill=0x51)
        write_tags(rnd, {"artist": "Gem Artist", "title": "Gem", "genre": "Electronic"}, dry_run=False)

        with open_db(self.db_path) as conn:
            s = load_settings(conn, music_root=self.music_root)
            res = scan_scope(self.music_root, settings=s, conn=conn)

        by_name = {u.folder.name: u for u in res.units}
        self.assertEqual(by_name["0mixes"].kind, "exempt_mix")
        self.assertEqual(by_name["0shacks"].kind, "exempt_mix")
        self.assertIn("full_album_inside_mix_folder", by_name["Full Album (2020)"].notes)
        self.assertEqual(by_name["Band - Demo EP (2022)"].kind, "album")
        self.assertEqual(by_name["Blade Runner (1982)"].kind, "soundtrack_score")
        self.assertEqual(by_name["0random"].kind, "loose_singles")

    def test_plan_apply_and_undo_end_to_end(self) -> None:
        # Existing library album with a track whose audio matches a duplicate drop in incoming
        canon = _make_mp3(
            self.music_root / "B" / "Burial" / "Burial - Untrue (2007)" / "01 - Burial - Archangel.mp3",
            fill=0x88,
        )
        write_tags(canon, {"artist": "Burial", "albumartist": "Burial", "album": "Untrue", "title": "Archangel", "genre": "Electronic", "rating": "0.4"}, dry_run=False)
        dup_ah = audio_hash(str(canon))

        # Existing target file for differing-hash collision check
        colliding_target = _make_mp3(
            self.music_root / "B" / "Burial" / "0singles" / "Burial - Ghost Hardware.mp3",
            fill=0x99,
        )

        # Features store
        with self.features_path.open("wb") as fh:
            pickle.dump(pd.DataFrame([{"path": str(canon), "v": 1}]), fh)

        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO tracks (audio_hash, canonical_path, updated_at) VALUES (?, ?, '2026-01-01T00:00:00Z')",
                (dup_ah, str(canon)),
            )
            conn.execute(
                "INSERT INTO track_paths (path, audio_hash, size, is_canonical, updated_at) VALUES (?, ?, ?, 1, '2026-01-01T00:00:00Z')",
                (str(canon), dup_ah, canon.stat().st_size),
            )
            conn.commit()

        # Incoming small incomplete release (2 tracks -> dissolved into 0singles) + cover.jpg + feat. credit + duplicate + collision
        inc = self.music_root / "incoming"
        small_dir = inc / "Loose Duo"
        _make_cover(small_dir / "cover.jpg")
        s1 = _make_mp3(small_dir / "01 - Track One.mp3", fill=0xAA)
        write_tags(s1, {"artist": "Burial feat. Four Tet", "album": "Partial Album", "title": "Moth", "genre": "Electronic", "track": "1"}, dry_run=False)

        dup_drop = _make_mp3(inc / "Dupe Drop" / "Archangel copy.mp3", fill=0x88)
        write_tags(dup_drop, {"artist": "Burial", "title": "Archangel", "rating": "1.0"}, dry_run=False)

        collision_src = _make_mp3(inc / "Ghost Collision" / "Ghost Hardware.mp3", fill=0xBB)
        write_tags(collision_src, {"artist": "Burial", "title": "Ghost Hardware", "genre": "Electronic", "track": "1"}, dry_run=False)

        with open_db(self.db_path) as conn:
            s = load_settings(conn, music_root=self.music_root)
            plan = build_plan(inc, conn, settings=s, online=False)
            self.assertGreaterEqual(plan["summary"]["actions_count"], 3)

            exec_res = execute_plan(
                conn,
                plan["plan_id"],
                settings=s,
                features_path=self.features_path,
                dry_run=False,
            )
            self.assertEqual(exec_res["failed"], 0)
            self.assertEqual(exec_res["status"], "applied")

        # Verify small release dissolved: moved to B/Burial/0singles/Burial - Moth.mp3, album cleared, feat split, cover embedded
        expected_single = self.music_root / "B" / "Burial" / "0singles" / "Burial - Moth.mp3"
        self.assertTrue(expected_single.is_file())
        s1_tags = read_tags(expected_single) or {}
        self.assertIsNone(s1_tags.get("album"))
        self.assertEqual(s1_tags.get("artists"), "Burial / Four Tet")
        self.assertTrue(has_embedded_artwork(expected_single))

        # Verify playlists updated
        all_singles_pl = (self.music_root / "Playlists" / "All_Singles.m3u8").read_text(encoding="utf-8")
        self.assertIn("B/Burial/0singles/Burial - Moth.mp3", all_singles_pl)

        # Verify duplicate quarantined + max rating merged onto canonical copy
        self.assertFalse(dup_drop.exists())
        self.assertEqual((read_tags(canon) or {}).get("rating"), "1.0")
        self.assertEqual(len(list((self.music_root / "0quarantine").rglob("*.mp3"))), 1)

        # Verify differing-hash collision did NOT clobber target and created rename_conflict review item
        self.assertTrue(collision_src.exists())
        self.assertTrue(colliding_target.exists())
        with open_db(self.db_path) as conn:
            rc = conn.execute("SELECT reason FROM review_items WHERE reason = 'rename_conflict'").fetchall()
            self.assertEqual(len(rc), 1)

            # Verify full undo reverts everything
            reverted = undo_plan_run(
                conn,
                exec_res["run_id"],
                music_root=self.music_root,
                features_path=self.features_path,
            )
            self.assertGreater(reverted, 0)

        self.assertFalse(expected_single.exists())
        self.assertTrue(s1.is_file())
        self.assertEqual((read_tags(s1) or {}).get("album"), "Partial Album")
        self.assertTrue(dup_drop.is_file())
        self.assertFalse((self.music_root / "Playlists" / "All_Singles.m3u8").exists())

    def test_cli_scan_plan_apply_undo_dry_run_default(self) -> None:
        inc = self.music_root / "incoming"
        f = _make_mp3(inc / "Solo" / "Track.mp3", fill=0xCD)
        write_tags(f, {"artist": "Solo Artist", "title": "Solo Song", "genre": "Rock"}, dry_run=False)

        rc_scan = cli_main(["--db", str(self.db_path), "scan", str(inc), "--music-root", str(self.music_root)])
        self.assertEqual(rc_scan, 0)

        rc_plan = cli_main(["--db", str(self.db_path), "plan", str(inc), "--music-root", str(self.music_root)])
        self.assertEqual(rc_plan, 0)

        # Default apply is dry-run -> file remains untouched
        rc_dry = cli_main(["--db", str(self.db_path), "apply", "1", "--music-root", str(self.music_root)])
        self.assertEqual(rc_dry, 0)
        self.assertTrue(f.exists())

    def test_offline_beets_helper_returns_none_gracefully(self) -> None:
        d = self.music_root / "Empty"
        d.mkdir(parents=True, exist_ok=True)
        with open_db(self.db_path) as conn:
            self.assertIsNone(beets_match_album(d, conn=conn, online=False))


if __name__ == "__main__":
    unittest.main()

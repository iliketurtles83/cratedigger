"""Tests for cratedigger.db and cratedigger.review_migration (Phase 1)."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

import config
from cratedigger.db import open_db, review_counts, sync_features_into_db, upsert_review_item
from cratedigger.review_migration import migrate_review_json


class TestDatabaseAndMigration(unittest.TestCase):
    def test_sync_features_into_db_separates_tracks_and_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "cratedigger.db"
            features_path = root / "features.pkl"

            df = pd.DataFrame([
                {
                    "audio_hash": "a1:1111",
                    "path": "/music/0compilations/Pitchfork/01 - Song.mp3",
                    "mtime": 1000.0,
                    "size": 1234,
                    "artist_tag": "Artist",
                    "album_tag": "Pitchfork",
                    "year_tag": "2010",
                    "genre_tag": "Indie",
                    "rating": 0.8,
                    "duration_ms": 200000,
                    "error": None,
                },
                {
                    "audio_hash": "a1:1111",
                    "path": "/music/A/Artist/Artist - Debut (2010)/01 - Artist - Song.mp3",
                    "mtime": 1000.0,
                    "size": 1234,
                    "artist_tag": "Artist",
                    "album_tag": "Debut",
                    "track_tag": "Song title",
                    "year_tag": "2010",
                    "genre_tag": "Indie",
                    "rating": 1.0,
                    "duration_ms": 200000,
                    "error": None,
                },
            ])
            df.to_pickle(features_path)

            with open_db(db_path) as conn:
                summary = sync_features_into_db(conn, features_path)
                self.assertEqual(summary, {"tracks": 1, "paths": 2, "duplicate_hashes": 1})

                track = conn.execute("SELECT * FROM tracks WHERE audio_hash = 'a1:1111'").fetchone()
                self.assertEqual(
                    track["canonical_path"],
                    "/music/A/Artist/Artist - Debut (2010)/01 - Artist - Song.mp3",
                )
                self.assertEqual(track["title"], "Song title")
                df = df.drop(columns=["track_tag"])
                df.to_pickle(features_path)
                sync_features_into_db(conn, features_path)
                self.assertEqual(
                    conn.execute("SELECT title FROM tracks WHERE audio_hash = 'a1:1111'").fetchone()["title"],
                    "Song title",
                )
                paths = conn.execute(
                    "SELECT path, is_canonical FROM track_paths WHERE audio_hash = 'a1:1111' ORDER BY is_canonical DESC"
                ).fetchall()
                self.assertEqual(len(paths), 2)
                self.assertEqual(paths[0]["is_canonical"], 1)
                self.assertEqual(paths[1]["is_canonical"], 0)

    def test_upsert_preserves_manual_resolution(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cratedigger.db"
            with open_db(db_path) as conn:
                upsert_review_item(
                    conn,
                    item_key="k1",
                    queue_type="actionable",
                    reason="no_genre",
                    path="/tmp/a.mp3",
                    status="resolved",
                    resolution={"genre": "Rock"},
                )
                upsert_review_item(
                    conn,
                    item_key="k1",
                    queue_type="actionable",
                    reason="no_genre",
                    path="/tmp/a.mp3",
                    status="pending",
                )
                row = conn.execute("SELECT status, resolution_json FROM review_items WHERE item_key = 'k1'").fetchone()
                self.assertEqual(row["status"], "resolved")
                self.assertEqual(json.loads(row["resolution_json"]), {"genre": "Rock"})

    def test_migrate_review_json_triages_queues_and_respects_dry_run(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "cratedigger.db"
            review_path = root / "review.json"

            tagged_song = root / "tagged.mp3"
            tagged_song.write_bytes(b"fake")

            cover_dir = root / "album_with_cover"
            cover_dir.mkdir()
            (cover_dir / "cover.jpg").write_bytes(b"img")

            consensus_dir = root / "consensus_album"
            consensus_dir.mkdir()
            tracks = [consensus_dir / f"0{i} - Track.mp3" for i in range(1, 5)]
            for t in tracks:
                t.write_bytes(b"fake")

            health_song = root / "health.mp3"
            health_song.write_bytes(b"fake")

            review_data = [
                {"path": str(root / "gone.mp3"), "reason": "no_genre"},
                {"path": str(tagged_song), "reason": "no_genre"},
                {"path": str(cover_dir), "reason": "ambiguous_cover_art", "images": ["a.jpg", "b.jpg"]},
                {"path": str(consensus_dir), "reason": "inconsistent_tags", "field": "year", "variants": ["2001", "2002"]},
                {"path": str(health_song), "reason": "single_no_mbid", "artist": "X", "title": "Y"},
            ]
            review_path.write_text(json.dumps(review_data), encoding="utf-8")

            def fake_read_tags(path: Path):
                if path == tagged_song:
                    return {"genre": "Electronic"}
                if path == tracks[3]:
                    return {"year": "2002"}
                return {"year": "2001"}

            with patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}), patch(
                "cratedigger.review_migration.read_tags", side_effect=fake_read_tags
            ), patch("cratedigger.review_migration.write_tags") as mock_write:
                dry_summary = migrate_review_json(
                    review_path, db_path, fix_consensus=True, dry_run=True
                )
                self.assertFalse(db_path.exists())
                mock_write.assert_not_called()
                self.assertEqual(dry_summary["consensus_suggestions"], 1)
                self.assertEqual(dry_summary["counts"]["actionable"]["pruned"], 1)
                self.assertEqual(dry_summary["counts"]["actionable"]["auto_resolved"], 2)
                self.assertEqual(dry_summary["counts"]["actionable"]["pending"], 1)
                self.assertEqual(dry_summary["counts"]["health"]["pending"], 1)

                live_summary = migrate_review_json(
                    review_path, db_path, fix_consensus=True, dry_run=False
                )
                self.assertTrue(db_path.exists())
                mock_write.assert_called_once_with(tracks[3], {"year": "2001"}, dry_run=False)
                self.assertEqual(live_summary["counts"]["actionable"]["auto_resolved"], 3)

            with open_db(db_path) as conn:
                counts = review_counts(conn)
                self.assertEqual(counts["actionable"]["pruned"], 1)
                self.assertEqual(counts["actionable"]["auto_resolved"], 3)
                self.assertEqual(counts["health"]["pending"], 1)


if __name__ == "__main__":
    unittest.main()

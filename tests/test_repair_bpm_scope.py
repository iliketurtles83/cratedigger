"""Scope checks for BPM repair reports and feature-store candidates."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.repair_bpm import _iter_suspect_files_from_features, _iter_target_files, apply_report


class RepairBpmScopeTests(unittest.TestCase):
    def test_report_preflights_all_paths_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            inside = music / "inside.mp3"
            outside = root / "outside.mp3"
            inside.touch()
            outside.touch()
            report = root / "report.json"
            report.write_text(json.dumps({
                "changes": [
                    {"path": str(path), "action": "update_mismatch", "new_bpm": "120"}
                    for path in (inside, outside)
                ]
            }), encoding="utf-8")
            with patch("scripts.repair_bpm.write_tags") as write:
                with self.assertRaisesRegex(ValueError, "outside the selected library root"):
                    apply_report(report, dry_run=False, scope_root=music)
                write.assert_not_called()
                apply_report(report, dry_run=False)
                self.assertEqual(write.call_count, 2)

    def test_feature_store_candidates_cannot_escape_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            outside = root / "outside.mp3"
            outside.touch()
            features = root / "features.pkl"
            pd.DataFrame([{
                "path": str(outside), "error": None, "bpm_tag": 1.0, "genre_tag": "Rock"
            }]).to_pickle(features)
            with self.assertRaisesRegex(ValueError, "outside the selected library root"):
                _iter_suspect_files_from_features(features, scope_root=music)

    def test_verified_report_preflights_source_and_tag_evidence_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracks = [root / "one.mp3", root / "two.mp3"]
            for path in tracks:
                path.write_bytes(b"fixture")
            changes = []
            for path in tracks:
                stat = path.stat()
                changes.append({
                    "path": str(path), "action": "update_mismatch", "old_bpm": "110", "new_bpm": "120",
                    "genre": "Rock", "source_dev": stat.st_dev, "source_ino": stat.st_ino,
                    "source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
                })
            report = root / "audit.json"
            report.write_text(json.dumps({"changes": changes}), encoding="utf-8")
            with patch("scripts.repair_bpm.read_tags", return_value={"bpm": "110", "genre": "Rock"}), patch(
                "scripts.repair_bpm.write_tags"
            ) as write:
                result = apply_report(report, dry_run=True, scope_root=root, verify_evidence=True)
                self.assertEqual(result["applied"], 2)
                write.assert_not_called()
                applied = apply_report(report, dry_run=False, scope_root=root, verify_evidence=True)
                self.assertEqual(applied["applied"], 2)
                self.assertEqual(write.call_count, 2)
                write.reset_mock()
                tracks[1].write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "changed since the BPM audit"):
                    apply_report(report, dry_run=False, scope_root=root, verify_evidence=True)
                write.assert_not_called()

            stat = tracks[1].stat()
            changes[1].update(
                source_dev=stat.st_dev, source_ino=stat.st_ino,
                source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
            )
            report.write_text(json.dumps({"changes": changes}), encoding="utf-8")
            with patch("scripts.repair_bpm.read_tags", return_value={"bpm": "111", "genre": "Rock"}), patch(
                "scripts.repair_bpm.write_tags"
            ) as write:
                with self.assertRaisesRegex(ValueError, "tags changed since"):
                    apply_report(report, dry_run=False, scope_root=root, verify_evidence=True)
                write.assert_not_called()

    def test_verified_report_rejects_legacy_and_invalid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "track.mp3"
            path.write_bytes(b"fixture")
            report = root / "audit.json"
            for payload in ({}, {"changes": [{"path": str(path), "action": "update_mismatch"}]}):
                report.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "fresh audit"):
                    apply_report(report, scope_root=root, verify_evidence=True)

    def test_scoped_folder_scan_rejects_audio_symlinks_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root / "music"
            music.mkdir()
            outside = root / "outside.mp3"
            outside.touch()
            (music / "link.mp3").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "outside the selected library root"):
                _iter_target_files(music, scope_root=music)


if __name__ == "__main__":
    unittest.main()

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

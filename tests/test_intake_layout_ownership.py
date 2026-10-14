import importlib.util
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import config


REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("intake_module_test", REPO_ROOT / "intake.py")
intake_module = importlib.util.module_from_spec(_spec)
sys.modules["intake_module_test"] = intake_module
_spec.loader.exec_module(intake_module)


class IntakeLayoutOwnershipTests(unittest.TestCase):
    def test_beets_tags_then_cratedigger_owns_all_layout_steps(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            incoming.mkdir()
            (incoming / "track.mp3").touch()
            completed = Mock(returncode=0)

            with patch.object(config, "INCOMING_FOLDER", incoming), \
                 patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}), \
                 patch.object(intake_module, "setup_logger"), \
                 patch.object(intake_module.subprocess, "run", return_value=completed) as run, \
                 patch.object(
                     sys,
                     "argv",
                     ["intake.py", "--dry-run", "--log", str(root / "review.log")],
                 ):
                intake_module.setup_logger.return_value = Mock()
                intake_module.main()

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 4)
        self.assertIn("--library", commands[0])
        self.assertIn("--nomove", commands[0])
        self.assertIn("--nocopy", commands[0])
        self.assertIn("--pretend", commands[0])
        self.assertEqual(commands[1][1], "02_rename.py")
        self.assertEqual(commands[2][1], "03_folders.py")
        self.assertEqual(commands[3][1], "04_move.py")
        self.assertIn("--to-artist-root", commands[3])


if __name__ == "__main__":
    unittest.main()

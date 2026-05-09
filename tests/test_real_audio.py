import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import config
from lib.context import get_folder_context
from lib.tags import read_tags


REPO_ROOT = Path(__file__).resolve().parents[1]


def _first_real_audio_file() -> Path | None:
    if not config.MUSIC_ROOT.exists():
        return None

    for path in config.MUSIC_ROOT.rglob("*"):
        if path.is_file() and path.suffix.lower() in config.AUDIO_EXTENSIONS:
            return path
    return None


def _folder_has_audio(folder: Path) -> bool:
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in config.AUDIO_EXTENSIONS:
            return True
    return False


def _choose_real_audio_folder() -> str | None:
    requested = os.environ.get("CRATEDIGGER_TEST_FOLDER")
    if requested:
        folder = config.MUSIC_ROOT / requested
        if folder.is_dir() and _folder_has_audio(folder):
            return requested
        return None

    preferred = ["incoming", "pop", "rock", "jazz"]
    for name in preferred:
        folder = config.MUSIC_ROOT / name
        if folder.is_dir() and _folder_has_audio(folder):
            return name

    skip = config.SPECIAL_FOLDERS | config.SKIP_FOLDERS
    for folder in sorted(config.MUSIC_ROOT.iterdir()):
        if not folder.is_dir() or folder.name.lower() in skip:
            continue
        if _folder_has_audio(folder):
            return folder.name
    return None


@unittest.skipUnless(
    os.environ.get("CRATEDIGGER_RUN_REAL_AUDIO_TESTS") == "1",
    "Set CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1 to run read-only tests against config.MUSIC_ROOT.",
)
class RealAudioSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample_file = _first_real_audio_file()
        cls.sample_folder = _choose_real_audio_folder()
        if cls.sample_file is None:
            raise unittest.SkipTest("No readable audio files found under config.MUSIC_ROOT")
        if cls.sample_folder is None:
            raise unittest.SkipTest("No suitable top-level folder with audio files found under config.MUSIC_ROOT")

    def _run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        with TemporaryDirectory() as tempdir:
            log_path = Path(tempdir) / "test.log"
            command = [sys.executable, *args, "--log", str(log_path)]
            return subprocess.run(
                command,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_read_tags_returns_standard_keys_for_real_file(self) -> None:
        tags = read_tags(self.sample_file)

        self.assertIsNotNone(tags)
        self.assertEqual(
            set(tags.keys()),
            {"title", "artist", "albumartist", "album", "year", "genre", "bpm", "track", "disc", "isrc"},
        )

    def test_get_folder_context_returns_context_for_real_file(self) -> None:
        context = get_folder_context(self.sample_file)

        self.assertGreaterEqual(context.depth, 1)
        self.assertNotEqual(context.folder_kind, "unknown")

    def test_01_tag_dry_run_completes_against_real_folder(self) -> None:
        result = self._run_script(
            "01_tag.py",
            "--dry-run",
            "--no-mb",
            "--no-bpm",
            "--folder",
            self.sample_folder,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(f"=== Folder: {self.sample_folder} ===", result.stderr)
        self.assertIn("Done (DRY-RUN)", result.stderr)

    def test_02_rename_dry_run_completes_against_real_folder(self) -> None:
        result = self._run_script(
            "02_rename.py",
            "--dry-run",
            "--folder",
            self.sample_folder,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(f"=== Folder: {self.sample_folder} ===", result.stderr)
        self.assertIn("Done (DRY-RUN)", result.stderr)

    def test_03_folders_dry_run_completes_against_real_folder(self) -> None:
        result = self._run_script(
            "03_folders.py",
            "--dry-run",
            "--folder",
            self.sample_folder,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(f"=== Folder: {self.sample_folder} ===", result.stderr)
        self.assertIn("Done (DRY-RUN)", result.stderr)

    def test_04_move_restructure_dry_run_completes_against_real_folder(self) -> None:
        result = self._run_script(
            "04_move.py",
            "--restructure",
            "--dry-run",
            "--folder",
            self.sample_folder,
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn(f"Restructure folder: {self.sample_folder}", result.stderr)
        self.assertIn("Done (DRY-RUN)", result.stderr)


if __name__ == "__main__":
    unittest.main()

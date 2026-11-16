import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIO_EXTENSIONS = (
    "mp2", "mp3", "flac", "ogg", "opus", "m4a",
    "m4b", "mp4", "aac", "alac", "wav", "wma",
)


@unittest.skipUnless(shutil.which("git"), "git is required to verify ignore rules")
class RepositoryHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tempdir.cleanup)
        root = Path(cls.tempdir.name)
        cls.repo = root / "repo"
        cls.repo.mkdir()
        template = root / "empty-template"
        template.mkdir()
        cls.git_env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        cls.git_env.update({
            "HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TEMPLATE_DIR": str(template),
        })
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=cls.repo,
            env=cls.git_env,
            check=True,
        )
        shutil.copyfile(REPO_ROOT / ".gitignore", cls.repo / ".gitignore")

    def ignored_paths(self, paths: list[str]) -> set[str]:
        if not paths:
            return set()
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin", "-z"],
            cwd=self.repo,
            env=self.git_env,
            input=b"\0".join(os.fsencode(path) for path in paths) + b"\0",
            capture_output=True,
        )
        self.assertIn(
            result.returncode, (0, 1),
            result.stderr.decode("utf-8", errors="replace"),
        )
        return {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}

    def test_feature_store_backups_and_checkpoints_are_ignored(self) -> None:
        paths = [
            "features.pkl",
            "features.pkl.bak",
            ".features.pkl.tmp",
            "results/features.PKL.2026-11-15",
            "results/features.pickle",
            "results/.features.pickle.tmp",
        ]
        self.assertEqual(self.ignored_paths(paths), set(paths))

    def test_generated_and_downloaded_assets_are_ignored(self) -> None:
        paths = [
            "experiments/arrays.npy",
            "experiments/results.NPZ",
            "downloads/model.ONNX",
            "downloads/.model.onnx.123.part",
            "models/alternative.weights",
            "models/heads/classifier.metadata",
            "notebooks/.ipynb_checkpoints/analysis-checkpoint.ipynb",
            ".env.local",
            "config.py.bak",
            "review.json.bak",
            "review.json.2026-11-15",
            "review.log.1",
            "features_export.json.gz.bak",
            "cratedigger.db.bak",
            "cratedigger.db-journal",
            "nested/cratedigger.db",
            "nested/cratedigger.db-wal",
        ]
        self.assertEqual(self.ignored_paths(paths), set(paths))

    def test_audio_is_ignored_except_reviewed_test_fixtures(self) -> None:
        audio = [f"incoming/track.{extension}" for extension in AUDIO_EXTENSIONS]
        audio.extend(["incoming/track.FLAC", "incoming/track.Mp3"])
        self.assertEqual(self.ignored_paths(audio), set(audio))

        fixtures = [
            f"tests/fixtures/short.{extension}" for extension in AUDIO_EXTENSIONS
        ]
        fixtures.extend([
            f"tests/fixtures/nested/short.{extension}" for extension in AUDIO_EXTENSIONS
        ])
        fixtures.append("tests/fixtures/short.WAV")
        self.assertEqual(self.ignored_paths(fixtures), set())
        unrelated = ["other/tests/fixtures/short.wav"]
        self.assertEqual(self.ignored_paths(unrelated), set(unrelated))

    def test_source_and_notebooks_remain_trackable(self) -> None:
        paths = [
            ".env.example",
            "notebooks/01_data_analysis.ipynb",
            "cratedigger/web/templates/base.html",
            "cratedigger/web/static/app.js",
            "docs/architecture.md",
            "docs/models/overview.md",
            "tests/fixtures/example.db",
        ]
        self.assertEqual(self.ignored_paths(paths), set())

    def test_index_contains_no_ignored_files(self) -> None:
        if not (REPO_ROOT / ".git").exists():
            self.skipTest("a Git worktree is required to inspect the index")
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=REPO_ROOT,
            env=self.git_env,
            capture_output=True,
        )
        self.assertEqual(
            result.returncode, 0,
            result.stderr.decode("utf-8", errors="replace"),
        )
        paths = [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]
        self.assertEqual(self.ignored_paths(paths), set())


if __name__ == "__main__":
    unittest.main()

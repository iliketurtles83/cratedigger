"""Tests for the feature-store backfills in 07_recommend.py."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from lib import embeddings
from lib.embeddings import (
    EMBEDDING_FAILED,
    EMBEDDING_OK,
    EMBEDDING_RECIPE,
    LATENT_DIM,
    STYLE_DIM,
    EmbeddingResult,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("recommend_cli", REPO_ROOT / "07_recommend.py")
recommend = importlib.util.module_from_spec(_spec)
sys.modules["recommend_cli"] = recommend
_spec.loader.exec_module(recommend)


def _ok(scale: float = 1.0) -> EmbeddingResult:
    return EmbeddingResult(
        EMBEDDING_OK,
        style_activations=np.full(STYLE_DIM, 0.5 * scale, dtype=np.float32),
        audio_embedding=np.full(LATENT_DIM, 0.1 * scale, dtype=np.float32),
        patches=3,
    )


class TestRecommendBackfills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root / "features.pkl"
        self.paths = {}
        for name in ("legacy", "current", "long", "stale"):
            path = self.root / f"{name}.flac"
            path.write_bytes(b"audio")
            self.paths[name] = str(path)
        self.paths["missing"] = str(self.root / "missing.flac")

    def tearDown(self):
        self.tmp.cleanup()

    def _frame(self) -> pd.DataFrame:
        current = {**embeddings.embedding_fields(_ok()), "path": self.paths["current"], "error": None}
        stale = {**current, "path": self.paths["stale"], "embedding_recipe": "old-recipe"}
        rows = [
            {"path": self.paths["legacy"], "error": None, "embedding": np.ones(STYLE_DIM, dtype=np.float32), "schema_version": 9},
            {**current, "schema_version": 10},
            {"path": self.paths["long"], "error": "duration_exceeds_20min", "schema_version": 9},
            {**stale, "schema_version": 10},
            {"path": self.paths["missing"], "error": None, "schema_version": 9},
        ]
        return pd.DataFrame(rows)

    def test_backfill_embeddings_refreshes_only_stale_rows(self):
        def fake_extract(path):
            return EmbeddingResult(EMBEDDING_FAILED, error="boom") if path == self.paths["stale"] else _ok(2.0)

        with patch.object(recommend, "extract_embeddings", side_effect=fake_extract) as extract:
            df = recommend.backfill_embeddings(self._frame(), self.store)

        self.assertEqual(
            sorted(call.args[0] for call in extract.call_args_list),
            sorted([self.paths["legacy"], self.paths["stale"]]),
        )
        saved = pd.read_pickle(self.store).set_index("path")
        self.assertFalse(any(p.name.endswith(".tmp") for p in self.root.iterdir()))
        legacy = saved.loc[self.paths["legacy"]]
        self.assertEqual(legacy["embedding_status"], EMBEDDING_OK)
        self.assertEqual(legacy["embedding_recipe"], EMBEDDING_RECIPE)
        self.assertEqual(legacy["audio_embedding"].shape, (LATENT_DIM,))
        self.assertEqual(legacy["schema_version"], 9)
        # A failed refresh keeps the previous successful embedding for a later retry.
        self.assertEqual(saved.loc[self.paths["stale"], "embedding_recipe"], "old-recipe")
        self.assertEqual(saved.loc[self.paths["stale"], "embedding_status"], EMBEDDING_OK)
        self.assertEqual(len(df), 5)

    def test_backfill_audio_hashes_keeps_schema_version(self):
        pd.DataFrame(
            [{"path": self.paths["legacy"], "audio_hash": None, "schema_version": 10}]
        ).to_pickle(self.store)
        with patch.object(recommend, "audio_hash", return_value="h1"):
            recommend.backfill_audio_hashes(self.store)
        saved = pd.read_pickle(self.store)
        self.assertEqual(saved.loc[0, "audio_hash"], "h1")
        self.assertEqual(saved.loc[0, "schema_version"], 10)


if __name__ == "__main__":
    unittest.main()

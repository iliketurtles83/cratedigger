"""Shared selection rules for recommender feature-store backfills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from lib.embeddings import embedding_needs_refresh


def missing_hash_positions(frame: pd.DataFrame) -> list[int]:
    hashes = list(frame.get("audio_hash", [None] * len(frame)))
    return [index for index, value in enumerate(hashes) if pd.isna(value) or not value]


def embedding_candidates(frame: pd.DataFrame, *, base_dir: Path | None = None) -> tuple[list[Any], list[Any]]:
    available: list[Any] = []
    missing: list[Any] = []
    for index, row in frame.iterrows():
        if row.get("error") == "duration_exceeds_20min":
            continue
        record = {str(key): value for key, value in row.items()}
        if not embedding_needs_refresh(record):
            continue
        path = Path(str(row.get("path")))
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        (available if path.exists() else missing).append(index)
    return available, missing

"""Content-based recommendation, hybrid vector baking, holdout evaluation, and feature export."""

from __future__ import annotations

import gzip
import json
import logging
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from lib.descriptors import DESCRIPTOR_RECIPE, DESCRIPTOR_SCALE, attach_descriptors, to_probability

log = logging.getLogger("lib.recommender")

# v2: learned EffNet-head scalars replace the librosa heuristics; tracks without
# a usable embedding or decoded for less than half their length are not exported.
RECIPE_VERSION = "cratedigger-v2.0-discogs-effnet-hybrid"
MIN_EXPORT_DECODE_COVERAGE = 0.5

DEFAULT_WEIGHTS = {
    "embedding": 0.70,
    "tempo": 0.15,
    "harmonic": 0.10,
    "scalars": 0.05,
}

PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
PITCH_TO_INDEX = {k: i for i, k in enumerate(PITCH_CLASSES)}

# Descriptor logits (lib.descriptors); the scalar block standardizes their probabilities.
SCALAR_COLS = ["energy", "valence", "danceability", "acousticness", "instrumentalness"]
# Readable export fields: descriptor logits (order-preserving; rank them as percentiles).
EXPORT_DESCRIPTOR_FIELDS = [
    "energy", "valence", "danceability", "acousticness", "instrumentalness",
    "happy", "sad", "relaxed", "aggressive", "party", "electronic",
]
EXPORT_REGRESSION_FIELDS = ["approachability", "engagement"]
EXPORT_LEVEL_FIELDS = ["loudness_lufs", "native_peak_dbfs"]


# ---------------------------------------------------------------------------
# Vector Components
# ---------------------------------------------------------------------------

def encode_tempo_circle(bpm: Optional[float]) -> np.ndarray:
    """Encode tempo on a 2D circle with octave folding (log2 BPM mod 1).

    Double-time / half-time tempos (e.g. 65 and 130 BPM) map to identical angles.
    Returns a 2-dim unit vector [cos, sin].
    """
    if bpm is None or np.isnan(bpm) or bpm <= 0:
        return np.array([0.0, 0.0], dtype=np.float32)
    # log2(bpm) mod 1 gives octave phase in [0, 1)
    phase = float(np.log2(bpm) % 1.0)
    theta = 2.0 * np.pi * phase
    return np.array([np.cos(theta), np.sin(theta)], dtype=np.float32)


def encode_harmonic_circle(key: Optional[str], mode: Optional[str]) -> np.ndarray:
    """Encode musical key and mode on Circle of Fifths with relative major/minor alignment.

    C major and A minor align harmonically. Returns a 3-dim unit vector [cos, sin, mode_sign].
    """
    if not key or key not in PITCH_TO_INDEX:
        return np.array([0.0, 0.0, 0.0], dtype=np.float32)

    pitch_idx = PITCH_TO_INDEX[key]
    # Circle of fifths step: (7 * pitch_class) % 12
    step = (7 * pitch_idx) % 12

    is_minor = str(mode).lower() == "minor"
    if is_minor:
        # Relative minor is 3 semitones down, which corresponds to step - 3 on circle of fifths
        step = (step - 3) % 12

    theta = 2.0 * np.pi * (step / 12.0)
    mode_sign = -1.0 if is_minor else 1.0

    # 3D unit vector: 2D angle (scaled by 1/sqrt(2)) + mode indicator (scaled by 1/sqrt(2))
    scale = 1.0 / np.sqrt(2.0)
    return np.array([scale * np.cos(theta), scale * np.sin(theta), scale * mode_sign], dtype=np.float32)


def _finite(value: Any) -> Optional[float]:
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if np.isfinite(val) else None


def scalar_probability(value: Any) -> Optional[float]:
    """Probability for a descriptor logit, or None when missing."""
    val = _finite(value)
    return None if val is None else float(to_probability(val))


def encode_scalars(row: dict | pd.Series, means: dict, stds: dict) -> np.ndarray:
    """Standardize and L2-normalize descriptor probabilities (missing values -> mean)."""
    vals = []
    for col in SCALAR_COLS:
        mean = means.get(col, 0.5)
        val = scalar_probability(row.get(col))
        if val is None:
            val = mean
        std = stds.get(col, 0.25)
        std = std if std > 1e-4 else 1.0
        standardized = (float(val) - mean) / std
        vals.append(standardized)
    arr = np.array(vals, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 1e-9:
        arr = arr / norm
    return arr


# ---------------------------------------------------------------------------
# Vector Baker
# ---------------------------------------------------------------------------

class VectorBaker:
    """Bakes multi-component features into a single L2-normalized hybrid vector."""

    def __init__(self, weights: Optional[dict[str, float]] = None):
        raw_weights = dict(DEFAULT_WEIGHTS)
        if weights:
            raw_weights.update(weights)

        # Normalize weights so sum == 1.0
        total = sum(raw_weights.values())
        self.weights = {k: v / total for k, v in raw_weights.items()}

        # Probability-scale defaults from the 240-track, 12-genre pilot
        self.scalar_means = {
            "energy": 0.32,
            "valence": 0.51,
            "danceability": 0.50,
            "acousticness": 0.35,
            "instrumentalness": 0.37,
        }
        self.scalar_stds = {
            "energy": 0.26,
            "valence": 0.27,
            "danceability": 0.43,
            "acousticness": 0.37,
            "instrumentalness": 0.36,
        }

    def fit_scalars(self, df: pd.DataFrame) -> None:
        """Fit probability means and stds from a reference DataFrame of descriptor logits."""
        for col in SCALAR_COLS:
            if col in df.columns:
                valid = to_probability(pd.to_numeric(df[col], errors="coerce")).dropna()
                if len(valid) > 0:
                    self.scalar_means[col] = float(valid.mean())
                    std = float(valid.std())
                    self.scalar_stds[col] = std if std > 1e-4 else 0.25

    def bake_vector(self, row: dict | pd.Series) -> np.ndarray:
        """Bake features into a single unit vector."""
        # 1. Embedding block (400 dims)
        emb = row.get("embedding")
        if emb is None or not isinstance(emb, np.ndarray) or emb.size == 0:
            emb = np.zeros(400, dtype=np.float32)
        else:
            emb = emb.astype(np.float32)
            norm = np.linalg.norm(emb)
            if norm > 1e-9:
                emb = emb / norm
            else:
                emb = np.zeros(400, dtype=np.float32)

        # 2. Tempo circle block (2 dims)
        tempo = encode_tempo_circle(row.get("bpm"))

        # 3. Harmonic circle block (3 dims)
        harm = encode_harmonic_circle(row.get("key"), row.get("mode"))

        # 4. Scalars block (5 dims)
        scalars = encode_scalars(row, self.scalar_means, self.scalar_stds)

        # Apply sqrt(weight) scaling
        w_emb = np.sqrt(self.weights.get("embedding", 0.70))
        w_tempo = np.sqrt(self.weights.get("tempo", 0.15))
        w_harm = np.sqrt(self.weights.get("harmonic", 0.10))
        w_scalars = np.sqrt(self.weights.get("scalars", 0.05))

        composite = np.concatenate([
            w_emb * emb,
            w_tempo * tempo,
            w_harm * harm,
            w_scalars * scalars,
        ])

        # Ensure unit norm
        norm = np.linalg.norm(composite)
        if norm > 1e-9:
            composite = composite / norm

        return composite.astype(np.float32)


# ---------------------------------------------------------------------------
# Holdout Evaluation
# ---------------------------------------------------------------------------

class HoldoutEvaluator:
    """Evaluates recommendation quality using a holdout split on 0faves."""

    def __init__(
        self,
        baker: VectorBaker,
        faves_df: pd.DataFrame,
        distractors_df: Optional[pd.DataFrame] = None,
        holdout_ratio: float = 0.20,
        random_seed: int = 42,
    ):
        self.baker = baker
        self.faves_df = faves_df.copy()
        self.distractors_df = distractors_df.copy() if distractors_df is not None else pd.DataFrame()
        self.holdout_ratio = holdout_ratio
        self.random_seed = random_seed

    def evaluate(self, k_neighbors: int = 5, top_k_list: Sequence[int] = (5, 10, 20)) -> dict[str, Any]:
        """Run holdout evaluation and compute Recall@K and MRR.

        Splits 0faves into 80% reference and 20% holdout test.
        Mixes holdout test into distractor pool, then computes ranking
        against reference favorites using k-nearest similarity.
        """
        n_total = len(self.faves_df)
        if n_total < 10:
            return {"error": "insufficient_faves", "count": n_total}

        # Shuffle and split faves
        shuffled = self.faves_df.sample(frac=1.0, random_state=self.random_seed).reset_index(drop=True)
        n_holdout = max(1, int(n_total * self.holdout_ratio))
        test_faves = shuffled.iloc[:n_holdout].reset_index(drop=True)
        ref_faves = shuffled.iloc[n_holdout:].reset_index(drop=True)

        # Bake vectors for reference faves
        ref_vectors = np.array([self.baker.bake_vector(r) for _, r in ref_faves.iterrows()])

        # Build candidate pool: test_faves + distractors
        candidates_list = []
        is_fave_label = []

        for _, r in test_faves.iterrows():
            candidates_list.append(self.baker.bake_vector(r))
            is_fave_label.append(True)

        if not self.distractors_df.empty:
            for _, r in self.distractors_df.iterrows():
                candidates_list.append(self.baker.bake_vector(r))
                is_fave_label.append(False)
        else:
            # If no external distractor pool, create noisy distractors by shuffling features
            n_distract = len(test_faves) * 4
            for _ in range(n_distract):
                sample_row = self.faves_df.sample(n=1).iloc[0].to_dict()
                # perturb
                sample_row["bpm"] = float(np.random.uniform(60, 180))
                sample_row["embedding"] = np.random.randn(400).astype(np.float32)
                candidates_list.append(self.baker.bake_vector(sample_row))
                is_fave_label.append(False)

        cand_vectors = np.array(candidates_list)
        is_fave_arr = np.array(is_fave_label)

        # Compute cosine similarity matrix between candidates and reference faves
        # Since vectors are unit-norm, dot product is cosine similarity
        sim_matrix = np.dot(cand_vectors, ref_vectors.T)  # shape (n_candidates, n_ref_faves)

        # Score per candidate: mean of top k_neighbors similarities
        k_val = min(k_neighbors, sim_matrix.shape[1])
        top_k_sims = np.sort(sim_matrix, axis=1)[:, -k_val:]
        candidate_scores = np.mean(top_k_sims, axis=1)

        # Rank all candidates (higher score = better rank)
        ranked_indices = np.argsort(-candidate_scores)
        ranks = np.empty_like(ranked_indices)
        ranks[ranked_indices] = np.arange(len(ranked_indices)) + 1  # 1-indexed

        fave_ranks = ranks[is_fave_arr]

        # Calculate metrics
        metrics = {
            "n_reference_faves": len(ref_faves),
            "n_test_faves": len(test_faves),
            "n_distractors": len(cand_vectors) - len(test_faves),
            "total_candidates": len(cand_vectors),
            "weights": self.baker.weights,
        }

        for k in top_k_list:
            recall = float(np.mean(fave_ranks <= k))
            metrics[f"recall@{k}"] = round(recall, 4)

        mrr = float(np.mean(1.0 / fave_ranks))
        metrics["mrr"] = round(mrr, 4)
        metrics["mean_rank"] = round(float(np.mean(fave_ranks)), 2)

        return metrics


# ---------------------------------------------------------------------------
# Feature Exporter
# ---------------------------------------------------------------------------

def _row_preference_score(row: pd.Series) -> tuple[int, int, int, float]:
    """Score a feature row to pick the canonical representative per audio_hash."""
    err = row.get("error")
    has_no_error = 1 if (err is None or (isinstance(err, float) and np.isnan(err)) or err == "") else 0
    emb = row.get("embedding")
    has_emb = 1 if isinstance(emb, np.ndarray) and emb.size > 0 else 0

    path_str = str(row.get("path") or "")
    parts = Path(path_str).parts
    tier_score = 0
    for idx, part in enumerate(parts[:-1]):
        if part == "#" or (len(part) == 1 and part.isalpha() and part.isupper()):
            if "0singles" in parts[idx + 1 :]:
                tier_score = max(tier_score, 2)
            else:
                tier_score = max(tier_score, 3)
            break
    if tier_score == 0:
        if any(p in {"0compilations", "0various", "soundtrack"} for p in parts):
            tier_score = 1

    rating_val = row.get("rating")
    rating_score = float(rating_val) if pd.notna(rating_val) else -1.0
    return (has_no_error, has_emb, tier_score, rating_score)


def deduplicate_features_by_audio_hash(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of *df* with at most one canonical row per non-empty audio_hash."""
    if df.empty or "audio_hash" not in df.columns:
        return df.copy()

    has_hash = df["audio_hash"].notna() & (df["audio_hash"].astype(str) != "")
    without_hash = df.loc[~has_hash]
    with_hash = df.loc[has_hash]
    if with_hash.empty or not with_hash["audio_hash"].duplicated().any():
        return df.copy()

    best_indices: list[object] = []
    for _, grp in with_hash.groupby("audio_hash", sort=False):
        if len(grp) == 1:
            best_indices.append(grp.index[0])
            continue
        best_idx = max(grp.index, key=lambda idx: _row_preference_score(grp.loc[idx]))
        best_indices.append(best_idx)

    deduped = df.loc[list(without_hash.index) + best_indices].copy()
    return deduped.reset_index(drop=True)


def _has_style_embedding(value: Any) -> bool:
    return (
        isinstance(value, np.ndarray)
        and value.ndim == 1
        and value.size > 0
        and bool(np.all(np.isfinite(value)))
        and float(np.linalg.norm(value)) > 1e-9
    )


def _rounded(value: Any, digits: int) -> Optional[float]:
    val = _finite(value)
    return None if val is None else round(val, digits)


def exportable_mask(df: pd.DataFrame) -> pd.Series:
    """Rows with a usable style embedding that are not silent and decoded enough audio."""
    if df.empty:
        return pd.Series(False, index=df.index, dtype=bool)
    has_emb = df["embedding"].map(_has_style_embedding).astype(bool) if "embedding" in df.columns else False
    if "decode_coverage" in df.columns:
        coverage = pd.to_numeric(df["decode_coverage"], errors="coerce")
        decoded = coverage.isna() | (coverage >= MIN_EXPORT_DECODE_COVERAGE)
    else:
        decoded = True
    audible = df["signal_status"].ne("silent") if "signal_status" in df.columns else True
    return pd.Series(has_emb, index=df.index) & decoded & audible


def export_features(
    df: pd.DataFrame,
    output_path: Path | str,
    baker: Optional[VectorBaker] = None,
    model_version: str = RECIPE_VERSION,
    descriptor_runners: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Export feature store to a compact gzipped JSON format for the phone/server.

    Descriptor columns are always recomputed from ``audio_embedding`` (replacing
    any legacy proxy columns).  Rows without a usable embedding, or decoded for
    less than ``MIN_EXPORT_DECODE_COVERAGE`` of their length, are left out.
    """
    df = deduplicate_features_by_audio_hash(df)
    if "audio_hash" in df.columns:
        df = df[df["audio_hash"].notna() & (df["audio_hash"].astype(str) != "")]
    else:
        df = df.iloc[0:0]
    keep = exportable_mask(df)
    excluded = int((~keep).sum())
    df = attach_descriptors(df.loc[keep], descriptor_runners)
    if baker is None:
        baker = VectorBaker()
        baker.fit_scalars(df)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tracks = []
    dims = None

    for _, row in df.iterrows():
        vec = baker.bake_vector(row)
        if dims is None:
            dims = len(vec)

        track_dict = {
            "audio_hash": row["audio_hash"],
            "vector": [round(float(x), 5) for x in vec],
            "bpm": _rounded(row.get("bpm"), 1),
            "key": str(row["key"]) if pd.notna(row.get("key")) else None,
            "mode": str(row["mode"]) if pd.notna(row.get("mode")) else None,
        }
        for field in EXPORT_DESCRIPTOR_FIELDS + EXPORT_REGRESSION_FIELDS:
            track_dict[field] = _rounded(row.get(field), 3)
        for field in EXPORT_LEVEL_FIELDS:
            track_dict[field] = _rounded(row.get(field), 2)
        if row.get("signal_status") == "truncated":
            track_dict["truncated"] = True

        # Optional metadata tags if present
        for tag_field in ["artist_tag", "album_tag", "genre_tag"]:
            if pd.notna(row.get(tag_field)):
                clean_name = tag_field.replace("_tag", "")
                track_dict[clean_name] = str(row[tag_field])

        tracks.append(track_dict)

    payload = {
        "model_version": model_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dims": dims or 0,
        "weights": baker.weights,
        "descriptor_recipe": DESCRIPTOR_RECIPE,
        "descriptor_scale": DESCRIPTOR_SCALE,
        "track_count": len(tracks),
        "excluded_count": excluded,
        "tracks": tracks,
    }

    raw_json = json.dumps(payload, ensure_ascii=False)

    target_path = output_path.resolve()
    with tempfile.TemporaryDirectory(prefix=".cratedigger-export-", dir=target_path.parent) as temp_dir:
        temp_path = Path(temp_dir) / target_path.name
        if str(output_path).endswith(".gz"):
            with gzip.open(temp_path, "wt", encoding="utf-8") as f:
                f.write(raw_json)
        else:
            temp_path.write_text(raw_json, encoding="utf-8")
        try:
            existing_mode = stat.S_IMODE(target_path.stat().st_mode)
        except FileNotFoundError:
            pass
        else:
            temp_path.chmod(existing_mode)
        temp_path.replace(target_path)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    log.info(
        "Exported %d tracks (%d dims) to %s (%.2f MB); %d left out (silent, no usable embedding, or decode coverage < %.0f%%)",
        len(tracks), dims or 0, output_path, size_mb, excluded, MIN_EXPORT_DECODE_COVERAGE * 100,
    )

    return {
        "output_path": str(output_path),
        "track_count": len(tracks),
        "excluded_count": excluded,
        "dims": dims,
        "size_mb": round(size_mb, 2),
    }

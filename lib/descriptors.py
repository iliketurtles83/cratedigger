"""Learned descriptors from Essentia Discogs-EffNet classification heads.

The heads are small ONNX networks over the 1280-d Discogs-EffNet embedding.
Applying them to the stored mean-pooled ``audio_embedding`` matches averaging
their per-patch outputs (Pearson 0.98-1.00 on the pilot sample), so the values
are derived on demand instead of being stored in ``features.pkl``.

Classifier descriptors are logits, ``log(p) - log(1 - p)``: probabilities from
these heads saturate near 0 and 1, and logits keep the ordering inside those
tails, which matters for library-percentile rules.  ``to_probability`` converts
back.  ``energy`` and ``valence`` are exact logits of mean probabilities:
energy = mean(aggressive, party, not relaxed), valence = mean(happy, not sad).
``approachability`` and ``engagement`` are regression outputs (about 0..1).

Models: https://essentia.upf.edu/models.html (CC BY-NC-SA 4.0, like the
Discogs-EffNet backbone).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import numpy as np
import pandas as pd

from lib.embeddings import LATENT_DIM, MODELS_DIR

log = logging.getLogger("lib.descriptors")

HEADS_DIR = MODELS_DIR / "heads"
HEADS_BASE_URL = "https://essentia.upf.edu/models/classification-heads"
DESCRIPTOR_RECIPE = "effnet-heads-1:mean-embedding:logit"
DESCRIPTOR_SCALE = "logit"
LOGIT_BOUND = float(np.log(1e38))  # probabilities are clipped at 1e-38 in float64
BATCH_ROWS = 4096


@dataclass(frozen=True)
class HeadSpec:
    field: str
    task: str
    stem: str
    classes: tuple[str, ...]
    positive: Optional[str]  # None for regression heads
    sha256: str

    @property
    def filename(self) -> str:
        return f"{self.stem}.onnx"

    @property
    def url(self) -> str:
        return f"{HEADS_BASE_URL}/{self.task}/{self.filename}"


def _head(field: str, task: str, classes: tuple[str, ...], positive: Optional[str], sha256: str,
          stem: Optional[str] = None) -> HeadSpec:
    return HeadSpec(field, task, f"{stem or task}-discogs-effnet-1", classes, positive, sha256)


# Class order is the ONNX output order from each head's metadata JSON.
HEADS: tuple[HeadSpec, ...] = (
    _head("happy", "mood_happy", ("happy", "non_happy"), "happy",
          "0ca322819ef137b4b87e9866bffe7370a630e6f1165184ec106326cef6f81e06"),
    _head("sad", "mood_sad", ("non_sad", "sad"), "sad",
          "1a50d11c23181bdfdeabc7d6032a2dad829dfc19c3776d2ee57ef1724cb08805"),
    _head("relaxed", "mood_relaxed", ("non_relaxed", "relaxed"), "relaxed",
          "8ba6515a1e5943a72b3b475e3a25fc7a2ff04142c3eaa6aa0716fca371efdfff"),
    _head("aggressive", "mood_aggressive", ("aggressive", "not_aggressive"), "aggressive",
          "de36550b5d1660791ad732ed6de6ebfdc3e65dcf50b928b2578ddf103dbfb400"),
    _head("party", "mood_party", ("non_party", "party"), "party",
          "c50ac2106ec2f209dd04ad48756582df0e3f3512235310d1a4a3fcc453745f04"),
    _head("acousticness", "mood_acoustic", ("acoustic", "non_acoustic"), "acoustic",
          "56b02abf772b9c1cf528e4d6521d6e09515cedccce47895ee5de8de33fbdf848"),
    _head("electronic", "mood_electronic", ("electronic", "non_electronic"), "electronic",
          "4cc09140d5b078cf39e55975a23cba813e6bd4014092b78e8c415fa32a382149"),
    _head("danceability", "danceability", ("danceable", "not_danceable"), "danceable",
          "9ce9b8c44f1dd5df5ffc124e5d41d67acf254232c1b90c7e057e079ab7cead73"),
    _head("instrumentalness", "voice_instrumental", ("instrumental", "voice"), "instrumental",
          "20155e4c439714b0c45c08644b73c8e12d9dccb173bd4ab9934bf1e5aee837ca"),
    _head("tonal", "tonal_atonal", ("atonal", "tonal"), "tonal",
          "39b9f73c23bec24c911cdb1370ae7b1aa387e69c97e256e83360b5560fd5f356"),
    _head("approachability", "approachability", ("approachability",), None,
          "f783b17ee994394f30f27d05ccd3fb845e589d107802e55e0b9cf1ea041ec894",
          stem="approachability_regression"),
    _head("engagement", "engagement", ("engagement",), None,
          "9551a6c162c9803aef5ce4e4d29a2e4fae99b5bfdc95dae9dd8fe5335e0fb567",
          stem="engagement_regression"),
)

CLASSIFIER_FIELDS: tuple[str, ...] = tuple(h.field for h in HEADS if h.positive)
REGRESSION_FIELDS: tuple[str, ...] = tuple(h.field for h in HEADS if not h.positive)
# Composite -> ((component, sign), ...); sign -1 uses the complement probability.
COMPOSITES: dict[str, tuple[tuple[str, int], ...]] = {
    "energy": (("aggressive", 1), ("party", 1), ("relaxed", -1)),
    "valence": (("happy", 1), ("sad", -1)),
}
LOGIT_FIELDS: tuple[str, ...] = CLASSIFIER_FIELDS + tuple(COMPOSITES)
DESCRIPTOR_COLUMNS: tuple[str, ...] = LOGIT_FIELDS + REGRESSION_FIELDS

HeadRunner = Callable[[np.ndarray], np.ndarray]
_runners: Optional[dict[str, HeadRunner]] = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(spec: HeadSpec, dest: Path) -> None:
    log.info("Downloading %s to %s...", spec.url, dest)
    req = urllib.request.Request(spec.url, headers={"User-Agent": "cratedigger/1.0"})
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=60) as resp:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                out.write(chunk)
        actual = _sha256(tmp)
        if actual != spec.sha256:
            raise RuntimeError(f"{spec.filename} checksum {actual} != pinned {spec.sha256}")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_heads(heads: tuple[HeadSpec, ...] = HEADS, directory: Path = HEADS_DIR) -> dict[str, Path]:
    """Download missing or altered heads into *directory*, verifying pinned checksums."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for spec in heads:
        path = directory / spec.filename
        if not path.exists() or _sha256(path) != spec.sha256:
            _download(spec, path)
        paths[spec.field] = path
    return paths


def load_head_sessions() -> dict[str, HeadRunner]:
    """Cached ONNX runners mapping an (n, 1280) float32 matrix to raw head outputs."""
    global _runners
    if _runners is None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        runners: dict[str, HeadRunner] = {}
        for field, path in ensure_heads().items():
            session = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
            in_name = session.get_inputs()[0].name
            out_name = session.get_outputs()[0].name
            runners[field] = (
                lambda x, s=session, i=in_name, o=out_name: np.asarray(s.run([o], {i: x})[0])
            )
        _runners = runners
    return _runners


def to_probability(logit: Any) -> Any:
    """Logistic sigmoid; NaN stays NaN.  Works on scalars, arrays and Series."""
    return 0.5 * (1.0 + np.tanh(0.5 * logit))


def _log_sigmoid(x: np.ndarray) -> np.ndarray:
    return -np.logaddexp(0.0, -x)


def mean_probability_logit(logits: list[np.ndarray]) -> np.ndarray:
    """Exact logit of the mean of ``sigmoid(logits)``, computed in the log domain."""
    stacked = np.stack(logits)
    return np.logaddexp.reduce(_log_sigmoid(stacked), axis=0) - np.logaddexp.reduce(
        _log_sigmoid(-stacked), axis=0
    )


def classifier_logit(outputs: np.ndarray, spec: HeadSpec) -> np.ndarray:
    """``log(p_pos) - log(p_neg)`` from a two-class softmax output."""
    pos = spec.classes.index(spec.positive)
    probs = np.clip(np.asarray(outputs, dtype=np.float64), 1e-38, None)
    return np.log(probs[:, pos]) - np.log(probs[:, 1 - pos])


def compute_descriptors(
    matrix: np.ndarray, runners: Optional[Mapping[str, HeadRunner]] = None
) -> pd.DataFrame:
    """Descriptor columns for an (n, 1280) matrix of mean-pooled audio embeddings."""
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != LATENT_DIM:
        raise ValueError(f"expected (n, {LATENT_DIM}) embeddings, got {matrix.shape}")
    runners = runners if runners is not None else load_head_sessions()
    values: dict[str, np.ndarray] = {}
    for spec in HEADS:
        raw = np.concatenate(
            [runners[spec.field](matrix[i : i + BATCH_ROWS]) for i in range(0, len(matrix), BATCH_ROWS)]
        ) if len(matrix) else np.empty((0, len(spec.classes)))
        if spec.positive:
            values[spec.field] = classifier_logit(raw, spec)
        else:
            values[spec.field] = np.asarray(raw, dtype=np.float64)[:, 0]
    for name, parts in COMPOSITES.items():
        values[name] = mean_probability_logit([sign * values[field] for field, sign in parts])
    return pd.DataFrame({col: values[col] for col in DESCRIPTOR_COLUMNS})


def _valid_latent(value: Any) -> bool:
    return (
        isinstance(value, np.ndarray)
        and value.shape == (LATENT_DIM,)
        and bool(np.all(np.isfinite(value)))
    )


def descriptor_frame(
    df: pd.DataFrame, runners: Optional[Mapping[str, HeadRunner]] = None
) -> pd.DataFrame:
    """Descriptors aligned to *df*'s index; NaN where ``audio_embedding`` is missing or invalid."""
    out = pd.DataFrame(np.nan, index=df.index, columns=list(DESCRIPTOR_COLUMNS))
    if df.empty or "audio_embedding" not in df.columns:
        return out
    valid = df["audio_embedding"].map(_valid_latent).astype(bool)
    if not valid.any():
        return out
    matrix = np.stack(df.loc[valid, "audio_embedding"].to_list())
    out.loc[valid, :] = compute_descriptors(matrix, runners).to_numpy()
    return out


def attach_descriptors(
    df: pd.DataFrame, runners: Optional[Mapping[str, HeadRunner]] = None
) -> pd.DataFrame:
    """Copy of *df* with descriptor columns, replacing legacy proxy columns of the same name."""
    desc = descriptor_frame(df, runners)
    result = df.copy()
    for col in desc.columns:
        result[col] = desc[col]
    return result

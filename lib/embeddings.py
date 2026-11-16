"""Pretrained music style embeddings using Discogs-EffNet ONNX.

The model returns two outputs per 128-frame patch: 400 Discogs style
activations (sigmoid) and a 1280-dimensional latent embedding.  Both are
mean-pooled and stored raw; L2 normalization belongs to derived similarity
vectors.  ``EMBEDDING_RECIPE`` identifies decoding, resampling, framing,
patch selection, and pooling, so a change to any of them must bump it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np

log = logging.getLogger("lib.embeddings")

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODELS_DIR / "discogs-effnet-bsdynamic-1.onnx"
MODEL_URL = "https://huggingface.co/Heyian/discogs-effnet-onnx/resolve/main/discogs-effnet-bsdynamic-1.onnx"
CLASSES_PATH = MODELS_DIR / "discogs400_classes.json"
CLASSES_URL = "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json"

STYLE_DIM = 400
LATENT_DIM = 1280
DECODE_SAMPLE_RATE = 22050
MODEL_SAMPLE_RATE = 16000
RESAMPLE_TYPE = "soxr_hq"
FRAME_SIZE = 512
HOP_SIZE = 256
MEL_BANDS = 96
PATCH_FRAMES = 128
MAX_PATCHES = 150

# Official Essentia inference uses overlapping patches (hop 62) and centered
# first frames; this recipe keeps the historical non-overlapping uniform sample.
# "ffmpegretry": short decodes from corrupt MP3 frames are retried (lib.decode).
EMBEDDING_RECIPE = (
    "discogs-effnet-bsdynamic-1"
    ":decode22050-ffmpegretry-sanitize-soxr_hq16000"
    ":musicnn-f512-h256-startzero"
    ":patch128-hop128-uniform150"
    ":mean"
)

EMBEDDING_OK = "ok"
EMBEDDING_FAILED = "failed"
EMBEDDING_UNAVAILABLE = "unavailable"
EMBEDDING_SKIPPED_TOO_SHORT = "skipped_too_short"
EMBEDDING_SKIPPED_SILENT = "skipped_silent"
EMBEDDING_SKIPPED_DURATION = "skipped_duration"
RETRYABLE_EMBEDDING_STATUSES = frozenset({EMBEDDING_FAILED, EMBEDDING_UNAVAILABLE})
# Decided from the analyzed signal, not the model recipe; changes need a full re-analysis.
ANALYZER_SKIP_STATUSES = frozenset({EMBEDDING_SKIPPED_SILENT, EMBEDDING_SKIPPED_DURATION})

EMBEDDING_COLUMNS: tuple[str, ...] = (
    "embedding",
    "style_activations",
    "audio_embedding",
    "embedding_patches",
    "embedding_status",
    "embedding_error",
    "embedding_recipe",
    "embedding_model_sha256",
    "style_labels_sha256",
)

_session = None
_classes = None
_intra_op_threads: Optional[int] = None
_model_sha256: Optional[str] = None
_labels_sha256: Optional[str] = None


@dataclass(frozen=True)
class EmbeddingResult:
    """Outcome of one model pass; arrays are raw float32 means when ``status == "ok"``."""

    status: str
    style_activations: Optional[np.ndarray] = None
    audio_embedding: Optional[np.ndarray] = None
    patches: int = 0
    error: Optional[str] = None
    recipe: str = EMBEDDING_RECIPE

    @property
    def ok(self) -> bool:
        return self.status == EMBEDDING_OK

    def normalized_style(self) -> Optional[np.ndarray]:
        return normalize_vector(self.style_activations)


def ensure_model_downloaded() -> Path:
    """Ensure Discogs-EffNet ONNX model and metadata exist locally."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if not MODEL_PATH.exists():
        log.info("Downloading Discogs-EffNet model to %s...", MODEL_PATH)
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
        log.info("Discogs-EffNet model download complete.")

    if not CLASSES_PATH.exists():
        try:
            req = urllib.request.Request(CLASSES_URL, headers={"User-Agent": "cratedigger/1.0"})
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                classes = data.get("classes", [])
                CLASSES_PATH.write_text(json.dumps(classes, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Could not download class labels: %s", exc)

    return MODEL_PATH


def set_inference_threads(threads: Optional[int]) -> None:
    """Set ONNX Runtime intra-op threads for sessions created after this call."""
    global _intra_op_threads, _session
    value = int(threads) if threads else None
    if value != _intra_op_threads:
        _intra_op_threads = value
        _session = None


def release_inference_session() -> None:
    """Drop the cached session and its thread pool; required before forking workers."""
    global _session
    _session = None


def get_inference_session():
    """Lazy-load ONNX Runtime session."""
    global _session
    if _session is None:
        import onnxruntime as ort

        model_file = ensure_model_downloaded()
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        available_providers = ort.get_available_providers()
        selected_providers = [p for p in providers if p in available_providers]
        options = ort.SessionOptions()
        if _intra_op_threads:
            options.intra_op_num_threads = _intra_op_threads
        _session = ort.InferenceSession(
            str(model_file), sess_options=options, providers=selected_providers
        )
    return _session


def get_style_classes() -> list[str]:
    """Return list of 400 style/genre class names."""
    global _classes
    if _classes is None:
        if CLASSES_PATH.exists():
            try:
                _classes = json.loads(CLASSES_PATH.read_text(encoding="utf-8"))
            except Exception:
                _classes = []
        else:
            _classes = []
    return _classes


def model_provenance() -> dict[str, Optional[str]]:
    """Return the model file checksum and the class-label mapping checksum."""
    global _model_sha256, _labels_sha256
    if _model_sha256 is None and MODEL_PATH.exists():
        digest = hashlib.sha256()
        with MODEL_PATH.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        _model_sha256 = digest.hexdigest()
    if _labels_sha256 is None:
        classes = get_style_classes()
        if len(classes) == STYLE_DIM:
            canonical = json.dumps(classes, ensure_ascii=False, separators=(",", ":"))
            _labels_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"embedding_model_sha256": _model_sha256, "style_labels_sha256": _labels_sha256}


def normalize_vector(vec: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """Return an L2-normalized float32 copy of *vec* (unchanged if its norm is ~0)."""
    if vec is None:
        return None
    out = np.asarray(vec, dtype=np.float32).copy()
    norm = float(np.linalg.norm(out))
    if norm > 1e-9:
        out /= norm
    return out


def sanitize_audio(y: np.ndarray) -> tuple[np.ndarray, int]:
    """Zero non-finite samples and decoder spikes beyond +/-2.0; clip the rest to +/-1.

    Returns the cleaned float32 signal and the number of zeroed samples.
    """
    arr = np.asarray(y, dtype=np.float32)
    with np.errstate(invalid="ignore"):
        corrupt = ~np.isfinite(arr) | (np.abs(arr) > 2.0)
    zeroed = int(np.count_nonzero(corrupt))
    clean = np.clip(np.where(corrupt, np.float32(0.0), arr), -1.0, 1.0)
    return clean.astype(np.float32, copy=False), zeroed


def decode_audio(path: Path | str) -> np.ndarray:
    """Decode *path* to mono float32 at ``DECODE_SAMPLE_RATE`` (not sanitized).

    Short decodes from corrupt MP3 frames are retried with ffmpeg (``lib.decode``).
    """
    try:
        import librosa

        from lib.decode import analysis_buffer, decode_native

        loaded = analysis_buffer(decode_native(path, librosa), librosa, DECODE_SAMPLE_RATE)
    except Exception:
        import essentia.standard as es

        loaded = es.MonoLoader(filename=str(path), sampleRate=DECODE_SAMPLE_RATE)()
    return np.asarray(loaded, dtype=np.float32)


def prepare_model_input(audio: np.ndarray, sr: int) -> np.ndarray:
    """Downmix, sanitize, and anti-alias resample *audio* to the model's 16 kHz input."""
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim > 1:
        arr = np.mean(arr, axis=0, dtype=np.float32)
    arr, _ = sanitize_audio(arr)
    if sr != MODEL_SAMPLE_RATE and arr.size:
        import librosa

        arr = librosa.resample(arr, orig_sr=sr, target_sr=MODEL_SAMPLE_RATE, res_type=RESAMPLE_TYPE)
    return np.ascontiguousarray(arr, dtype=np.float32)


def _resolve_output_names(session) -> tuple[str, str]:
    """Return ``(style_output, latent_output)`` names, matched by output dimension."""
    style = latent = None
    for output in session.get_outputs():
        shape = list(getattr(output, "shape", None) or [])
        dim = shape[-1] if shape else None
        if dim == STYLE_DIM:
            style = output.name
        elif dim == LATENT_DIM:
            latent = output.name
    if style is None or latent is None:
        raise RuntimeError(
            f"model outputs do not expose {STYLE_DIM} activations and {LATENT_DIM} embeddings"
        )
    return style, latent


def _mel_patches(signal: np.ndarray, max_patches: int) -> Optional[np.ndarray]:
    import essentia.standard as es

    tf_input = es.TensorflowInputMusiCNN()
    mels = [
        tf_input(frame)
        for frame in es.FrameGenerator(signal, frameSize=FRAME_SIZE, hopSize=HOP_SIZE, startFromZero=True)
    ]
    if not mels:
        return None
    mel_matrix = np.asarray(mels, dtype=np.float32)
    n_patches = mel_matrix.shape[0] // PATCH_FRAMES
    if n_patches == 0:
        return None
    if n_patches > max_patches:
        step = n_patches / max_patches
        indices = [int(i * step) for i in range(max_patches)]
    else:
        indices = list(range(n_patches))
    return np.stack(
        [mel_matrix[i * PATCH_FRAMES : (i + 1) * PATCH_FRAMES, :] for i in indices]
    ).astype(np.float32, copy=False)


def _pooled_output(values: Any, n_patches: int, dim: int, label: str) -> np.ndarray:
    arr = np.asarray(values)
    if arr.shape != (n_patches, dim):
        raise RuntimeError(f"{label} output shape {arr.shape}, expected {(n_patches, dim)}")
    if not np.all(np.isfinite(arr)):
        raise RuntimeError(f"{label} output contains non-finite values")
    return np.mean(arr, axis=0, dtype=np.float64).astype(np.float32)


def _extract(
    path: Optional[Path | str],
    audio: Optional[np.ndarray],
    sr: Optional[int],
    max_patches: int,
) -> EmbeddingResult:
    recipe = EMBEDDING_RECIPE
    if max_patches != MAX_PATCHES:
        recipe = f"{recipe}:max{max_patches}"
    try:
        import essentia.standard  # noqa: F401
    except ImportError:
        return EmbeddingResult(EMBEDDING_UNAVAILABLE, error="essentia_not_installed", recipe=recipe)

    try:
        session = get_inference_session()
        style_name, latent_name = _resolve_output_names(session)
    except Exception as exc:
        return EmbeddingResult(EMBEDDING_UNAVAILABLE, error=f"model_unavailable: {exc}", recipe=recipe)

    try:
        if audio is None:
            if path is None:
                raise ValueError("either path or audio is required")
            audio, sr = decode_audio(path), DECODE_SAMPLE_RATE
        input_sr = int(sr or MODEL_SAMPLE_RATE)
        if input_sr != DECODE_SAMPLE_RATE:
            recipe = f"{recipe}:input{input_sr}"
        signal = prepare_model_input(audio, input_sr)
        if signal.size == 0:
            return EmbeddingResult(EMBEDDING_SKIPPED_TOO_SHORT, error="empty_audio", recipe=recipe)

        patches = _mel_patches(signal, max_patches)
        if patches is None:
            return EmbeddingResult(
                EMBEDDING_SKIPPED_TOO_SHORT, error="fewer_than_one_patch", recipe=recipe
            )

        input_name = session.get_inputs()[0].name
        style_out, latent_out = session.run([style_name, latent_name], {input_name: patches})
        n_patches = int(patches.shape[0])
        return EmbeddingResult(
            EMBEDDING_OK,
            style_activations=_pooled_output(style_out, n_patches, STYLE_DIM, "activations"),
            audio_embedding=_pooled_output(latent_out, n_patches, LATENT_DIM, "embeddings"),
            patches=n_patches,
            recipe=recipe,
        )
    except Exception as exc:
        name = Path(path).name if path is not None else "<buffer>"
        log.warning("Embedding extraction failed for %s: %s", name, exc)
        return EmbeddingResult(
            EMBEDDING_FAILED, error=f"{type(exc).__name__}: {exc}"[:500], recipe=recipe
        )


def extract_embeddings(
    path: Optional[Path | str] = None,
    *,
    audio: Optional[np.ndarray] = None,
    sr: Optional[int] = None,
) -> EmbeddingResult:
    """Run one Discogs-EffNet inference and return both raw mean-pooled outputs.

    Pass *audio* decoded at ``DECODE_SAMPLE_RATE`` to share decoding with other
    analysis; otherwise *path* is decoded the same way.
    """
    return _extract(path, audio, sr, MAX_PATCHES)


def extract_embedding(
    path: Path | str,
    max_patches: int = MAX_PATCHES,
    audio: Optional[np.ndarray] = None,
    sr: Optional[int] = None,
) -> Optional[np.ndarray]:
    """Return the L2-normalized 400-dim style vector, or None (compatibility API)."""
    result = _extract(path, audio, sr, max_patches)
    return result.normalized_style() if result.ok else None


def embedding_fields(result: EmbeddingResult) -> dict[str, Any]:
    """Map *result* onto feature-store columns, including provenance."""
    fields: dict[str, Any] = {
        "embedding": result.normalized_style() if result.ok else None,
        "style_activations": result.style_activations if result.ok else None,
        "audio_embedding": result.audio_embedding if result.ok else None,
        "embedding_patches": result.patches if result.ok else None,
        "embedding_status": result.status,
        "embedding_error": result.error,
        "embedding_recipe": result.recipe,
    }
    fields.update(model_provenance())
    return fields


def _clean_text(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    return str(value)


def _valid_vector(value: Any, dim: int) -> bool:
    return (
        isinstance(value, np.ndarray)
        and value.shape == (dim,)
        and bool(np.all(np.isfinite(value)))
    )


def embedding_needs_refresh(row: Mapping[str, Any]) -> bool:
    """Return True when a stored embedding is missing, retryable, or from another recipe/model."""
    status = _clean_text(row.get("embedding_status"))
    if status is None or status in RETRYABLE_EMBEDDING_STATUSES:
        return True
    if status in ANALYZER_SKIP_STATUSES:
        return False
    if _clean_text(row.get("embedding_recipe")) != EMBEDDING_RECIPE:
        return True
    if status != EMBEDDING_OK:
        return False
    current_model = model_provenance()["embedding_model_sha256"]
    if current_model is not None and _clean_text(row.get("embedding_model_sha256")) != current_model:
        return True
    return not (
        _valid_vector(row.get("embedding"), STYLE_DIM)
        and _valid_vector(row.get("style_activations"), STYLE_DIM)
        and _valid_vector(row.get("audio_embedding"), LATENT_DIM)
    )


def reusable_embedding_fields(
    row: Optional[Mapping[str, Any]], audio_hash: Optional[str]
) -> Optional[dict[str, Any]]:
    """Return a current, successful embedding from *row* if it describes *audio_hash*."""
    if row is None or not audio_hash or row.get("audio_hash") != audio_hash:
        return None
    if _clean_text(row.get("embedding_status")) != EMBEDDING_OK or embedding_needs_refresh(row):
        return None
    return {col: row.get(col) for col in EMBEDDING_COLUMNS}


def preflight() -> list[str]:
    """Return blocking problems that would prevent embedding extraction."""
    problems: list[str] = []
    for module, label in (("librosa", "librosa"), ("essentia.standard", "essentia"), ("onnxruntime", "onnxruntime")):
        try:
            __import__(module)
        except ImportError:
            problems.append(f"{label} is not installed")
    if problems:
        return problems
    try:
        session = get_inference_session()
        _resolve_output_names(session)
        shape = list(session.get_inputs()[0].shape or [])
        if shape[-2:] != [PATCH_FRAMES, MEL_BANDS]:
            problems.append(f"model input shape {shape} does not end with [{PATCH_FRAMES}, {MEL_BANDS}]")
    except Exception as exc:
        problems.append(f"Discogs-EffNet model unavailable: {exc}")
    if model_provenance()["style_labels_sha256"] is None:
        log.warning("Discogs style labels missing or incomplete at %s", CLASSES_PATH)
    return problems

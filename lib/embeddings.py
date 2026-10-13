"""Pretrained music style embeddings using Discogs-EffNet ONNX."""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("lib.embeddings")

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH = MODELS_DIR / "discogs-effnet-bsdynamic-1.onnx"
MODEL_URL = "https://huggingface.co/Heyian/discogs-effnet-onnx/resolve/main/discogs-effnet-bsdynamic-1.onnx"
CLASSES_PATH = MODELS_DIR / "discogs400_classes.json"
CLASSES_URL = "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json"

_session = None
_classes = None


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


def get_inference_session():
    """Lazy-load ONNX Runtime session."""
    global _session
    if _session is None:
        import onnxruntime as ort

        model_file = ensure_model_downloaded()
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        available_providers = ort.get_available_providers()
        selected_providers = [p for p in providers if p in available_providers]
        _session = ort.InferenceSession(str(model_file), providers=selected_providers)
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


def extract_embedding(
    path: Path | str,
    max_patches: int = 150,
    audio: Optional[np.ndarray] = None,
    sr: Optional[int] = None,
) -> Optional[np.ndarray]:
    """Extract mean 400-dim style activation embedding from an audio file.

    Uses Essentia MusiCNN mel-spectrogram frontend (sr=16000, 96 mel bands, 128 frames)
    and Discogs-EffNet ONNX model. Returns an L2-normalized 400-dim vector.
    """
    try:
        import essentia.standard as es
    except ImportError:
        log.warning("essentia is not installed; skipping embedding extraction.")
        return None

    try:
        session = get_inference_session()
    except Exception as exc:
        log.warning("Could not initialize ONNX session: %s", exc)
        return None

    try:
        if audio is not None:
            if sr is not None and sr != 16000:
                import librosa
                audio = librosa.resample(audio, orig_sr=sr, target_sr=16000, res_type="soxr_qq")
            audio = np.ascontiguousarray(audio, dtype=np.float32)
        else:
            try:
                import librosa
                loaded, _ = librosa.load(str(path), sr=16000, mono=True)
                audio = np.ascontiguousarray(loaded, dtype=np.float32)
            except Exception:
                audio = es.MonoLoader(filename=str(path), sampleRate=16000)()

        if audio is None or len(audio) == 0:
            return None

        mels = []
        tf_input = es.TensorflowInputMusiCNN()
        for frame in es.FrameGenerator(audio, frameSize=512, hopSize=256, startFromZero=True):
            mels.append(tf_input(frame))

        if not mels:
            return None

        mel_matrix = np.array(mels)
        # Model expects patches of shape [128, 96]
        n_patches = mel_matrix.shape[0] // 128
        if n_patches == 0:
            return None

        # Sample up to max_patches uniformly across the track to balance compute
        if n_patches > max_patches:
            step = n_patches / max_patches
            indices = [int(i * step) for i in range(max_patches)]
        else:
            indices = list(range(n_patches))

        patches = []
        for i in indices:
            patch = mel_matrix[i * 128 : (i + 1) * 128, :]
            patches.append(patch)

        patches_arr = np.array(patches, dtype=np.float32)

        inp_name = session.get_inputs()[0].name
        preds = session.run(None, {inp_name: patches_arr})

        # Output 0 is activations: shape (n_patches, 400)
        activations = preds[0]
        mean_vector = np.mean(activations, axis=0)

        # L2-normalize
        norm = np.linalg.norm(mean_vector)
        if norm > 1e-9:
            mean_vector = mean_vector / norm

        return mean_vector.astype(np.float32)

    except Exception as exc:
        log.warning("Embedding extraction failed for %s: %s", Path(path).name, exc)
        return None

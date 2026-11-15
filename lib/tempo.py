"""Shared audio tempo (BPM) estimation and validation helpers."""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("lib.tempo")

MIN_VALID_BPM = 35.0
MAX_VALID_BPM = 220.0
DEFAULT_MIN_CONFIDENCE = 0.5
BEATLESS_MIN_CONFIDENCE = 0.8
ESSENTIA_SAMPLE_RATE = 44100
DEFAULT_MAX_ANALYSIS_SECONDS = 120.0
MIN_AUDIO_SECONDS = 1.5

BEATLESS_GENRE_KEYWORDS = (
    "meditation",
    "spoken",
    "audiobook",
    "podcast",
    "speech",
)

DNB_GENRE_KEYWORDS = (
    "drum & bass",
    "drum and bass",
    "dnb",
    "jungle",
)


@dataclass(frozen=True)
class TempoResult:
    """Result of tempo estimation for a single audio track."""

    bpm: Optional[float]
    confidence: Optional[float]
    method: str


def is_valid_bpm(bpm: object) -> bool:
    """Return True if *bpm* is finite and within [MIN_VALID_BPM, MAX_VALID_BPM]."""
    if bpm is None:
        return False
    try:
        val = float(bpm)
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(val) and MIN_VALID_BPM <= val <= MAX_VALID_BPM)


def sanitize_bpm_tag(raw_bpm: object) -> Optional[float]:
    """Parse a metadata BPM tag and return rounded float if sane, else None."""
    if raw_bpm is None:
        return None
    try:
        val = float(str(raw_bpm).strip())
    except (TypeError, ValueError):
        return None
    if not is_valid_bpm(val):
        return None
    return round(val, 2)


def required_confidence_for_genre(
    genre: Optional[str],
    default_min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> float:
    """Raise confidence requirement for genres likely to lack a musical pulse."""
    if not genre:
        return default_min_confidence
    lowered = genre.lower()
    if any(kw in lowered for kw in BEATLESS_GENRE_KEYWORDS):
        return max(default_min_confidence, BEATLESS_MIN_CONFIDENCE)
    return default_min_confidence


def apply_genre_octave_fold(bpm: float, genre: Optional[str]) -> float:
    """Apply conservative genre-band octave correction for human-facing tags.

    Currently doubles half-time Drum & Bass / Jungle estimates (<105 BPM) when
    doubling stays within MAX_VALID_BPM.
    """
    if not genre or not is_valid_bpm(bpm):
        return bpm
    lowered = genre.lower()
    if any(kw in lowered for kw in DNB_GENRE_KEYWORDS):
        if bpm < 105.0 and (bpm * 2.0) <= MAX_VALID_BPM:
            return round(bpm * 2.0, 2)
    return bpm


def _slice_central_window(
    audio: np.ndarray,
    sr: int,
    max_seconds: Optional[float],
) -> np.ndarray:
    """Return a centered slice of up to *max_seconds* from 1D audio."""
    if max_seconds is None or max_seconds <= 0 or sr <= 0:
        return audio
    max_samples = int(round(max_seconds * sr))
    if len(audio) <= max_samples:
        return audio
    mid = len(audio) // 2
    half = max_samples // 2
    return audio[max(0, mid - half) : min(len(audio), mid + half)]


def _prepare_essentia_signal(
    path: Optional[Path | str],
    audio: Optional[np.ndarray],
    sr: Optional[int],
    max_analysis_seconds: Optional[float],
    es,
) -> np.ndarray:
    """Return mono float32 audio at ESSENTIA_SAMPLE_RATE, sliced before resampling."""
    if audio is None:
        if path is None:
            raise ValueError("Either path or audio must be provided")
        loaded = es.MonoLoader(filename=str(path), sampleRate=ESSENTIA_SAMPLE_RATE)()
        loaded = np.asarray(loaded, dtype=np.float32)
        return np.ascontiguousarray(
            _slice_central_window(loaded, ESSENTIA_SAMPLE_RATE, max_analysis_seconds),
            dtype=np.float32,
        )

    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim > 1:
        arr = np.mean(arr, axis=0, dtype=np.float32)

    src_sr = sr or ESSENTIA_SAMPLE_RATE
    arr = _slice_central_window(arr, src_sr, max_analysis_seconds)

    if src_sr != ESSENTIA_SAMPLE_RATE and arr.size > 0:
        import librosa

        arr = librosa.resample(
            arr,
            orig_sr=src_sr,
            target_sr=ESSENTIA_SAMPLE_RATE,
            res_type="soxr_qq",
        )
    return np.ascontiguousarray(arr, dtype=np.float32)


def _estimate_with_essentia(
    path: Optional[Path | str],
    audio: Optional[np.ndarray],
    sr: Optional[int],
    genre: Optional[str],
    min_confidence: float,
    max_analysis_seconds: Optional[float],
    octave_fold: bool,
) -> TempoResult:
    import essentia
    import essentia.standard as es

    essentia.log.infoActive = False
    essentia.log.warningActive = False

    signal = _prepare_essentia_signal(path, audio, sr, max_analysis_seconds, es)
    if signal.size < int(MIN_AUDIO_SECONDS * ESSENTIA_SAMPLE_RATE):
        return TempoResult(bpm=None, confidence=0.0, method="essentia_rhythm2013_mf")

    extractor = es.RhythmExtractor2013(method="multifeature")
    bpm_raw, _ticks, conf_raw, _estimates, _intervals = extractor(signal)

    bpm_val = float(bpm_raw)
    conf_val = round(float(conf_raw), 3)
    threshold = required_confidence_for_genre(genre, min_confidence)

    if conf_val < threshold or not is_valid_bpm(bpm_val):
        return TempoResult(bpm=None, confidence=conf_val, method="essentia_rhythm2013_mf")

    bpm_out = round(bpm_val, 2)
    if octave_fold:
        bpm_out = apply_genre_octave_fold(bpm_out, genre)

    return TempoResult(bpm=bpm_out, confidence=conf_val, method="essentia_rhythm2013_mf")


def _estimate_with_librosa_fallback(
    path: Optional[Path | str],
    audio: Optional[np.ndarray],
    sr: Optional[int],
    genre: Optional[str],
    max_analysis_seconds: Optional[float],
    octave_fold: bool,
) -> TempoResult:
    import librosa

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="PySoundFile failed", category=UserWarning)
        warnings.filterwarnings("ignore", message=".*audioread.*", category=FutureWarning)
        if audio is None:
            if path is None:
                raise ValueError("Either path or audio must be provided")
            y, actual_sr = librosa.load(str(path), sr=22050, mono=True)
        else:
            y = np.asarray(audio, dtype=np.float32)
            actual_sr = sr or 22050

    y = _slice_central_window(y, actual_sr, max_analysis_seconds)
    if y.size < int(MIN_AUDIO_SECONDS * actual_sr):
        return TempoResult(bpm=None, confidence=None, method="librosa_fallback")

    onset_env = librosa.onset.onset_strength(y=y, sr=actual_sr)
    detected = librosa.feature.tempo(onset_envelope=onset_env, sr=actual_sr)
    bpm_val = float(detected[0] if hasattr(detected, "__len__") else detected)
    if not is_valid_bpm(bpm_val):
        return TempoResult(bpm=None, confidence=None, method="librosa_fallback")

    bpm_out = round(bpm_val, 2)
    if octave_fold:
        bpm_out = apply_genre_octave_fold(bpm_out, genre)

    return TempoResult(bpm=bpm_out, confidence=None, method="librosa_fallback")


def estimate_tempo(
    path: Optional[Path | str] = None,
    *,
    audio: Optional[np.ndarray] = None,
    sr: Optional[int] = None,
    genre: Optional[str] = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    max_analysis_seconds: Optional[float] = DEFAULT_MAX_ANALYSIS_SECONDS,
    octave_fold: bool = False,
) -> TempoResult:
    """Estimate track BPM and beat confidence using Essentia RhythmExtractor2013.

    Uses a centered slice of up to *max_analysis_seconds* (default 120s) to avoid
    beatless intros/outros while keeping CPU time bounded. Returns ``bpm=None``
    when beat confidence falls below the threshold or outside [35, 220] BPM.
    Falls back to Librosa if Essentia is unavailable.
    """
    try:
        return _estimate_with_essentia(
            path=path,
            audio=audio,
            sr=sr,
            genre=genre,
            min_confidence=min_confidence,
            max_analysis_seconds=max_analysis_seconds,
            octave_fold=octave_fold,
        )
    except ImportError:
        log.debug("essentia unavailable — using librosa tempo fallback")
    except Exception as exc:
        name = Path(path).name if path is not None else "<buffer>"
        log.warning("Tempo detection failed for %s: %s", name, exc)
        return TempoResult(bpm=None, confidence=None, method="error")

    try:
        return _estimate_with_librosa_fallback(
            path=path,
            audio=audio,
            sr=sr,
            genre=genre,
            max_analysis_seconds=max_analysis_seconds,
            octave_fold=octave_fold,
        )
    except Exception as exc:
        name = Path(path).name if path is not None else "<buffer>"
        log.warning("Librosa tempo fallback failed for %s: %s", name, exc)
        return TempoResult(bpm=None, confidence=None, method="error")

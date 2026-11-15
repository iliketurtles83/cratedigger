#!/usr/bin/env python3
"""06_analyze.py — Audio feature extraction (Phase 5).

Walks a folder of songs and/or albums, extracts audio features per file,
and persists a pandas DataFrame to disk as a pickle.  Designed to run
independently of tagging — no audio tags are mutated.

Output schema (one row per audio file):

    path                : str  (absolute file path; primary key)
    mtime               : float (file modification time; incremental key)
    size                : int  (file size in bytes; incremental key)
    artist_tag          : str  (raw artist tag read from file metadata)
    album_tag           : str  (raw album tag read from file metadata)
    track_tag           : str  (raw track number tag read from file metadata)
    year_tag            : str  (raw year/date tag read from file metadata)
    genre_tag           : str  (raw genre tag read from file metadata)
    duration_ms         : int
    bpm_tag             : float (sanitized file BPM tag when present)
    bpm                 : float (audio-estimated tempo in BPM, None if beatless)
    bpm_confidence      : float (Essentia RhythmExtractor2013 confidence)
    key                 : str  (e.g. "C", "F#")
    mode                : str  ("major" | "minor")
    energy              : float (mean RMS, 0..1)
    loudness_lufs       : float (ITU-R BS.1770 integrated LUFS of the original channels at native rate; may be None)
    spectral_centroid   : float (mean Hz)
    zero_crossing_rate  : float (mean 0..1)
    danceability        : float (heuristic proxy 0..1)
    speechiness         : float  (spectral flatness × ZCR proxy; 0=tonal, 1=speech-like)
    acousticness        : float  (HPSS harmonic ratio, penalized by HF rolloff)
    instrumentalness    : float  (inverse vocal activity via MFCC delta variance)
    liveness            : float  (inverse dynamic range; high noise floor ≈ live)
    valence             : float  (mode + tempo + brightness + energy heuristic)
    bpm_method          : str  (tempo estimator used)
    bpm_status          : str  (ok | low_confidence | out_of_range | too_short | failed)
    key_method          : str  (essentia_key_extractor | librosa_ks_chroma)
    key_strength        : float (raw Essentia key strength; None for the fallback)
    dynamic_complexity  : float (raw Essentia DynamicComplexity, dB)
    danceability_raw    : float (raw Essentia DFA danceability)
    mfcc_delta_std      : float (mean std of lower MFCC deltas; vocal-activity proxy input)
    peak_dbfs           : float (sanitized peak level)
    rms_dbfs            : float (sanitized RMS level)
    sanitized_fraction  : float (fraction of samples zeroed as non-finite/decoder spikes)
    signal_status       : str  (ok | silent; silent when peak <= SILENT_PEAK_DBFS)
    embedding           : ndarray(400) float32 (L2-normalized style activations; compatibility)
    style_activations   : ndarray(400) float32 (raw mean Discogs-EffNet style activations)
    audio_embedding     : ndarray(1280) float32 (raw mean Discogs-EffNet latent embedding)
    embedding_patches   : int  (model patches pooled)
    embedding_status    : str  (ok | failed | unavailable | skipped_too_short | skipped_silent | skipped_duration)
    embedding_error     : str | None
    embedding_recipe    : str  (lib.embeddings.EMBEDDING_RECIPE at extraction time)
    embedding_model_sha256 / style_labels_sha256 : str (model and class-label identity)
    schema_version      : int  (FEATURE_SCHEMA_VERSION at extraction time)
    analyzed_at         : str  (ISO 8601 UTC timestamp)
    error               : str | None  (extraction error message, if any)

Migration guidance: bump ``FEATURE_SCHEMA_VERSION`` whenever a feature's
meaning or computation changes.  Rows with an older ``schema_version``
are re-analyzed automatically on the next run.  Embedding columns are
versioned separately by ``EMBEDDING_RECIPE`` and model checksum: failed,
unavailable, or stale embeddings are recomputed without repeating DSP, and a
full re-analysis reuses a current embedding for unchanged audio.

Usage:
    python 06_analyze.py --folder rock
    python 06_analyze.py --folder rock --output features.pkl
    python 06_analyze.py --folder rock --force
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import threading
import time
import warnings
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from multiprocessing import Queue as MPQueue
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import config
from lib import embeddings as embedding_lib
from lib.audiohash import audio_hash
from lib.embeddings import (
    EMBEDDING_COLUMNS,
    EMBEDDING_FAILED,
    EMBEDDING_OK,
    EMBEDDING_SKIPPED_DURATION,
    EMBEDDING_SKIPPED_SILENT,
    EMBEDDING_UNAVAILABLE,
    RETRYABLE_EMBEDDING_STATUSES,
    EmbeddingResult,
    embedding_fields,
    embedding_needs_refresh,
    extract_embeddings,
    reusable_embedding_fields,
    sanitize_audio,
)
from lib.logger import setup_logger
from lib.tags import read_tags
from lib.tempo import estimate_tempo, sanitize_bpm_tag

# 10: HQ model resampling, raw 400+1280 model outputs, embedding/tempo/key status
# and provenance, signal quality, and raw Essentia/MFCC summaries.
# 11: loudness_lufs measured on the original channels at the native rate instead
# of the mono 22.05 kHz downmix (stereo tracks read 3-6 dB low before).
FEATURE_SCHEMA_VERSION = 11
SILENT_PEAK_DBFS = -80.0
DEFAULT_CHECKPOINT_SECONDS = 300.0

log: logging.Logger = logging.getLogger("06_analyze")

_librosa = None
_pyloudnorm = None
_essentia_std = None


def _load_librosa():
    global _librosa
    if _librosa is None:
        try:
            import librosa as lib
            _librosa = lib
        except ImportError:
            _librosa = False
    return _librosa or None


def _load_pyloudnorm():
    global _pyloudnorm
    if _pyloudnorm is None:
        try:
            import pyloudnorm as pln
            _pyloudnorm = pln
        except ImportError:
            _pyloudnorm = False
    return _pyloudnorm or None


def _load_essentia():
    global _essentia_std
    if _essentia_std is None:
        try:
            import essentia
            essentia.log.infoActive = False
            essentia.log.warningActive = False
            import essentia.standard as es_std
            _essentia_std = es_std
        except ImportError:
            _essentia_std = False
    return _essentia_std or None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

FEATURE_COLUMNS: list[str] = [
    "audio_hash", "path", "mtime", "size",
    "artist_tag", "album_tag", "track_tag", "year_tag", "genre_tag",
    "rating_tag", "rating",
    "duration_ms", "bpm_tag", "bpm", "bpm_confidence", "key", "mode",
    "energy", "loudness_lufs",
    "spectral_centroid", "zero_crossing_rate",
    "danceability",
    "speechiness", "acousticness", "instrumentalness", "liveness", "valence",
    "bpm_method", "bpm_status", "key_method", "key_strength",
    "dynamic_complexity", "danceability_raw", "mfcc_delta_std",
    "peak_dbfs", "rms_dbfs", "sanitized_fraction", "signal_status",
    *EMBEDDING_COLUMNS,
    "schema_version", "analyzed_at", "error",
]

# Krumhansl-Schmuckler key profiles
_KS_MAJOR = np.array([
    6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
    2.52, 5.19, 2.39, 3.66, 2.29, 2.88,
])
_KS_MINOR = np.array([
    6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
    2.54, 4.75, 3.98, 2.69, 3.34, 3.17,
])
_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_ENHARMONIC_SHARP_MAP = {
    "Db": "C#",
    "Eb": "D#",
    "Gb": "F#",
    "Ab": "G#",
    "Bb": "A#",
}


def _normalize_key_name(key: str | None) -> str | None:
    """Normalize enharmonic flats returned by Essentia to sharp names."""
    if not key:
        return None
    return _ENHARMONIC_SHARP_MAP.get(key, key)


def _sanitize_audio(y: np.ndarray) -> np.ndarray:
    """Scrub non-finite samples and corrupt decoder spikes (e.g. libmpg123 > 2.0)."""
    return sanitize_audio(y)[0]


def _level_dbfs(value: float) -> float:
    return round(float(20.0 * np.log10(max(value, 1e-10))), 2)


def _signal_quality(y: np.ndarray, zeroed: int, total: int) -> dict[str, Any]:
    """Peak/RMS level and sanitation summary for a sanitized signal."""
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(y, dtype=np.float64)))) if y.size else 0.0
    peak_dbfs = _level_dbfs(peak)
    return {
        "peak_dbfs": peak_dbfs,
        "rms_dbfs": _level_dbfs(rms),
        "sanitized_fraction": round(zeroed / total, 8) if total else None,
        "signal_status": "silent" if peak_dbfs <= SILENT_PEAK_DBFS else "ok",
    }


def _estimate_key(chroma_mean: np.ndarray) -> tuple[str, str]:
    """Return (key, mode) via Krumhansl-Schmuckler correlation."""
    best_corr = -np.inf
    best_key = "C"
    best_mode = "major"
    for i in range(12):
        rotated = np.roll(chroma_mean, -i)
        for profile, label in ((_KS_MAJOR, "major"), (_KS_MINOR, "minor")):
            corr = float(np.corrcoef(rotated, profile)[0, 1])
            if corr > best_corr:
                best_corr = corr
                best_key = _PITCH_CLASSES[i]
                best_mode = label
    return best_key, best_mode


@contextlib.contextmanager
def _suppress_c_stderr():
    """Redirect C-level stderr (fd 2) to /dev/null.

    Python's warnings.filterwarnings cannot silence messages written
    directly by C libraries (e.g. libmpg123 resync notices).
    """
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    saved_fd = os.dup(2)
    os.dup2(devnull_fd, 2)
    os.close(devnull_fd)
    try:
        yield
    finally:
        os.dup2(saved_fd, 2)
        os.close(saved_fd)


def _danceability_proxy(
    bpm: float | None,
    beat_strength: float,
    *,
    dance_raw: float | None = None,
    bpm_confidence: float | None = None,
    duration_s: float | None = None,
) -> float:
    """0..1 danceability gated on detected tempo, preferring Essentia DFA when valid."""
    if not bpm or bpm <= 0:
        return 0.0
    if (
        dance_raw is not None
        and np.isfinite(dance_raw)
        and (duration_s is None or duration_s >= 5.0)
    ):
        score = float(np.clip((dance_raw - 0.6) / 2.0, 0.0, 1.0))
        if bpm_confidence is not None and bpm_confidence < 1.0:
            score *= float(np.clip(bpm_confidence, 0.25, 1.0))
        return round(score, 4)
    tempo_score = float(np.exp(-((bpm - 120.0) ** 2) / (2 * 30.0 ** 2)))
    strength_score = float(min(max(beat_strength, 0.0), 1.0))
    return round(tempo_score * 0.6 + strength_score * 0.4, 4)


def _speechiness_proxy(
    y: np.ndarray,
    librosa,
    *,
    dmfcc: float | None = None,
    S_pow: np.ndarray | None = None,
    sr: int = 22050,
    rms_frames: np.ndarray | None = None,
) -> float:
    """Speech likelihood via syllabic MFCC modulation, pause dynamics, and HF penalty.

    Combines lower-MFCC delta spread (vocal articulation), RMS frame coefficient
    of variation and silence fraction (speech pauses), while penalizing >4 kHz
    cymbal/distortion fizz that misclassified Metal and EDM in the flat/ZCR proxy.
    """
    if S_pow is None:
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
        S_pow = S ** 2
    if rms_frames is None:
        rms_frames = librosa.feature.rms(S=np.sqrt(S_pow))[0]
    if dmfcc is None:
        mel = librosa.feature.melspectrogram(S=S_pow, sr=sr)
        mfccs = librosa.feature.mfcc(S=librosa.power_to_db(mel + 1e-12), n_mfcc=13)
        delta = librosa.feature.delta(mfccs)
        dmfcc = float(np.mean(np.std(delta[:5], axis=1)))

    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    tot_e = np.sum(S_pow, axis=0) + 1e-12
    hf_ratio = float(np.mean(np.sum(S_pow[freqs >= 4000], axis=0) / tot_e))

    mean_rms = float(np.mean(rms_frames)) + 1e-9
    sil = float(np.mean(rms_frames < (0.15 * mean_rms)))
    cv = float(np.std(rms_frames) / mean_rms)

    vocal_mod = 1.0 / (1.0 + float(np.exp(-(dmfcc - 3.5) / 1.5)))
    pause_dyn = float(np.clip((cv - 0.25) / 0.9, 0.0, 1.0))
    sil_score = float(np.clip(sil / 0.35, 0.0, 1.0))
    hf_pen = float(np.clip(1.0 - hf_ratio * 3.0, 0.2, 1.0))
    score = (0.40 * vocal_mod + 0.35 * pause_dyn + 0.25 * sil_score) * hf_pen
    return round(float(np.clip(score, 0.0, 1.0)), 4)


def _acousticness_proxy(
    y: np.ndarray | None,
    y_harm: np.ndarray | None,
    sr: int,
    librosa,
    *,
    S_win: np.ndarray | None = None,
    S_harm: np.ndarray | None = None,
) -> float:
    """Harmonic-to-total energy ratio, penalized by high-frequency spectral rolloff.

    Acoustic instruments → high harmonic ratio + moderate rolloff → high score.
    Distorted electric / synthesizers → high rolloff or low harmonic ratio → lower score.
    """
    if S_win is not None and S_harm is not None:
        harm_energy = float(np.sum(S_harm ** 2))
        total_energy = float(np.sum(S_win ** 2)) + 1e-9
        harm_ratio = float(np.clip(harm_energy / total_energy, 0.0, 1.0))
        rolloff = librosa.feature.spectral_rolloff(S=S_win, sr=sr, roll_percent=0.85)
    else:
        harm_energy = float(np.mean(y_harm ** 2))
        total_energy = float(np.mean(y ** 2)) + 1e-9
        harm_ratio = float(np.clip(harm_energy / total_energy, 0.0, 1.0))
        rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, roll_percent=0.85)
    rolloff_norm = float(np.clip(np.mean(rolloff) / (sr / 2.0), 0.0, 1.0))
    return round(harm_ratio * (1.0 - rolloff_norm * 0.5), 4)


def _instrumentalness_proxy(
    y: np.ndarray,
    sr: int,
    librosa,
    *,
    dmfcc: float | None = None,
) -> float:
    """Inverse vocal activity estimated via lower MFCC delta variance.

    Vocals produce rapid spectral modulation captured in lower MFCC deltas.
    Sigmoid centred at 3.0 (empirical: instruments ≈ 1-2, vocals ≈ 4-8).
    """
    if dmfcc is None:
        mfccs = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
        delta = librosa.feature.delta(mfccs)
        dmfcc = float(np.mean(np.std(delta[:5], axis=1)))
    vocal_norm = 1.0 / (1.0 + float(np.exp(-(dmfcc - 3.0) / 2.0)))
    return round(1.0 - vocal_norm, 4)


def _liveness_proxy(
    y: np.ndarray | None,
    sr: int,
    librosa,
    *,
    dynamic_complexity: float | None = None,
    rms_frames: np.ndarray | None = None,
) -> float:
    """Dynamic-range / loudness-compression indicator (0=wide dynamics, 1=compressed).

    Prefers Essentia's ``DynamicComplexity`` (dB loudness spread, scaled by
    ``1 - dc / 12``) when available, falling back to the RMS p90/p10 ratio.
    """
    if dynamic_complexity is not None and np.isfinite(dynamic_complexity):
        return round(float(np.clip(1.0 - dynamic_complexity / 12.0, 0.0, 1.0)), 4)
    if rms_frames is None:
        rms_frames = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    if len(rms_frames) < 4:
        return 0.0
    noise_floor = float(np.percentile(rms_frames, 10))
    peak_rms = float(np.percentile(rms_frames, 90))
    dynamic_range = peak_rms / (noise_floor + 1e-9)
    # log10(DR): 1→0, 10→0.33, 100→0.67, 1000→1.0; invert and clip.
    liveness = float(np.clip(1.0 - np.log10(max(dynamic_range, 1.0)) / 3.0, 0.0, 1.0))
    return round(liveness, 4)


def _valence_proxy(
    mode: str,
    bpm: float | None,
    spectral_centroid: float | None,
    energy: float | None,
) -> float:
    """Weighted heuristic: major + fast + bright + loud → high valence.

    Weights are rough; mode is the strongest single predictor (~0.15 Pearson
    correlation with Spotify valence in published studies).
    """
    mode_score = 1.0 if mode == "major" else 0.0
    tempo_score = float(np.clip(bpm / 200.0, 0.0, 1.0)) if bpm else 0.5
    centroid_score = float(np.clip(spectral_centroid / 5000.0, 0.0, 1.0)) if spectral_centroid else 0.5
    energy_score = float(np.clip(energy * 8.0, 0.0, 1.0)) if energy else 0.5
    return round(
        0.35 * mode_score + 0.25 * tempo_score + 0.20 * centroid_score + 0.20 * energy_score,
        4,
    )


# ---------------------------------------------------------------------------
# Per-file extraction
# ---------------------------------------------------------------------------

def extract_features(path: Path, prior: Mapping[str, Any] | None = None) -> dict:
    """Extract audio features for a single file.

    *prior* is the stored row for this path; its embedding is reused when it is
    current and the audio hash is unchanged.
    """
    stat = path.stat()
    base: dict = {col: None for col in FEATURE_COLUMNS}
    base["path"] = str(path)
    base["mtime"] = stat.st_mtime
    base["size"] = stat.st_size
    base["schema_version"] = FEATURE_SCHEMA_VERSION
    base["analyzed_at"] = datetime.now(timezone.utc).isoformat()

    try:
        base["audio_hash"] = audio_hash(str(path))
    except Exception as exc:
        log.debug("audio_hash failed for %s: %s", path.name, exc)

    try:
        tags = read_tags(path)
        if tags:
            if tags.get("artist"):
                base["artist_tag"] = tags.get("artist")
            if tags.get("album"):
                base["album_tag"] = tags.get("album")
            if tags.get("track"):
                base["track_tag"] = tags.get("track")
            if tags.get("year"):
                base["year_tag"] = tags.get("year")
            if tags.get("genre"):
                base["genre_tag"] = tags.get("genre")
            if tags.get("bpm"):
                base["bpm_tag"] = sanitize_bpm_tag(tags.get("bpm"))
            if tags.get("rating"):
                try:
                    r_val = float(tags["rating"])
                    base["rating_tag"] = round(r_val, 2)
                    base["rating"] = round(r_val, 2)
                except (TypeError, ValueError):
                    base["rating_tag"] = None
    except Exception as exc:
        log.debug("tag read failed for %s: %s", path.name, exc)

    librosa = _load_librosa()
    if librosa is None:
        base["error"] = "librosa_not_installed"
        return base

    # Quick pre-check using mutagen for long files (> 20 min) to avoid decoding
    try:
        import mutagen
        mf = mutagen.File(str(path))
        if mf is not None and getattr(mf, "info", None) and getattr(mf.info, "length", 0) > 1200:
            stat = path.stat()
            base["mtime"] = stat.st_mtime
            base["size"] = stat.st_size
            base["schema_version"] = FEATURE_SCHEMA_VERSION
            base["duration_ms"] = int(round(mf.info.length * 1000))
            base["error"] = "duration_exceeds_20min"
            base.update(embedding_fields(EmbeddingResult(EMBEDDING_SKIPPED_DURATION)))
            base["analyzed_at"] = datetime.now(timezone.utc).isoformat()
            return base
    except Exception:
        pass

    # Decode once at the native rate for loudness, then analyze a mono 22,050 Hz
    # buffer (identical to librosa.load(sr=22050)) to keep DSP fast and stable.
    try:
        y, sr, native_lufs = _load_analysis_audio_with_loudness(path, librosa)
    except Exception as exc:
        base["error"] = f"load_failed: {exc}"
        return base

    if y.size == 0:
        base["error"] = "empty_audio"
        return base

    y, zeroed = sanitize_audio(y)
    base.update(_signal_quality(y, zeroed, y.size))

    try:
        duration_s = float(librosa.get_duration(y=y, sr=sr))
        base["duration_ms"] = int(round(duration_s * 1000))

        if duration_s > 1200:  # Skip continuous mixes/podcasts longer than 20 minutes
            base["error"] = "duration_exceeds_20min"
            base.update(embedding_fields(EmbeddingResult(EMBEDDING_SKIPPED_DURATION)))
            return base

        tempo_res = estimate_tempo(
            path,
            audio=y,
            sr=sr,
            genre=base.get("genre_tag"),
        )
        base["bpm"] = tempo_res.bpm
        base["bpm_confidence"] = tempo_res.confidence
        base["bpm_method"] = tempo_res.method
        base["bpm_status"] = tempo_res.status
        bpm = base["bpm"]

        # Shared STFT + spectral/mel intermediates so proxies avoid redundant STFTs.
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
        S_pow = S ** 2

        rms_frames = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
        base["energy"] = round(float(np.mean(rms_frames)), 6)

        centroid = librosa.feature.spectral_centroid(S=S, sr=sr)
        base["spectral_centroid"] = round(float(np.mean(centroid)), 2)

        zcr = librosa.feature.zero_crossing_rate(y=y)
        base["zero_crossing_rate"] = round(float(np.mean(zcr)), 6)

        mel = librosa.feature.melspectrogram(S=S_pow, sr=sr)
        mfccs = librosa.feature.mfcc(S=librosa.power_to_db(mel + 1e-12), n_mfcc=13)
        dmfcc = float(np.mean(np.std(librosa.feature.delta(mfccs)[:5], axis=1)))
        base["mfcc_delta_std"] = round(dmfcc, 6)

        # Central 90s window for tonal/rhythmic Essentia extractors.
        if duration_s > 90.0:
            start_s = max(0.0, (duration_s / 2.0) - 45.0)
            end_s = start_s + 90.0
            y_win = y[int(start_s * sr) : int(end_s * sr)]
        else:
            y_win = y

        dance_raw: float | None = None
        dyn_comp: float | None = None
        es_std = _load_essentia()
        if es_std is not None and y_win.size > 0:
            try:
                y44 = librosa.resample(
                    y_win, orig_sr=sr, target_sr=44100, res_type="soxr_qq"
                ).astype(np.float32, copy=False)
                key_raw, mode_raw, key_strength = es_std.KeyExtractor()(y44)
                base["key"] = _normalize_key_name(key_raw)
                base["mode"] = mode_raw
                base["key_method"] = "essentia_key_extractor"
                base["key_strength"] = round(float(key_strength), 6)
                try:
                    d_val, _ = es_std.Danceability()(y44)
                    dance_raw = float(d_val)
                    base["danceability_raw"] = round(dance_raw, 6)
                except Exception as exc:
                    log.debug("Essentia Danceability failed for %s: %s", path.name, exc)
                try:
                    dc_val, _ = es_std.DynamicComplexity()(y44)
                    dyn_comp = float(dc_val)
                    base["dynamic_complexity"] = round(dyn_comp, 6)
                except Exception as exc:
                    log.debug("Essentia DynamicComplexity failed for %s: %s", path.name, exc)
            except Exception as exc:
                log.debug("Essentia key/rhythm extractors failed for %s: %s", path.name, exc)

        if base["key"] is None or base["mode"] is None:
            try:
                y_harm_fallback, _ = librosa.effects.hpss(y_win, kernel_size=15)
            except Exception:
                y_harm_fallback = y_win
            chroma = librosa.feature.chroma_cqt(y=y_harm_fallback, sr=sr)
            key, mode = _estimate_key(chroma.mean(axis=1))
            base["key"] = key
            base["mode"] = mode
            base["key_method"] = "librosa_ks_chroma"
            base["key_strength"] = None

        beat_strength = 0.0
        if bpm is not None and (dance_raw is None or duration_s < 5.0):
            onset_env = librosa.onset.onset_strength(y=y, sr=sr)
            beat_strength = float(np.mean(onset_env) / (np.max(onset_env) + 1e-9))
        base["danceability"] = _danceability_proxy(
            bpm,
            beat_strength,
            dance_raw=dance_raw,
            bpm_confidence=tempo_res.confidence,
            duration_s=duration_s,
        )

        base["loudness_lufs"] = native_lufs

        try:
            base["speechiness"] = _speechiness_proxy(
                y,
                librosa,
                dmfcc=dmfcc,
                S_pow=S_pow,
                sr=sr,
                rms_frames=rms_frames,
            )
        except Exception as exc:
            log.debug("speechiness proxy failed for %s: %s", path.name, exc)

        try:
            if duration_s > 30.0:
                start_f = int(librosa.time_to_frames((duration_s / 2.0) - 15.0, sr=sr, hop_length=512))
                end_f = int(librosa.time_to_frames((duration_s / 2.0) + 15.0, sr=sr, hop_length=512))
                end_f = min(end_f, S.shape[1])
                start_f = max(0, min(start_f, end_f))
                S_win = S[:, start_f:end_f] if (end_f - start_f) >= 16 else S
            else:
                S_win = S
            try:
                S_harm, _ = librosa.decompose.hpss(S_win, kernel_size=15)
            except Exception:
                S_harm = S_win
            base["acousticness"] = _acousticness_proxy(
                None,
                None,
                sr,
                librosa,
                S_win=S_win,
                S_harm=S_harm,
            )
        except Exception as exc:
            log.debug("acousticness proxy failed for %s: %s", path.name, exc)

        try:
            base["instrumentalness"] = _instrumentalness_proxy(
                y, sr, librosa, dmfcc=dmfcc
            )
        except Exception as exc:
            log.debug("instrumentalness proxy failed for %s: %s", path.name, exc)

        try:
            base["liveness"] = _liveness_proxy(
                y,
                sr,
                librosa,
                dynamic_complexity=dyn_comp,
                rms_frames=rms_frames,
            )
        except Exception as exc:
            log.debug("liveness proxy failed for %s: %s", path.name, exc)

        try:
            base["valence"] = _valence_proxy(
                base["mode"], bpm, base.get("spectral_centroid"), base.get("energy")
            )
        except Exception as exc:
            log.debug("valence proxy failed for %s: %s", path.name, exc)

        base.update(_embedding_for(path, y, sr, base, prior))

    except Exception as exc:
        base["error"] = f"feature_failed: {exc}"

    return base


def _load_analysis_audio(path: Path, librosa) -> tuple[np.ndarray, int]:
    """Decode *path* exactly as ``lib.embeddings.decode_audio`` does, muting decoder noise."""
    with warnings.catch_warnings(), _suppress_c_stderr():
        warnings.filterwarnings("ignore", category=UserWarning)
        warnings.filterwarnings("ignore", category=FutureWarning)
        y, sr = librosa.load(str(path), sr=embedding_lib.DECODE_SAMPLE_RATE, mono=True)
    return y, int(sr)


def _integrated_loudness(native: np.ndarray, native_sr: int) -> float | None:
    """BS.1770 integrated loudness of the decoded channels (pyloudnorm supports up to five)."""
    pln = _load_pyloudnorm()
    if pln is None:
        return None
    channels = native if native.ndim == 1 else native.T
    if channels.shape[0] < int(0.5 * native_sr):
        return None
    clean, _ = sanitize_audio(channels)
    try:
        lufs = float(pln.Meter(native_sr).integrated_loudness(clean))
    except Exception as exc:
        log.debug("LUFS failed: %s", exc)
        return None
    return round(lufs, 2) if np.isfinite(lufs) and lufs <= 0.0 else None


def _load_analysis_audio_with_loudness(path: Path, librosa) -> tuple[np.ndarray, int, float | None]:
    """Decode once at the native rate, measure loudness on the original channels,
    then derive the same mono buffer ``_load_analysis_audio`` returns
    (``librosa.load`` downmixes and resamples in this order)."""
    with warnings.catch_warnings(), _suppress_c_stderr():
        warnings.filterwarnings("ignore", category=UserWarning)
        warnings.filterwarnings("ignore", category=FutureWarning)
        native, native_sr = librosa.load(str(path), sr=None, mono=False)
    native_sr = int(native_sr)
    lufs = _integrated_loudness(native, native_sr)
    y = librosa.to_mono(native)
    del native
    target_sr = embedding_lib.DECODE_SAMPLE_RATE
    if native_sr != target_sr:
        y = librosa.resample(y, orig_sr=native_sr, target_sr=target_sr, res_type="soxr_hq")
    return y, target_sr, lufs


def _embedding_for(
    path: Path,
    y: np.ndarray,
    sr: int,
    base: Mapping[str, Any],
    prior: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if base.get("signal_status") == "silent":
        return embedding_fields(EmbeddingResult(EMBEDDING_SKIPPED_SILENT, error="silent_signal"))
    reused = reusable_embedding_fields(prior, base.get("audio_hash"))
    if reused is not None:
        log.debug("Reusing current embedding for %s", path.name)
        return reused
    return embedding_fields(extract_embeddings(path, audio=y, sr=sr))


def extract_embedding_update(path: Path) -> dict[str, Any]:
    """Recompute only embedding columns for a row whose DSP features are current."""
    librosa = _load_librosa()
    if librosa is None:
        return embedding_fields(EmbeddingResult(EMBEDDING_UNAVAILABLE, error="librosa_not_installed"))
    try:
        y, sr = _load_analysis_audio(path, librosa)
    except Exception as exc:
        return embedding_fields(EmbeddingResult(EMBEDDING_FAILED, error=f"load_failed: {exc}"[:500]))
    y, _ = sanitize_audio(y)
    return embedding_fields(extract_embeddings(path, audio=y, sr=sr))


# Module-level slot set by _worker_init in each worker process.
_worker_start_q: MPQueue | None = None

FULL_ANALYSIS = "full"
EMBEDDING_ONLY = "embedding"

AnalysisTask = tuple[str, Path, Mapping[str, Any] | None]


def _worker_init(q: MPQueue, model_threads: int | None = None) -> None:
    """ProcessPoolExecutor initializer; Ctrl+C is handled by the parent, which stops workers."""
    global _worker_start_q
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _worker_start_q = q
    embedding_lib.set_inference_threads(model_threads)


def _run_task(task: AnalysisTask) -> dict:
    mode, path, prior = task
    if mode == EMBEDDING_ONLY:
        return extract_embedding_update(path)
    return extract_features(path, prior)


def _worker_run(task: AnalysisTask) -> dict:
    """Worker wrapper: signals the main process when it starts a file."""
    try:
        if _worker_start_q is not None:
            _worker_start_q.put(str(task[1]), block=False)
    except Exception:
        pass
    return _run_task(task)


# ---------------------------------------------------------------------------
# Folder walk + incremental run
# ---------------------------------------------------------------------------

def _iter_audio_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() in config.AUDIO_EXTENSIONS:
            return [root]
        return []
    skip = {s.lower() for s in getattr(config, "SKIP_FOLDERS", set())} | {"0mixes"}
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        if any(part.lower() in skip for part in p.relative_to(root).parts):
            continue
        out.append(p)
    return sorted(out)


def _load_existing(output_path: Path) -> pd.DataFrame:
    if not output_path.exists():
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    try:
        df = pd.read_pickle(output_path)
        if not isinstance(df, pd.DataFrame):
            raise TypeError("Feature store must contain a pandas DataFrame")
        for col in FEATURE_COLUMNS:
            if col not in df.columns:
                df[col] = None
        if "error" in df.columns:
            df["error"] = df["error"].where(df["error"].notna(), None)
        return df[FEATURE_COLUMNS]
    except Exception as exc:
        raise RuntimeError(
            f"Could not load existing feature store {output_path}; stopping to "
            "avoid overwriting saved progress. Use the Python environment that "
            "created the store (normally .venv/bin/python) with a compatible "
            "pandas version."
        ) from exc


def _has_error(val) -> bool:
    if val is None or pd.isna(val):
        return False
    return bool(val)


def _needs_analysis(row, path: Path) -> bool:
    if row is None:
        return True
    try:
        stat = path.stat()
    except OSError:
        return False
    if row.get("schema_version") != FEATURE_SCHEMA_VERSION:
        return True
    if not row.get("audio_hash"):
        return True
    if row.get("error") == "duration_exceeds_20min" and row.get("size") == stat.st_size:
        return False
    if _has_error(row.get("error")):
        return True
    if row.get("mtime") != stat.st_mtime:
        return True
    if row.get("size") != stat.st_size:
        return True
    return False


def _save_dataframe(rows: dict[str, dict], output_path: Path) -> pd.DataFrame:
    df = pd.DataFrame(list(rows.values()), columns=FEATURE_COLUMNS)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp")
    df.to_pickle(tmp_path)
    tmp_path.replace(output_path)
    return df


class PreflightError(RuntimeError):
    """Extraction dependencies or model are unusable; nothing was scheduled."""


def _within(scope: Path, path_str: str) -> bool:
    candidate = Path(path_str)
    return candidate == scope or scope in candidate.parents


def _prior_embedding(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Embedding columns (plus hash) worth reusing if a full re-analysis finds unchanged audio."""
    if row is None or row.get("embedding_status") != EMBEDDING_OK or embedding_needs_refresh(row):
        return None
    return {"audio_hash": row.get("audio_hash"), **{col: row.get(col) for col in EMBEDDING_COLUMNS}}


def _error_row(path: Path, message: str) -> dict[str, Any]:
    features: dict[str, Any] = {col: None for col in FEATURE_COLUMNS}
    features["path"] = str(path)
    try:
        stat = path.stat()
        features["mtime"] = stat.st_mtime
        features["size"] = stat.st_size
    except OSError:
        pass
    features["schema_version"] = FEATURE_SCHEMA_VERSION
    features["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    features["error"] = message
    return features


def _worker_failure_result(task: AnalysisTask, exc: BaseException) -> dict[str, Any]:
    message = f"worker_error: {exc}"[:500]
    if task[0] == EMBEDDING_ONLY:
        return embedding_fields(EmbeddingResult(EMBEDDING_FAILED, error=message))
    return _error_row(task[1], message)


def _merge_full_result(rows: dict[str, dict], key: str, features: dict) -> bool:
    """Store a full-analysis row; return True if it failed.

    A failed recomputation keeps an existing error-free row unchanged, so old
    evidence keeps its old version and is retried on the next run.
    """
    err = features.get("error")
    failed = _has_error(err) and err != "duration_exceeds_20min"
    previous = rows.get(key)
    if failed and previous is not None and not _has_error(previous.get("error")):
        return True
    rows[key] = features
    return failed


def _merge_embedding_result(rows: dict[str, dict], key: str, fields: dict) -> None:
    """Update embedding columns, keeping a previous successful embedding if this attempt failed."""
    previous = rows.get(key)
    if previous is None:
        return
    if (
        fields.get("embedding_status") in RETRYABLE_EMBEDDING_STATUSES
        and previous.get("embedding_status") == EMBEDDING_OK
    ):
        return
    rows[key] = {**previous, **fields}


def _run_preflight(parallel: bool) -> list[str]:
    if not parallel:
        return embedding_lib.preflight()
    # Probe in a spawned process so forked workers never inherit ONNX Runtime threads.
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as probe:
        return probe.submit(embedding_lib.preflight).result()


def _stop_executor(executor: ProcessPoolExecutor) -> None:
    """Cancel queued work and terminate workers without waiting for in-flight files."""
    processes = list((getattr(executor, "_processes", None) or {}).values())
    executor.shutdown(wait=False, cancel_futures=True)
    for proc in processes:
        if proc.is_alive():
            proc.terminate()
    for proc in processes:
        proc.join(timeout=5.0)


def analyze_folder(
    folder: Path,
    output_path: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    workers: int = 1,
    checkpoint_seconds: float = DEFAULT_CHECKPOINT_SECONDS,
    model_threads: int | None = None,
) -> pd.DataFrame:
    """Run feature extraction on *folder*, persisting to *output_path*.

    ``workers`` controls the number of parallel worker processes (1 runs
    sequentially).  Rows outside *folder* are preserved.  Progress is saved at
    most every ``checkpoint_seconds`` and on Ctrl+C, which stops all workers.
    ``model_threads`` sets ONNX Runtime threads per worker (default:
    ``cpu_count // workers`` when parallel, runtime default when sequential).
    """
    audio_files = _iter_audio_files(folder)
    log.info("Found %d audio files under %s", len(audio_files), folder)

    existing = pd.DataFrame(columns=FEATURE_COLUMNS) if force else _load_existing(output_path)
    existing_by_path: dict[str, dict] = {}
    existing_by_hash: dict[str, dict] = {}
    if not existing.empty:
        for _, row in existing.iterrows():
            d = row.to_dict()
            existing_by_path[str(row["path"])] = d
            if d.get("audio_hash"):
                existing_by_hash[d["audio_hash"]] = d

    rows: dict[str, dict] = dict(existing_by_path)

    # Separate skip determination from extraction so parallelism is clean.
    tasks: list[AnalysisTask] = []
    skipped = 0
    moved = 0
    claimed_paths: set[str] = set()
    relocated_from: set[str] = set()

    for path in audio_files:
        key = str(path)
        current = existing_by_path.get(key)
        if not force and current is not None and not _needs_analysis(current, path):
            claimed_paths.add(key)
            if embedding_needs_refresh(current):
                tasks.append((EMBEDDING_ONLY, path, None))
            else:
                skipped += 1
            continue

        # Check if this file is a renamed/moved file whose audio_hash already exists
        ah = None
        if not force and current is None and existing_by_hash:
            try:
                ah = audio_hash(key)
            except Exception:
                ah = None
        source = existing_by_hash.get(ah) if ah else None

        if (
            source is not None
            and source.get("schema_version") == FEATURE_SCHEMA_VERSION
            and (
                not _has_error(source.get("error"))
                or source.get("error") == "duration_exceeds_20min"
            )
        ):
            matched_row = dict(source)
            stat = path.stat()
            matched_row["path"] = key
            matched_row["mtime"] = stat.st_mtime
            matched_row["size"] = stat.st_size
            try:
                tags = read_tags(path)
                if tags:
                    for field, tag_key in [
                        ("artist_tag", "artist"),
                        ("album_tag", "album"),
                        ("track_tag", "track"),
                        ("year_tag", "year"),
                        ("genre_tag", "genre"),
                    ]:
                        if tags.get(tag_key):
                            matched_row[field] = tags[tag_key]
            except Exception:
                pass
            rows[key] = matched_row
            claimed_paths.add(key)
            source_path = str(source.get("path"))
            if source_path != key and not Path(source_path).exists():
                relocated_from.add(source_path)
            moved += 1
            log.info("Recognized moved/renamed file by audio_hash: %s", path.name)
            if embedding_needs_refresh(matched_row):
                tasks.append((EMBEDDING_ONLY, path, None))
            else:
                skipped += 1
        else:
            tasks.append((FULL_ANALYSIS, path, None if force else _prior_embedding(current)))

    # Prune rows for files missing from the scanned scope (or moved away from
    # anywhere); rows elsewhere survive unmounted media and subset runs.
    pruned = 0
    for path_str in list(rows.keys()):
        if path_str in claimed_paths:
            continue
        if path_str in relocated_from or (_within(folder, path_str) and not Path(path_str).exists()):
            del rows[path_str]
            pruned += 1
    if pruned:
        log.info("Pruned %d row(s) for files no longer on disk", pruned)

    n_full = sum(1 for task in tasks if task[0] == FULL_ANALYSIS)
    n_embedding = len(tasks) - n_full
    parallel = workers > 1 and len(tasks) > 1

    if tasks:
        problems = _run_preflight(parallel and not dry_run)
        if problems:
            message = "Extraction preflight failed: " + "; ".join(problems)
            if not dry_run:
                raise PreflightError(f"{message}. No files were analyzed; {output_path} is unchanged.")
            log.warning("[DRY-RUN] %s", message)

    if dry_run:
        log.info(
            "[DRY-RUN] Would analyze %d file(s) (%d full, %d embedding-only); skip %d current; "
            "prune %d orphan(s); output %s",
            len(tasks), n_full, n_embedding, skipped, pruned, output_path,
        )
        for mode, path, _ in tasks[:20]:
            log.info("[DRY-RUN]   %-9s %s", mode, path)
        if len(tasks) > 20:
            log.info("[DRY-RUN]   ... and %d more", len(tasks) - 20)
        return pd.DataFrame(list(rows.values()), columns=FEATURE_COLUMNS)

    threads = model_threads or (max(1, (os.cpu_count() or 1) // workers) if parallel else None)
    log.info(
        "Skipping %d current file(s); analyzing %d (%d full, %d embedding-only) with %d worker(s)%s",
        skipped, len(tasks), n_full, n_embedding, workers if parallel else 1,
        f", {threads} model thread(s) each" if threads else "",
    )

    failed = 0
    embedding_issues = 0
    done = 0
    recorded = 0
    unsaved = 0
    last_save = time.monotonic()

    def record(task: AnalysisTask, result: dict[str, Any]) -> None:
        nonlocal failed, embedding_issues, recorded, unsaved
        mode, path, _ = task
        key = str(path)
        if mode == EMBEDDING_ONLY:
            _merge_embedding_result(rows, key, result)
            status = result.get("embedding_status")
            if status in RETRYABLE_EMBEDDING_STATUSES:
                embedding_issues += 1
            log.info("[%d/%d] Embedding %s -> %s", done, len(tasks), path.name, status)
        elif _merge_full_result(rows, key, result):
            failed += 1
            log.warning("[%d/%d] %s -> %s", done, len(tasks), path.name, result.get("error"))
        else:
            if result.get("embedding_status") in RETRYABLE_EMBEDDING_STATUSES:
                embedding_issues += 1
                log.warning(
                    "[%d/%d] %s embedding %s: %s", done, len(tasks), path.name,
                    result.get("embedding_status"), result.get("embedding_error"),
                )
            log.info("[%d/%d] Done  %s", done, len(tasks), path.name)
        recorded += 1
        unsaved += 1

    def maybe_checkpoint() -> None:
        nonlocal unsaved, last_save
        if unsaved and time.monotonic() - last_save >= checkpoint_seconds:
            started = time.perf_counter()
            _save_dataframe(rows, output_path)
            log.info(
                "Checkpoint: saved %d rows to %s in %.1fs",
                len(rows), output_path, time.perf_counter() - started,
            )
            unsaved = 0
            last_save = time.monotonic()

    def save_interrupted() -> None:
        _save_dataframe(rows, output_path)
        log.warning(
            "Interrupted after %d/%d completed file(s); saved %d rows to %s. Re-run the same command to resume.",
            recorded, len(tasks), len(rows), output_path,
        )

    if parallel:
        # Each worker signals the main process when it starts a file so progress
        # is visible immediately, not only on completion.
        start_q: MPQueue = MPQueue()

        def _drain_start_queue() -> None:
            while True:
                msg = start_q.get()
                if msg is None:
                    break
                log.debug("  -> %s", Path(msg).name)

        drain_thread = threading.Thread(target=_drain_start_queue, daemon=True)
        drain_thread.start()

        # Forked workers must never inherit a live ONNX Runtime thread pool.
        embedding_lib.release_inference_session()
        executor = ProcessPoolExecutor(
            max_workers=workers, initializer=_worker_init, initargs=(start_q, threads)
        )
        completed = False
        try:
            futures = {executor.submit(_worker_run, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                done += 1
                try:
                    result = future.result()
                except Exception as exc:
                    if type(exc).__name__ in ("BrokenProcessPool", "BrokenExecutor"):
                        log.error("Process pool broke (%s) at [%d/%d]; halting batch to protect store integrity", exc, done, len(tasks))
                        break
                    log.warning("[%d/%d] Worker error for %s: %s", done, len(tasks), task[1].name, exc)
                    record(task, _worker_failure_result(task, exc))
                else:
                    record(task, result)
                maybe_checkpoint()
            else:
                completed = True
        except KeyboardInterrupt:
            save_interrupted()
            raise
        finally:
            if completed:
                executor.shutdown(wait=True)
            else:
                _stop_executor(executor)
            start_q.put(None)  # sentinel: stop drain thread
            drain_thread.join(timeout=2.0)
    else:
        if threads:
            embedding_lib.set_inference_threads(threads)
        try:
            for task in tasks:
                done += 1
                log.info("[%d/%d] Analyzing %s", done, len(tasks), task[1].name)
                record(task, _run_task(task))
                maybe_checkpoint()
        except KeyboardInterrupt:
            save_interrupted()
            raise

    log.info(
        "Analyzed %d, skipped %d (incremental), failed %d, embedding failures %d",
        len(tasks), skipped, failed, embedding_issues,
    )

    if tasks or pruned or moved:
        df = _save_dataframe(rows, output_path)
        log.info("Wrote %d rows to %s", len(df), output_path)
    else:
        df = pd.DataFrame(list(rows.values()), columns=FEATURE_COLUMNS)
        log.info("No changes; %s not rewritten", output_path)

    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_output_path() -> Path:
    configured = getattr(config, "FEATURES_STORE_PATH", None)
    if configured:
        return Path(configured)
    return Path("features.pkl")


def main() -> None:
    global log

    parser = argparse.ArgumentParser(description="Extract audio features to a pickled DataFrame")
    parser.add_argument("--folder", type=str, default=None,
                        help="Top-level folder relative to MUSIC_ROOT, or an absolute path")
    parser.add_argument("--output", type=str, default=None,
                        help="Output pickle path (default: FEATURES_STORE_PATH or features.pkl)")
    parser.add_argument("--force", action="store_true",
                        help="Re-analyze all files, ignoring incremental cache")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run extraction without writing the pickle")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1),
                        help="Parallel worker processes (default: min(4, cpu_count); "
                             "librosa.load is I/O-heavy so more workers can thrash disk)")
    parser.add_argument("--checkpoint-seconds", type=float, default=DEFAULT_CHECKPOINT_SECONDS,
                        help="Minimum seconds between progress saves (default: %(default)s); "
                             "progress is also saved on completion and Ctrl+C")
    parser.add_argument("--model-threads", type=int, default=None,
                        help="ONNX Runtime threads per worker (default: cpu_count // workers)")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    log = setup_logger("06_analyze", Path(args.log))

    if args.folder:
        candidate = Path(args.folder)
        folder = candidate if candidate.is_absolute() else (config.MUSIC_ROOT / candidate)
    else:
        folder = config.MUSIC_ROOT

    if not folder.exists():
        log.error("Folder does not exist: %s", folder)
        return

    output_path = Path(args.output) if args.output else _default_output_path()

    try:
        analyze_folder(
            folder,
            output_path,
            force=args.force,
            dry_run=args.dry_run,
            workers=args.workers,
            checkpoint_seconds=args.checkpoint_seconds,
            model_threads=args.model_threads,
        )
    except PreflightError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

"""Truncation-safe audio decoding shared by feature extraction and embeddings.

librosa decodes MP3 through libsndfile/libmpg123, which can give up at corrupt
frames ("Giving up resync") and return a partial signal without raising.  A
decode noticeably shorter than the container's declared length is retried with
ffmpeg, which skips damaged frames, and the longer result is kept.  Healthy
files are decoded exactly as ``librosa.load`` decodes them.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

log = logging.getLogger("lib.decode")

DECODER_LIBROSA = "librosa"
DECODER_FFMPEG = "ffmpeg"
# A decode is suspect when it is below 97% of the declared length *and* more than
# 2 s short; encoder delay/padding and VBR length estimates stay well inside this.
TRUNCATION_MAX_COVERAGE = 0.97
TRUNCATION_MIN_SHORTFALL_S = 2.0
FFMPEG_TIMEOUT_S = 600.0
ANALYSIS_RESAMPLE_TYPE = "soxr_hq"

_UNSET: Any = object()


@dataclass(frozen=True)
class NativeAudio:
    """Decoded samples at the native rate, shaped like ``librosa.load(sr=None, mono=False)``."""

    samples: np.ndarray
    sr: int
    decoder: str = DECODER_LIBROSA
    header_seconds: Optional[float] = None

    @property
    def seconds(self) -> float:
        return self.samples.shape[-1] / self.sr if self.sr else 0.0

    @property
    def coverage(self) -> Optional[float]:
        """Decoded duration / declared duration (None when the header has no length)."""
        if not self.header_seconds:
            return None
        return round(self.seconds / self.header_seconds, 4)

    @property
    def truncated(self) -> bool:
        return is_short(self.seconds, self.header_seconds)


def is_short(decoded_seconds: float, header_seconds: Optional[float]) -> bool:
    if not header_seconds:
        return False
    return (
        decoded_seconds < header_seconds * TRUNCATION_MAX_COVERAGE
        and header_seconds - decoded_seconds > TRUNCATION_MIN_SHORTFALL_S
    )


def header_seconds(path: Path | str) -> Optional[float]:
    """Declared duration from the container header via mutagen, or None if unreadable."""
    try:
        import mutagen

        mf = mutagen.File(str(path))
        length = float(getattr(getattr(mf, "info", None), "length", None) or 0.0)
    except Exception as exc:
        log.debug("Header duration unavailable for %s: %s", Path(path).name, exc)
        return None
    return length if np.isfinite(length) and length > 0 else None


@contextlib.contextmanager
def suppress_c_stderr():
    """Redirect C-level stderr (fd 2) to /dev/null.

    Python's warnings filters cannot silence messages written directly by C
    libraries (e.g. libmpg123 resync notices).
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


def ffmpeg_decode(path: Path | str, sr: int, channels: int) -> Optional[np.ndarray]:
    """Decode the first audio stream with ffmpeg at *sr*/*channels*; None if unavailable or empty."""
    exe = shutil.which("ffmpeg")
    if exe is None or channels < 1:
        return None
    cmd = [
        exe, "-nostdin", "-v", "quiet", "-i", str(path), "-map", "0:a:0",
        "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffmpeg decode failed for %s: %s", Path(path).name, exc)
        return None
    data = np.frombuffer(proc.stdout, dtype=np.float32)
    frames = data.size // channels
    if frames == 0:
        return None
    interleaved = data[: frames * channels].reshape(frames, channels)
    if channels == 1:
        return interleaved[:, 0].copy()
    return np.ascontiguousarray(interleaved.T)


def decode_native(path: Path | str, librosa: Any = None, *, header_s: Any = _UNSET) -> NativeAudio:
    """Decode *path* at its native rate and channel count, retrying short decodes with ffmpeg.

    *header_s* may pass an already-read declared duration (None if unknown).
    Exceptions from the primary decoder propagate.
    """
    if librosa is None:
        import librosa
    with warnings.catch_warnings(), suppress_c_stderr():
        warnings.filterwarnings("ignore", category=UserWarning)
        warnings.filterwarnings("ignore", category=FutureWarning)
        samples, sr = librosa.load(str(path), sr=None, mono=False)
    if header_s is _UNSET:
        header_s = header_seconds(path)
    audio = NativeAudio(samples, int(sr), DECODER_LIBROSA, header_s)
    if not audio.truncated:
        return audio
    channels = 1 if samples.ndim == 1 else int(samples.shape[0])
    retry = ffmpeg_decode(path, audio.sr, channels)
    if retry is None and shutil.which("ffmpeg") is None:
        log.warning("ffmpeg unavailable; cannot recover short decode for %s", Path(path).name)
    if retry is None or retry.shape[-1] <= samples.shape[-1]:
        return audio
    log.info(
        "Recovered %s with ffmpeg: %.1fs -> %.1fs of %.1fs declared",
        Path(path).name, audio.seconds, retry.shape[-1] / audio.sr, header_s,
    )
    return NativeAudio(retry, audio.sr, DECODER_FFMPEG, header_s)


def analysis_buffer(audio: NativeAudio, librosa: Any, target_sr: int) -> np.ndarray:
    """Mono buffer at *target_sr*, identical to ``librosa.load(sr=target_sr, mono=True)``."""
    y = librosa.to_mono(audio.samples)
    if audio.sr != target_sr:
        y = librosa.resample(y, orig_sr=audio.sr, target_sr=target_sr, res_type=ANALYSIS_RESAMPLE_TYPE)
    return y

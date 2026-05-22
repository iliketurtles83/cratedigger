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
    bpm                 : float (from file BPM tag when present)
    key                 : str  (e.g. "C", "F#")
    mode                : str  ("major" | "minor")
    energy              : float (mean RMS, 0..1)
    loudness_lufs       : float (integrated LUFS, may be None)
    spectral_centroid   : float (mean Hz)
    zero_crossing_rate  : float (mean 0..1)
    danceability        : float (heuristic proxy 0..1)
    speechiness         : float  (spectral flatness × ZCR proxy; 0=tonal, 1=speech-like)
    acousticness        : float  (HPSS harmonic ratio, penalized by HF rolloff)
    instrumentalness    : float  (inverse vocal activity via MFCC delta variance)
    liveness            : float  (inverse dynamic range; high noise floor ≈ live)
    valence             : float  (mode + tempo + brightness + energy heuristic)
    schema_version      : int  (FEATURE_SCHEMA_VERSION at extraction time)
    analyzed_at         : str  (ISO 8601 UTC timestamp)
    error               : str | None  (extraction error message, if any)

Migration guidance: bump ``FEATURE_SCHEMA_VERSION`` whenever a feature's
meaning or computation changes.  Rows with an older ``schema_version``
are re-analyzed automatically on the next run.

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
import threading
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from multiprocessing import Queue as MPQueue
from pathlib import Path

import numpy as np
import pandas as pd

import config
from lib.logger import setup_logger
from lib.tags import read_tags

FEATURE_SCHEMA_VERSION = 6  # bpm sourced from tags; added artist/album/track tag columns

log: logging.Logger = logging.getLogger("06_analyze")

_librosa = None
_pyloudnorm = None


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


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

FEATURE_COLUMNS: list[str] = [
    "path", "mtime", "size",
    "artist_tag", "album_tag", "track_tag", "year_tag", "genre_tag",
    "duration_ms", "bpm", "key", "mode",
    "energy", "loudness_lufs",
    "spectral_centroid", "zero_crossing_rate",
    "danceability",
    "speechiness", "acousticness", "instrumentalness", "liveness", "valence",
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


def _danceability_proxy(bpm: float, beat_strength: float) -> float:
    """Heuristic 0..1 danceability from tempo (peaked at 120 BPM) + beat strength."""
    if not bpm or bpm <= 0:
        return 0.0
    tempo_score = float(np.exp(-((bpm - 120.0) ** 2) / (2 * 30.0 ** 2)))
    strength_score = float(min(max(beat_strength, 0.0), 1.0))
    return round(tempo_score * 0.6 + strength_score * 0.4, 4)


def _speechiness_proxy(y: np.ndarray, librosa) -> float:
    """Spectral flatness × ZCR proxy.

    Speech is noise-like (higher flatness) with moderate-to-high ZCR.
    Tonal music has very low flatness.  Output is 0..1.

    Calibration: typical tonal music → 0.05-0.25; rap/spoken word → 0.50-0.90.
    """
    flatness = librosa.feature.spectral_flatness(y=y)
    # Flatness for tonal music ≈ 0.01-0.10; scale ×5 so 0.20 maps to 1.0.
    flatness_norm = float(np.clip(np.mean(flatness) * 5.0, 0.0, 1.0))
    zcr = librosa.feature.zero_crossing_rate(y=y)
    # ZCR for speech ≈ 0.05-0.15; normalize with 0.15 ceiling.
    zcr_norm = float(np.clip(np.mean(zcr) / 0.15, 0.0, 1.0))
    return round(0.5 * flatness_norm + 0.5 * zcr_norm, 4)


def _acousticness_proxy(y: np.ndarray, y_harm: np.ndarray, sr: int, librosa) -> float:
    """Harmonic-to-total energy ratio, penalized by high-frequency spectral rolloff.

    Acoustic instruments → high harmonic ratio + moderate rolloff → high score.
    Distorted electric / synthesizers → high rolloff or low harmonic ratio → lower score.
    """
    harm_energy = float(np.mean(y_harm ** 2))
    total_energy = float(np.mean(y ** 2)) + 1e-9
    harm_ratio = float(np.clip(harm_energy / total_energy, 0.0, 1.0))
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, roll_percent=0.85)
    rolloff_norm = float(np.clip(np.mean(rolloff) / (sr / 2.0), 0.0, 1.0))
    return round(harm_ratio * (1.0 - rolloff_norm * 0.5), 4)


def _instrumentalness_proxy(y: np.ndarray, sr: int, librosa) -> float:
    """Inverse vocal activity estimated via lower MFCC delta variance.

    Vocals produce rapid spectral modulation captured in lower MFCC deltas.
    Sigmoid centred at 3.0 (empirical: instruments ≈ 1-2, vocals ≈ 4-8).
    """
    mfccs = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    delta = librosa.feature.delta(mfccs)
    vocal_activity = float(np.mean(np.std(delta[:5], axis=1)))
    vocal_norm = 1.0 / (1.0 + float(np.exp(-(vocal_activity - 3.0) / 2.0)))
    return round(1.0 - vocal_norm, 4)


def _liveness_proxy(y: np.ndarray, sr: int, librosa) -> float:
    """Inverse dynamic range proxy.

    NOTE: this responds to *any* loudness compression, not just live
    recordings.  Brick-wall-mastered studio EDM/pop scores high; a
    well-mastered live recording with genuine quiet passages scores low.
    Treat as a compression/noise-floor indicator more than a true
    liveness signal.
    """
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

def extract_features(path: Path) -> dict:
    """Extract audio features for a single file."""
    stat = path.stat()
    base: dict = {col: None for col in FEATURE_COLUMNS}
    base["path"] = str(path)
    base["mtime"] = stat.st_mtime
    base["size"] = stat.st_size
    base["schema_version"] = FEATURE_SCHEMA_VERSION
    base["analyzed_at"] = datetime.now(timezone.utc).isoformat()

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
                try:
                    bpm_tag = float(tags["bpm"])
                    base["bpm"] = round(bpm_tag, 2) if bpm_tag > 0 else None
                except (TypeError, ValueError):
                    base["bpm"] = None
    except Exception as exc:
        log.debug("tag read failed for %s: %s", path.name, exc)

    librosa = _load_librosa()
    if librosa is None:
        base["error"] = "librosa_not_installed"
        return base

    try:
        with warnings.catch_warnings(), _suppress_c_stderr():
            warnings.filterwarnings("ignore", category=UserWarning)
            warnings.filterwarnings("ignore", category=FutureWarning)
            y, sr = librosa.load(str(path), sr=None, mono=True)
    except Exception as exc:
        base["error"] = f"load_failed: {exc}"
        return base

    if y.size == 0:
        base["error"] = "empty_audio"
        return base

    try:
        duration_s = float(librosa.get_duration(y=y, sr=sr))
        base["duration_ms"] = int(round(duration_s * 1000))

        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        bpm = base.get("bpm")

        beat_strength = float(np.mean(onset_env) / (np.max(onset_env) + 1e-9))
        base["danceability"] = _danceability_proxy(bpm, beat_strength)

        rms = librosa.feature.rms(y=y)
        base["energy"] = round(float(np.mean(rms)), 6)

        centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
        base["spectral_centroid"] = round(float(np.mean(centroid)), 2)

        zcr = librosa.feature.zero_crossing_rate(y=y)
        base["zero_crossing_rate"] = round(float(np.mean(zcr)), 6)

        # Harmonic component improves key estimation for drum-heavy material.
        try:
            y_harm, _ = librosa.effects.hpss(y)
        except Exception:
            y_harm = y
        chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr)
        chroma_mean = chroma.mean(axis=1)
        key, mode = _estimate_key(chroma_mean)
        base["key"] = key
        base["mode"] = mode

        pln = _load_pyloudnorm()
        if pln is not None and duration_s >= 0.5:
            try:
                meter = pln.Meter(sr)
                lufs = float(meter.integrated_loudness(y))
                base["loudness_lufs"] = round(lufs, 2) if np.isfinite(lufs) else None
            except Exception as exc:
                log.debug("LUFS failed for %s: %s", path.name, exc)

        # DIY proxy features — no trained model required.
        try:
            base["speechiness"] = _speechiness_proxy(y, librosa)
        except Exception as exc:
            log.debug("speechiness proxy failed for %s: %s", path.name, exc)

        try:
            base["acousticness"] = _acousticness_proxy(y, y_harm, sr, librosa)
        except Exception as exc:
            log.debug("acousticness proxy failed for %s: %s", path.name, exc)

        try:
            base["instrumentalness"] = _instrumentalness_proxy(y, sr, librosa)
        except Exception as exc:
            log.debug("instrumentalness proxy failed for %s: %s", path.name, exc)

        try:
            base["liveness"] = _liveness_proxy(y, sr, librosa)
        except Exception as exc:
            log.debug("liveness proxy failed for %s: %s", path.name, exc)

        try:
            base["valence"] = _valence_proxy(
                mode, bpm, base.get("spectral_centroid"), base.get("energy")
            )
        except Exception as exc:
            log.debug("valence proxy failed for %s: %s", path.name, exc)

    except Exception as exc:
        base["error"] = f"feature_failed: {exc}"

    return base


# Module-level slot set by _worker_init in each worker process.
_worker_start_q: MPQueue | None = None


def _worker_init(q: MPQueue) -> None:
    """ProcessPoolExecutor initializer: store the shared queue in the worker global."""
    global _worker_start_q
    _worker_start_q = q


def _worker_extract(path: Path) -> dict:
    """Worker wrapper: signals the main process when extraction starts, then delegates."""
    try:
        if _worker_start_q is not None:
            _worker_start_q.put(str(path), block=False)
    except Exception:
        pass
    return extract_features(path)


# ---------------------------------------------------------------------------
# Folder walk + incremental run
# ---------------------------------------------------------------------------

def _iter_audio_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.suffix.lower() in config.AUDIO_EXTENSIONS:
            return [root]
        return []
    skip = {s.lower() for s in getattr(config, "SKIP_FOLDERS", set())}
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
            return pd.DataFrame(columns=FEATURE_COLUMNS)
        for col in FEATURE_COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df[FEATURE_COLUMNS]
    except Exception as exc:
        log.warning("Could not load existing feature store %s: %s", output_path, exc)
        return pd.DataFrame(columns=FEATURE_COLUMNS)


def _needs_analysis(row, path: Path) -> bool:
    if row is None:
        return True
    try:
        stat = path.stat()
    except OSError:
        return False
    if row.get("error"):
        return True
    if row.get("schema_version") != FEATURE_SCHEMA_VERSION:
        return True
    if row.get("mtime") != stat.st_mtime:
        return True
    if row.get("size") != stat.st_size:
        return True
    return False


def analyze_folder(
    folder: Path,
    output_path: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    workers: int = 1,
) -> pd.DataFrame:
    """Run feature extraction on *folder*, persisting to *output_path*.

    ``workers`` controls the number of parallel worker processes.  Set to 1
    (the default) for sequential execution.  The CLI defaults to
    ``os.cpu_count()``.
    """
    audio_files = _iter_audio_files(folder)
    log.info("Found %d audio files under %s", len(audio_files), folder)

    existing = pd.DataFrame(columns=FEATURE_COLUMNS) if force else _load_existing(output_path)
    existing_by_path: dict[str, dict] = {}
    if not existing.empty:
        for _, row in existing.iterrows():
            existing_by_path[str(row["path"])] = row.to_dict()

    # Prune rows whose audio file no longer exists. Without this, renames
    # and deletes accumulate dead rows forever in the pickle.
    pruned = 0
    for path_str in list(existing_by_path.keys()):
        if not Path(path_str).exists():
            del existing_by_path[path_str]
            pruned += 1
    if pruned:
        log.info("Pruned %d row(s) for files no longer on disk", pruned)

    rows: dict[str, dict] = dict(existing_by_path)

    # Separate skip determination from extraction so parallelism is clean.
    to_analyze: list[Path] = []
    skipped = 0
    for path in audio_files:
        current = existing_by_path.get(str(path))
        if not force and current is not None and not _needs_analysis(current, path):
            skipped += 1
        else:
            to_analyze.append(path)

    if dry_run:
        log.info(
            "[DRY-RUN] Would analyze %d file(s); skip %d unchanged; prune %d orphan(s); output %s",
            len(to_analyze), skipped, pruned, output_path,
        )
        for path in to_analyze[:20]:
            log.info("[DRY-RUN]   %s", path)
        if len(to_analyze) > 20:
            log.info("[DRY-RUN]   ... and %d more", len(to_analyze) - 20)
        return pd.DataFrame(list(rows.values()), columns=FEATURE_COLUMNS)

    log.info(
        "Skipping %d unchanged file(s); analyzing %d file(s) with %d worker(s)",
        skipped, len(to_analyze), workers,
    )

    failed = 0

    if workers > 1 and len(to_analyze) > 1:
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

        with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=(start_q,)) as executor:
            futures = {
                executor.submit(_worker_extract, path): path
                for path in to_analyze
            }
            done = 0
            for future in as_completed(futures):
                path = futures[future]
                done += 1
                try:
                    features = future.result()
                except Exception as exc:
                    log.warning("[%d/%d] Worker error for %s: %s", done, len(to_analyze), path.name, exc)
                    stat = path.stat()
                    features = {col: None for col in FEATURE_COLUMNS}
                    features["path"] = str(path)
                    features["mtime"] = stat.st_mtime
                    features["size"] = stat.st_size
                    features["schema_version"] = FEATURE_SCHEMA_VERSION
                    features["analyzed_at"] = datetime.now(timezone.utc).isoformat()
                    features["error"] = f"worker_error: {exc}"
                    failed += 1
                else:
                    if features.get("error"):
                        failed += 1
                        log.warning("[%d/%d] %s -> %s", done, len(to_analyze), path.name, features["error"])
                    else:
                        log.info("[%d/%d] Done  %s", done, len(to_analyze), path.name)
                rows[str(path)] = features

        start_q.put(None)  # sentinel: stop drain thread
        drain_thread.join(timeout=2.0)
    else:
        for idx, path in enumerate(to_analyze, start=1):
            log.info("[%d/%d] Analyzing %s", idx, len(to_analyze), path.name)
            features = extract_features(path)
            if features.get("error"):
                failed += 1
                log.warning("  -> %s", features["error"])
            rows[str(path)] = features

    log.info("Analyzed %d, skipped %d (incremental), failed %d", len(to_analyze), skipped, failed)

    df = pd.DataFrame(list(rows.values()), columns=FEATURE_COLUMNS)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_pickle(output_path)
    log.info("Wrote %d rows to %s", len(df), output_path)

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

    analyze_folder(
        folder,
        output_path,
        force=args.force,
        dry_run=args.dry_run,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()

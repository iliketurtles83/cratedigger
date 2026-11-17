"""Build a read-only analytics snapshot from a completed feature store."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any

import numpy as np
import pandas as pd

from lib.recommender import deduplicate_features_by_audio_hash

SNAPSHOT_VERSION = 2
DEFAULT_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "analytics" / "library.sqlite3"

_SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tracks (
    audio_hash TEXT PRIMARY KEY,
    path TEXT,
    artist TEXT,
    album TEXT,
    title TEXT,
    year TEXT,
    release_year INTEGER,
    genre TEXT,
    genre_tier1 TEXT,
    genre_tier2 TEXT,
    rating REAL,
    duration_ms INTEGER,
    size INTEGER,
    file_format TEXT,
    schema_version INTEGER,
    error TEXT,
    bpm REAL,
    bpm_confidence REAL,
    musical_key TEXT,
    mode TEXT,
    loudness_lufs REAL,
    native_peak_dbfs REAL,
    danceability_raw REAL,
    decode_coverage REAL,
    signal_status TEXT,
    analyzed_at TEXT
);
CREATE INDEX idx_tracks_year ON tracks(release_year);
CREATE INDEX idx_tracks_genre1_year ON tracks(genre_tier1, release_year);
CREATE INDEX idx_tracks_genre2_year ON tracks(genre_tier2, release_year);
CREATE TABLE summary (
    tracks INTEGER NOT NULL,
    artists INTEGER NOT NULL,
    albums INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    duration_tracks INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    sized_tracks INTEGER NOT NULL,
    errors INTEGER NOT NULL
);
CREATE TABLE breakdown (
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (kind, label)
);
CREATE TABLE schema_counts (
    version INTEGER,
    count INTEGER NOT NULL,
    errors INTEGER NOT NULL
);
CREATE TABLE genre_year (
    tier INTEGER NOT NULL,
    release_year INTEGER NOT NULL,
    genre TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (tier, release_year, genre)
);
"""


def source_file_state(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _scalar(value: Any, field: str) -> Any:
    if value is None:
        return None
    missing = pd.isna(value)
    if not isinstance(missing, (bool, np.bool_)):
        raise ValueError(f"Expected a scalar value in {field}")
    return None if missing else value


def _text(value: Any, field: str) -> str | None:
    value = _scalar(value, field)
    if value is None:
        return None
    if isinstance(value, (float, np.floating)) and math.isfinite(value) and float(value).is_integer():
        return str(int(value))
    result = str(value).strip()
    return result or None


def _number(value: Any, field: str, *, integer: bool = False) -> int | float | None:
    value = _scalar(value, field)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric value in {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"Non-finite numeric value in {field}: {value!r}")
    if integer:
        if not number.is_integer():
            raise ValueError(f"Expected an integer in {field}: {value!r}")
        return int(number)
    return number


def _release_year(year: str | None) -> int | None:
    if not year:
        return None
    match = re.match(r"^(\d{4})(?:$|[-/])", year)
    if not match:
        return None
    value = int(match.group(1))
    return value if 1800 <= value <= datetime.now(timezone.utc).year + 1 else None


def _genre_tiers(genre: str | None) -> tuple[str | None, str | None]:
    parts = [part.strip() for part in (genre or "").split("/") if part.strip()]
    if not parts:
        return None, None
    return parts[0], " / ".join(parts[:2])


def _snapshot_rows(frame: pd.DataFrame) -> tuple[list[tuple[Any, ...]], dict[str, int]]:
    if "audio_hash" not in frame.columns:
        raise ValueError("Feature store is missing the audio_hash column")
    valid = frame["audio_hash"].map(lambda value: isinstance(value, str) and bool(value.strip()))
    hashed = frame.loc[valid].reset_index(drop=True)
    canonical = deduplicate_features_by_audio_hash(hashed)
    rows = []
    for _, row in canonical.iterrows():
        path = _text(row.get("path"), "path")
        genre = _text(row.get("genre_tag"), "genre_tag")
        genre1, genre2 = _genre_tiers(genre)
        year = _text(row.get("year_tag"), "year_tag")
        rows.append((
            row["audio_hash"],
            path,
            _text(row.get("artist_tag"), "artist_tag"),
            _text(row.get("album_tag"), "album_tag"),
            _text(row.get("title_tag"), "title_tag"),
            year,
            _release_year(year),
            genre,
            genre1,
            genre2,
            _number(row.get("rating"), "rating"),
            _number(row.get("duration_ms"), "duration_ms", integer=True),
            _number(row.get("size"), "size", integer=True),
            Path(path).suffix.lower().lstrip(".") or None if path else None,
            _number(row.get("schema_version"), "schema_version", integer=True),
            _text(row.get("error"), "error"),
            _number(row.get("bpm"), "bpm"),
            _number(row.get("bpm_confidence"), "bpm_confidence"),
            _text(row.get("key"), "key"),
            _text(row.get("mode"), "mode"),
            _number(row.get("loudness_lufs"), "loudness_lufs"),
            _number(row.get("native_peak_dbfs"), "native_peak_dbfs"),
            _number(row.get("danceability_raw"), "danceability_raw"),
            _number(row.get("decode_coverage"), "decode_coverage"),
            _text(row.get("signal_status"), "signal_status"),
            _text(row.get("analyzed_at"), "analyzed_at"),
        ))
    return rows, {
        "source_rows": len(frame),
        "unique_tracks": len(rows),
        "duplicate_rows": len(hashed) - len(rows),
        "unhashed_rows": len(frame) - len(hashed),
    }


def _populate_aggregates(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO summary "
        "SELECT COUNT(*), COUNT(DISTINCT NULLIF(TRIM(artist), '') COLLATE NOCASE), "
        "(SELECT COUNT(*) FROM (SELECT artist, album FROM tracks "
        "WHERE NULLIF(TRIM(artist), '') IS NOT NULL AND NULLIF(TRIM(album), '') IS NOT NULL "
        "GROUP BY artist COLLATE NOCASE, album COLLATE NOCASE)), "
        "COALESCE(SUM(CASE WHEN duration_ms > 0 THEN duration_ms ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN duration_ms > 0 THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN size > 0 THEN size ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN size > 0 THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END), 0) "
        "FROM tracks"
    )
    for column in ("genre_tier1", "file_format"):
        label = f"COALESCE(NULLIF(TRIM({column}), ''), 'Unknown')"
        conn.execute(
            f"INSERT INTO breakdown SELECT ?, {label}, COUNT(*) FROM tracks GROUP BY {label}",
            (column,),
        )
    conn.execute(
        "INSERT INTO schema_counts "
        "SELECT schema_version, COUNT(*), "
        "SUM(CASE WHEN error IS NOT NULL AND error != '' THEN 1 ELSE 0 END) "
        "FROM tracks GROUP BY schema_version"
    )
    for tier, column in ((1, "genre_tier1"), (2, "genre_tier2")):
        genre = f"COALESCE(NULLIF(TRIM({column}), ''), 'Unknown')"
        conn.execute(
            f"INSERT INTO genre_year SELECT ?, release_year, {genre}, COUNT(*) FROM tracks "
            f"WHERE release_year IS NOT NULL GROUP BY release_year, {genre}",
            (tier,),
        )


def build_snapshot(
    features_path: Path,
    output_path: Path,
    *,
    dry_run: bool = True,
    expected_source_state: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Preview or atomically publish a snapshot; never modify the source store."""
    source = features_path.resolve()
    output = output_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Feature store does not exist: {source}")
    if source == output or (output.exists() and os.path.samefile(source, output)):
        raise ValueError("The analytics snapshot must not overwrite the feature store")
    if output.suffix.lower() not in {".sqlite", ".sqlite3", ".db"}:
        raise ValueError("The analytics snapshot must be a SQLite file")

    signature = source_file_state(source)
    if expected_source_state is not None and signature != expected_source_state:
        raise RuntimeError("Feature store changed after the job was queued; no snapshot was written")
    frame = pd.read_pickle(source)
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Feature store does not contain a pandas DataFrame")
    if source_file_state(source) != signature:
        raise RuntimeError("Feature store changed while reading; no snapshot was written")
    rows, counts = _snapshot_rows(frame)
    generated_at = datetime.now(timezone.utc).isoformat()
    summary: dict[str, Any] = {
        **counts,
        "generated_at": generated_at,
        "source": str(source),
        "source_size": signature[2],
        "source_mtime_ns": signature[3],
        "output": str(output),
        "dry_run": dry_run,
    }
    if dry_run:
        if source_file_state(source) != signature:
            raise RuntimeError("Feature store changed during snapshot preview; no snapshot was written")
        return summary

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".analytics-", suffix=".sqlite3", dir=output.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with closing(sqlite3.connect(temp_path)) as conn:
            conn.executescript(_SCHEMA)
            metadata = {
                "snapshot_version": SNAPSHOT_VERSION,
                "generated_at": generated_at,
                "source": str(source),
                "source_size": signature[2],
                "source_mtime_ns": signature[3],
                **counts,
            }
            conn.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in metadata.items()],
            )
            placeholders = ",".join("?" for _ in range(26))
            conn.executemany(f"INSERT INTO tracks VALUES ({placeholders})", rows)
            _populate_aggregates(conn)
            conn.commit()
        if source_file_state(source) != signature:
            raise RuntimeError("Feature store changed during snapshot creation; no snapshot was published")
        os.replace(temp_path, output)
    finally:
        temp_path.unlink(missing_ok=True)
    return summary

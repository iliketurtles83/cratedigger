"""Operational SQLite database for cratedigger state, plans, review queue, and undo journal."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

import pandas as pd

DEFAULT_DB_PATH = Path("cratedigger.db")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tracks (
    audio_hash       TEXT PRIMARY KEY,
    canonical_path   TEXT,
    artist           TEXT,
    albumartist      TEXT,
    album            TEXT,
    title            TEXT,
    year             TEXT,
    genre            TEXT,
    rating           REAL,
    duration_ms      INTEGER,
    file_format      TEXT,
    mb_trackid       TEXT,
    mb_albumid       TEXT,
    mb_artistid      TEXT,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS track_paths (
    path             TEXT PRIMARY KEY,
    audio_hash       TEXT NOT NULL,
    size             INTEGER,
    mtime_ns         INTEGER,
    is_canonical     INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_track_paths_hash ON track_paths(audio_hash);

CREATE TABLE IF NOT EXISTS artists (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name   TEXT NOT NULL,
    comparison_key   TEXT NOT NULL,
    bucket           TEXT NOT NULL,
    mb_artistid      TEXT,
    folder_path      TEXT UNIQUE NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artists_key ON artists(comparison_key);
CREATE INDEX IF NOT EXISTS idx_artists_mbid ON artists(mb_artistid);

CREATE TABLE IF NOT EXISTS review_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key         TEXT UNIQUE NOT NULL,
    queue_type       TEXT NOT NULL,
    reason           TEXT NOT NULL,
    path             TEXT NOT NULL,
    audio_hash       TEXT,
    payload_json     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    resolution_json  TEXT,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_queue_status ON review_items(queue_type, status, reason);
CREATE INDEX IF NOT EXISTS idx_review_path ON review_items(path);

CREATE TABLE IF NOT EXISTS plans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scope            TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft',
    summary_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    applied_at       TEXT
);

CREATE TABLE IF NOT EXISTS plan_actions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id          INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    action_type      TEXT NOT NULL,
    source_path      TEXT,
    target_path      TEXT,
    audio_hash       TEXT,
    payload_json     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_plan_actions_plan ON plan_actions(plan_id, status);

CREATE TABLE IF NOT EXISTS undo_journal (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id          INTEGER REFERENCES plans(id) ON DELETE SET NULL,
    run_id           TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    op_type          TEXT NOT NULL,
    src_path         TEXT,
    dst_path         TEXT,
    before_json      TEXT,
    after_json       TEXT,
    reverted         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_undo_run ON undo_journal(run_id, seq);

CREATE TABLE IF NOT EXISTS mb_cache (
    cache_key        TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key              TEXT PRIMARY KEY,
    value_json       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    operation_id     TEXT NOT NULL,
    params_json      TEXT NOT NULL,
    mode             TEXT NOT NULL CHECK (mode IN ('preview', 'apply')),
    fingerprint      TEXT NOT NULL,
    argv_json        TEXT NOT NULL,
    locks_json       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'queued',
    pid              INTEGER,
    pgid             INTEGER,
    exit_code        INTEGER,
    log_path         TEXT NOT NULL,
    error            TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_locks (
    lock_key         TEXT NOT NULL,
    job_id           TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    PRIMARY KEY (lock_key, job_id)
);

CREATE TABLE IF NOT EXISTS ui_mutation_locks (
    lock_key         TEXT PRIMARY KEY,
    owner_token      TEXT NOT NULL,
    owner_pid        INTEGER NOT NULL,
    process_start    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ui_resource_changes (
    lock_key         TEXT NOT NULL,
    changed_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ui_resource_changes ON ui_resource_changes(lock_key, changed_at);
"""


def utc_now_iso() -> str:
    """Return current UTC timestamp as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes idempotently."""
    conn.executescript(_SCHEMA_SQL)
    lock_columns = {row[1]: row[5] for row in conn.execute("PRAGMA table_info(job_locks)")}
    if lock_columns.get("job_id") != 2:
        conn.execute("BEGIN IMMEDIATE")
        try:
            lock_columns = {row[1]: row[5] for row in conn.execute("PRAGMA table_info(job_locks)")}
            if lock_columns.get("job_id") != 2:
                conn.execute(
                    "CREATE TABLE job_locks_new ("
                    "lock_key TEXT NOT NULL, "
                    "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, "
                    "PRIMARY KEY (lock_key, job_id))"
                )
                conn.execute("INSERT INTO job_locks_new SELECT lock_key, job_id FROM job_locks")
                conn.execute("DROP TABLE job_locks")
                conn.execute("ALTER TABLE job_locks_new RENAME TO job_locks")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
    conn.commit()


@contextmanager
def open_db(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open a WAL-mode SQLite connection with schema initialized."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        init_schema(conn)
        yield conn
    finally:
        conn.close()


def upsert_review_item(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    queue_type: str,
    reason: str,
    path: str,
    audio_hash: str | None = None,
    payload: dict[str, Any] | None = None,
    status: str = "pending",
    resolution: dict[str, Any] | None = None,
    now: str | None = None,
) -> None:
    """Insert or update a review queue item deterministically by *item_key*."""
    ts = now or utc_now_iso()
    resolved_at = ts if status in {"auto_resolved", "resolved", "dismissed", "pruned"} else None
    payload_str = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
    res_str = json.dumps(resolution, sort_keys=True, ensure_ascii=False) if resolution is not None else None

    conn.execute(
        """
        INSERT INTO review_items (
            item_key, queue_type, reason, path, audio_hash,
            payload_json, status, resolution_json, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            queue_type = excluded.queue_type,
            reason = excluded.reason,
            path = excluded.path,
            audio_hash = COALESCE(excluded.audio_hash, review_items.audio_hash),
            payload_json = excluded.payload_json,
            status = CASE
                WHEN review_items.status IN ('resolved', 'dismissed') THEN review_items.status
                ELSE excluded.status
            END,
            resolution_json = CASE
                WHEN review_items.status IN ('resolved', 'dismissed') THEN review_items.resolution_json
                ELSE excluded.resolution_json
            END,
            resolved_at = CASE
                WHEN review_items.status IN ('resolved', 'dismissed') THEN review_items.resolved_at
                ELSE excluded.resolved_at
            END
        """,
        (item_key, queue_type, reason, path, audio_hash, payload_str, status, res_str, ts, resolved_at),
    )


def review_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Return nested counts grouped by queue_type and status."""
    counts: dict[str, dict[str, int]] = {}
    rows = conn.execute(
        "SELECT queue_type, status, COUNT(*) AS cnt FROM review_items GROUP BY queue_type, status"
    ).fetchall()
    for row in rows:
        q = counts.setdefault(row["queue_type"], {})
        q[row["status"]] = int(row["cnt"])
    return counts


def sync_features_into_db(
    conn: sqlite3.Connection,
    features_path: Path | str = Path("features.pkl"),
) -> dict[str, int]:
    """Import track identities and path mappings from features.pkl into SQLite.

    Stores one canonical row per ``audio_hash`` in ``tracks`` and all observed
    filesystem locations in ``track_paths``.
    """
    from lib.recommender import deduplicate_features_by_audio_hash

    fpath = Path(features_path)
    if not fpath.exists():
        return {"tracks": 0, "paths": 0, "duplicate_hashes": 0}

    df = pd.read_pickle(fpath)
    if df.empty or "audio_hash" not in df.columns:
        return {"tracks": 0, "paths": 0, "duplicate_hashes": 0}

    ts = utc_now_iso()
    deduped = deduplicate_features_by_audio_hash(df)
    canonical_by_hash: dict[str, str] = {}

    track_rows = 0
    for _, row in deduped.iterrows():
        ah = row.get("audio_hash")
        if not isinstance(ah, str) or not ah:
            continue
        p_str = str(row.get("path") or "")
        canonical_by_hash[ah] = p_str
        rating_val = row.get("rating")
        rating = float(rating_val) if pd.notna(rating_val) else None
        dur_val = row.get("duration_ms")
        duration_ms = int(dur_val) if pd.notna(dur_val) else None
        fmt = Path(p_str).suffix.lower().lstrip(".") if p_str else None
        conn.execute(
            """
            INSERT INTO tracks (
                audio_hash, canonical_path, artist, album, title, year, genre,
                rating, duration_ms, file_format, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audio_hash) DO UPDATE SET
                canonical_path = excluded.canonical_path,
                artist = COALESCE(excluded.artist, tracks.artist),
                album = COALESCE(excluded.album, tracks.album),
                title = COALESCE(excluded.title, tracks.title),
                year = COALESCE(excluded.year, tracks.year),
                genre = COALESCE(excluded.genre, tracks.genre),
                rating = COALESCE(excluded.rating, tracks.rating),
                duration_ms = COALESCE(excluded.duration_ms, tracks.duration_ms),
                file_format = COALESCE(excluded.file_format, tracks.file_format),
                updated_at = excluded.updated_at
            """,
            (
                ah,
                p_str,
                str(row["artist_tag"]) if pd.notna(row.get("artist_tag")) else None,
                str(row["album_tag"]) if pd.notna(row.get("album_tag")) else None,
                str(row["track_tag"]) if pd.notna(row.get("track_tag")) else None,
                str(row["year_tag"]) if pd.notna(row.get("year_tag")) else None,
                str(row["genre_tag"]) if pd.notna(row.get("genre_tag")) else None,
                rating,
                duration_ms,
                fmt,
                ts,
            ),
        )
        track_rows += 1

    path_rows = 0
    for _, row in df.iterrows():
        ah = row.get("audio_hash")
        p_str = row.get("path")
        if not isinstance(ah, str) or not ah or not isinstance(p_str, str) or not p_str:
            continue
        size_val = row.get("size")
        size = int(size_val) if pd.notna(size_val) else None
        mtime_val = row.get("mtime")
        mtime_ns = int(float(mtime_val) * 1_000_000_000) if pd.notna(mtime_val) else None
        is_canonical = 1 if canonical_by_hash.get(ah) == p_str else 0
        conn.execute(
            """
            INSERT INTO track_paths (path, audio_hash, size, mtime_ns, is_canonical, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                audio_hash = excluded.audio_hash,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                is_canonical = excluded.is_canonical,
                updated_at = excluded.updated_at
            """,
            (p_str, ah, size, mtime_ns, is_canonical, ts),
        )
        path_rows += 1

    conn.commit()
    return {
        "tracks": track_rows,
        "paths": path_rows,
        "duplicate_hashes": max(0, path_rows - track_rows),
    }

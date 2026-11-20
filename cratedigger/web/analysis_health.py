"""Browse extraction evidence from the read-only analytics snapshot."""

from __future__ import annotations

from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from cratedigger.web.insights import read_snapshot_metadata

PAGE_SIZE = 25
_FILTERS = {
    "error": "NULLIF(TRIM(t.error), '') IS NOT NULL",
    "truncated": "t.signal_status = 'truncated'",
    "silent": "t.signal_status = 'silent'",
    "excluded": "v.audio_hash IS NULL",
}


def _text(value: Any, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Analytics snapshot has invalid {field} metadata")
    return value


def load_analysis_health(
    snapshot_path: Path, *, kind: str = "all", query: str = "", page: int = 1, per_page: int = PAGE_SIZE
) -> dict[str, Any] | None:
    """Return overlapping issue counts and one page of snapshot evidence."""
    if kind not in {"all", *_FILTERS} or page < 1 or not 1 <= per_page <= 100 or len(query) > 200:
        raise ValueError("Invalid extraction health filters")
    if not snapshot_path.exists():
        return None
    if not snapshot_path.is_file():
        raise ValueError(f"Analytics snapshot is not a file: {snapshot_path}")
    uri = snapshot_path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            metadata = read_snapshot_metadata(conn)
            has_vectors = metadata["snapshot_version"] >= 3
            expressions = {key: value for key, value in _FILTERS.items() if key != "excluded" or has_vectors}
            expressions["all"] = "(" + " OR ".join(expressions.values()) + ")"
            join = "LEFT JOIN similarity_vectors AS v ON v.audio_hash=t.audio_hash" if has_vectors else ""
            columns = ", ".join(
                f"SUM(CASE WHEN {expression} THEN 1 ELSE 0 END) AS {key}_count"
                for key, expression in expressions.items()
            )
            summary = conn.execute(f"SELECT COUNT(*) AS tracks, {columns} FROM tracks AS t {join}").fetchone()
            total_tracks = int(summary["tracks"])
            counts = {key: int(summary[f"{key}_count"] or 0) for key in expressions}
            if total_tracks != metadata["unique_tracks"]:
                raise ValueError("Analytics snapshot track count does not match its metadata")
            if has_vectors:
                vector_count = int(conn.execute("SELECT COUNT(*) FROM similarity_vectors").fetchone()[0])
                if counts["excluded"] != metadata["similarity_excluded"] or vector_count != metadata["similarity_tracks"]:
                    raise ValueError("Analytics snapshot similarity counts do not match its contents")

            unavailable_filter = kind == "excluded" and not has_vectors
            where = expressions.get(kind, "0")
            params: list[Any] = []
            if query:
                fields = ("t.audio_hash", "t.artist", "t.album", "t.title", "t.path", "t.error")
                where += " AND (" + " OR ".join(f"instr(lower(coalesce({field}, '')), lower(?)) > 0" for field in fields) + ")"
                params.extend([query] * len(fields))
            matching = int(conn.execute(f"SELECT COUNT(*) FROM tracks AS t {join} WHERE {where}", params).fetchone()[0])
            pages = max(1, (matching + per_page - 1) // per_page)
            page = min(page, pages)
            vector_column = "v.audio_hash IS NOT NULL" if has_vectors else "NULL"
            rows = conn.execute(
                f"SELECT t.audio_hash, t.path, t.artist, t.album, t.title, t.error, t.signal_status, "
                f"t.decode_coverage, {vector_column} AS has_vector FROM tracks AS t {join} WHERE {where} "
                f"ORDER BY CASE WHEN {_FILTERS['error']} THEN 0 WHEN {_FILTERS['truncated']} THEN 1 "
                f"WHEN {_FILTERS['silent']} THEN 2 ELSE 3 END, "
                "t.artist COLLATE NOCASE, t.album COLLATE NOCASE, t.title COLLATE NOCASE, t.audio_hash "
                "LIMIT ? OFFSET ?",
                [*params, per_page, (page - 1) * per_page],
            )
            entries = []
            for row in rows:
                audio_hash = _text(row["audio_hash"], "audio hash")
                if not audio_hash or not audio_hash.strip():
                    raise ValueError("Analytics snapshot has invalid audio hash metadata")
                path = _text(row["path"], "path")
                title = _text(row["title"], "title")
                error = _text(row["error"], "error")
                status = _text(row["signal_status"], "signal status")
                coverage = row["decode_coverage"]
                if coverage is not None and (not isinstance(coverage, (int, float)) or not math.isfinite(coverage)):
                    raise ValueError("Analytics snapshot has invalid decode coverage")
                entries.append({
                    "audio_hash": audio_hash,
                    "path": path,
                    "artist": _text(row["artist"], "artist"),
                    "album": _text(row["album"], "album"),
                    "title": title or (Path(path).stem if path else "(unknown title)"),
                    "error": error if error and error.strip() else None,
                    "signal_status": status or "unknown",
                    "coverage": float(coverage) if coverage is not None else None,
                    "has_vector": bool(row["has_vector"]) if has_vectors else None,
                })
            return {
                "metadata": metadata,
                "tracks": total_tracks,
                "counts": counts,
                "has_vectors": has_vectors,
                "unavailable_filter": unavailable_filter,
                "entries": entries,
                "total": matching,
                "page": page,
                "pages": pages,
            }
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read analytics snapshot: {exc}") from exc

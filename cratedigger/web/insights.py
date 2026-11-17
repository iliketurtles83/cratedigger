"""Read aggregate insights from the published analytics snapshot."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any

from cratedigger.analytics import SNAPSHOT_VERSION

_COLORS = (
    "#65b9ff", "#ffad66", "#7cdda4", "#e593df", "#e7d270",
    "#9ea4ff", "#ff8793", "#7bd5d2", "#c5a77f", "#c3d58c",
)


def _read_metadata(conn: sqlite3.Connection) -> dict[str, Any]:
    metadata = {row["key"]: json.loads(row["value"]) for row in conn.execute("SELECT key, value FROM metadata")}
    if metadata.get("snapshot_version") != SNAPSHOT_VERSION:
        raise ValueError("Unsupported analytics snapshot version; rebuild the snapshot")
    for key in ("generated_at", "source_rows", "unique_tracks", "duplicate_rows", "unhashed_rows"):
        if key not in metadata:
            raise ValueError(f"Analytics snapshot is missing metadata: {key}")
    if not isinstance(metadata["generated_at"], str) or not metadata["generated_at"]:
        raise ValueError("Analytics snapshot has an invalid timestamp; rebuild the snapshot")
    for key in ("source_rows", "unique_tracks", "duplicate_rows", "unhashed_rows"):
        if type(metadata[key]) is not int or metadata[key] < 0:
            raise ValueError(f"Analytics snapshot has an invalid count: {key}")
    if metadata["source_rows"] != metadata["unique_tracks"] + metadata["duplicate_rows"] + metadata["unhashed_rows"]:
        raise ValueError("Analytics snapshot counts are inconsistent; rebuild the snapshot")
    return metadata


def _counts(conn: sqlite3.Connection, column: str, *, total: int, limit: int = 10) -> list[dict[str, Any]]:
    if column not in {"genre_tier1", "file_format"}:
        raise ValueError("Unsupported insight breakdown")
    rows = conn.execute(
        "SELECT label, count FROM breakdown WHERE kind=? ORDER BY count DESC, label COLLATE NOCASE LIMIT ?",
        (column, limit),
    ).fetchall()
    return [
        {"label": row["label"], "count": row["count"], "percent": round(100 * row["count"] / total, 1) if total else 0}
        for row in rows
    ]


def _timeline(conn: sqlite3.Connection, *, tier: int, top_n: int) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT release_year, genre, count FROM genre_year WHERE tier=?",
        (tier,),
    ).fetchall()
    totals: Counter[str] = Counter()
    by_year: dict[int, Counter[str]] = defaultdict(Counter)
    for row in rows:
        year, genre, count = int(row["release_year"]), str(row["genre"]), int(row["count"])
        totals[genre] += count
        by_year[year][genre] += count
    if not by_year:
        return {"legend": [], "years": [], "dated_tracks": 0}

    top = sorted(totals, key=lambda genre: (-totals[genre], genre.casefold(), genre))[:top_n]
    top_set = set(top)
    other_count = sum(count for genre, count in totals.items() if genre not in top_set)
    legend = [
        {"key": genre, "label": genre, "count": totals[genre], "color": _COLORS[index]}
        for index, genre in enumerate(top)
    ]
    if other_count:
        legend.append({"key": None, "label": "Other genres", "count": other_count, "color": "#7e8798"})

    years = []
    for year in range(min(by_year), max(by_year) + 1):
        counts = by_year[year]
        total = sum(counts.values())
        segments = []
        for entry in legend:
            count = (
                counts[entry["key"]]
                if entry["key"] is not None
                else sum(value for genre, value in counts.items() if genre not in top_set)
            )
            if count:
                segments.append({
                    "label": entry["label"],
                    "color": entry["color"],
                    "count": count,
                    "percent": round(100 * count / total, 3),
                })
        years.append({
            "year": year,
            "total": total,
            "segments": segments,
            "description": (
                f"{year}: " + ", ".join(f"{segment['label']} {segment['count']}" for segment in segments)
                if total else f"{year}: no dated tracks"
            ),
        })
    return {"legend": legend, "years": years, "dated_tracks": sum(totals.values())}


def load_insights(snapshot_path: Path, *, tier: int = 1, top_n: int = 6) -> dict[str, Any] | None:
    """Return snapshot-only aggregates, or None when no snapshot has been built."""
    if tier not in (1, 2) or not 1 <= top_n <= len(_COLORS):
        raise ValueError("Invalid genre chart options")
    if not snapshot_path.exists():
        return None
    if not snapshot_path.is_file():
        raise ValueError(f"Analytics snapshot is not a file: {snapshot_path}")
    uri = snapshot_path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            metadata = _read_metadata(conn)
            row = conn.execute(
                "SELECT tracks, artists, albums, duration_ms, duration_tracks, bytes, sized_tracks, errors "
                "FROM summary"
            ).fetchone()
            if row is None:
                raise ValueError("Analytics snapshot is missing its summary; rebuild the snapshot")
            total = int(row["tracks"])
            if metadata["unique_tracks"] != total:
                raise ValueError("Analytics snapshot track count does not match its metadata; rebuild the snapshot")
            schemas = [
                {"version": item["version"], "count": item["count"], "errors": item["errors"]}
                for item in conn.execute(
                    "SELECT version, count, errors FROM schema_counts ORDER BY version DESC"
                )
            ]
            timeline = _timeline(conn, tier=tier, top_n=top_n)
            return {
                "metadata": metadata,
                "tracks": total,
                "artists": int(row["artists"]),
                "albums": int(row["albums"]),
                "duration_ms": int(row["duration_ms"] or 0),
                "duration_tracks": int(row["duration_tracks"] or 0),
                "bytes": int(row["bytes"] or 0),
                "sized_tracks": int(row["sized_tracks"] or 0),
                "errors": int(row["errors"] or 0),
                "schemas": schemas,
                "genres": _counts(conn, "genre_tier1", total=total),
                "formats": _counts(conn, "file_format", total=total),
                "timeline": timeline,
                "undated_tracks": total - timeline["dated_tracks"],
            }
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read analytics snapshot: {exc}") from exc

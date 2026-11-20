"""Aggregate musical features from a published analytics snapshot."""

from __future__ import annotations

from bisect import bisect_right
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from cratedigger.web.insights import read_snapshot_metadata
from lib.descriptors import DESCRIPTOR_RECIPE
from lib.recommender import PITCH_CLASSES

_TEMPO_BOUNDS = (60, 80, 100, 120, 140, 160, 180, 200)
_TEMPO_LABELS = ("<60", "60 to <80", "80 to <100", "100 to <120", "120 to <140",
                 "140 to <160", "160 to <180", "180 to <200", "200+")
_LOUDNESS_BOUNDS = (-30, -25, -20, -15, -10, -5)
_LOUDNESS_LABELS = ("<-30", "-30 to <-25", "-25 to <-20", "-20 to <-15",
                    "-15 to <-10", "-10 to <-5", "-5+")
_FIFTHS = tuple(PITCH_CLASSES[(7 * index) % 12] for index in range(12))


def _number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Analytics snapshot has an invalid {field} value")
    return float(value)


def _text(value: Any, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Analytics snapshot has an invalid {field} value")
    return value


def _histogram(values: list[float], total: int, bounds: tuple[int, ...], labels: tuple[str, ...]) -> dict[str, Any]:
    counts = [0] * len(labels)
    for value in values:
        counts[bisect_right(bounds, value)] += 1
    largest = max(counts, default=0)
    bins = []
    for label, count in zip(labels, counts):
        percent = 100 * count / len(values) if values else 0.0
        if 0 < percent < 0.1:
            percent_label = "<0.1%"
        elif percent < 100 and round(percent, 1) == 100:
            percent_label = ">99.9%"
        else:
            percent_label = f"{percent:.1f}%"
        bins.append({
            "label": label,
            "count": count,
            "percent": round(percent, 1),
            "percent_label": percent_label,
            "width": round(100 * count / largest, 1) if largest else 0,
        })
    return {"known": len(values), "excluded": total - len(values), "bins": bins}


def _key_distribution(counts: dict[str, dict[str, int]], total: int) -> dict[str, Any]:
    known = sum(sum(modes.values()) for modes in counts.values())
    largest = max((sum(modes.values()) for modes in counts.values()), default=0)
    rows = []
    for key in _FIFTHS:
        modes = counts[key]
        rows.append({
            "key": key,
            **modes,
            "major_width": round(100 * modes["major"] / largest, 2) if largest else 0,
            "minor_width": round(100 * modes["minor"] / largest, 2) if largest else 0,
            "unknown_width": round(100 * modes["unknown"] / largest, 2) if largest else 0,
        })
    return {"known": known, "excluded": total - known, "rows": rows}


def _heatmap_style(count: int, largest: int) -> tuple[float, str]:
    opacity = round(0.12 + 0.78 * count / largest, 3) if count and largest else 0
    return opacity, "#000" if opacity >= 0.66 else "#fff"


def _mood_grid(conn: sqlite3.Connection, metadata: dict[str, Any], total: int) -> dict[str, Any]:
    if metadata["snapshot_version"] < 3:
        return {"available": False}
    counts = [[0] * 10 for _ in range(10)]
    seen = known = 0
    for row in conn.execute(
        "SELECT v.energy, v.valence FROM similarity_vectors AS v "
        "JOIN tracks AS t ON t.audio_hash=v.audio_hash"
    ):
        seen += 1
        energy = _number(row["energy"], "energy")
        valence = _number(row["valence"], "valence")
        if energy is None or valence is None:
            continue
        energy_score = 0.5 * (1 + math.tanh(energy / 2))
        valence_score = 0.5 * (1 + math.tanh(valence / 2))
        energy_bin = min(int(energy_score * 10), 9)
        valence_bin = min(int(valence_score * 10), 9)
        counts[energy_bin][valence_bin] += 1
        known += 1
    if seen != metadata["similarity_tracks"]:
        raise ValueError("Analytics snapshot similarity counts do not match its contents")
    largest = max((max(row) for row in counts), default=0)
    labels = [f"{index / 10:.1f}-{(index + 1) / 10:.1f}" for index in range(10)]
    rows = []
    for energy_bin in range(9, -1, -1):
        cells = []
        for valence_bin, count in enumerate(counts[energy_bin]):
            opacity, text_color = _heatmap_style(count, largest)
            cells.append({
                "count": count,
                "opacity": opacity,
                "text_color": text_color,
                "label": f"Energy {labels[energy_bin]}, valence {labels[valence_bin]}: "
                         f"{count} {'track' if count == 1 else 'tracks'}",
            })
        rows.append({"label": labels[energy_bin], "cells": cells})
    return {
        "available": True,
        "current_recipe": metadata["descriptor_recipe"] == DESCRIPTOR_RECIPE,
        "known": known,
        "eligible": seen,
        "missing_from_eligible": seen - known,
        "excluded": total - known,
        "labels": labels,
        "rows": rows,
    }


def load_sound_profile(snapshot_path: Path) -> dict[str, Any] | None:
    """Return musical distributions without reading or changing the feature store."""
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
            tempo: list[float] = []
            loudness: list[float] = []
            keys = {key: {"major": 0, "minor": 0, "unknown": 0} for key in _FIFTHS}
            total = 0
            for row in conn.execute("SELECT bpm, musical_key, mode, loudness_lufs FROM tracks"):
                total += 1
                bpm = _number(row["bpm"], "tempo")
                if bpm is not None and bpm > 0:
                    tempo.append(bpm)
                lufs = _number(row["loudness_lufs"], "loudness")
                if lufs is not None:
                    loudness.append(lufs)
                key = _text(row["musical_key"], "key")
                mode = _text(row["mode"], "mode")
                if key in keys:
                    normalized_mode = mode.strip().casefold() if mode else ""
                    bucket = normalized_mode if normalized_mode in {"major", "minor"} else "unknown"
                    keys[key][bucket] += 1
            if total != metadata["unique_tracks"]:
                raise ValueError("Analytics snapshot track count does not match its metadata")
            return {
                "metadata": metadata,
                "tracks": total,
                "tempo": _histogram(tempo, total, _TEMPO_BOUNDS, _TEMPO_LABELS),
                "loudness": _histogram(loudness, total, _LOUDNESS_BOUNDS, _LOUDNESS_LABELS),
                "musical_keys": _key_distribution(keys, total),
                "mood": _mood_grid(conn, metadata, total),
            }
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read analytics snapshot: {exc}") from exc

"""Preview, validate, and atomically publish player feature exports."""

from __future__ import annotations

from datetime import datetime
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Callable

import pandas as pd

from cratedigger.analytics import source_file_state
from cratedigger.ops.registry import REPOSITORY_ROOT
from lib.descriptors import DESCRIPTOR_RECIPE, DESCRIPTOR_SCALE
from lib.recommender import (
    DEFAULT_WEIGHTS,
    EXPORT_DESCRIPTOR_FIELDS,
    EXPORT_LEVEL_FIELDS,
    EXPORT_REGRESSION_FIELDS,
    RECIPE_VERSION,
    export_features,
)

DEFAULT_EXPORT_PATH = REPOSITORY_ROOT / "features_export.json.gz"
FileState = tuple[int, int, int, int]


def export_output_path(value: str | Path | None, repo_root: Path) -> Path:
    """Restrict UI exports to the standard destination or reports/exports."""
    root = repo_root.resolve()
    path = Path(value) if value else root / "features_export.json.gz"
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if path != root / "features_export.json.gz":
        reports = (root / "reports").resolve()
        exports = (reports / "exports").resolve()
        if not exports.is_relative_to(reports):
            raise ValueError("The exports directory must stay inside the reports directory")
        if not path.is_relative_to(exports):
            raise ValueError("Export output must be features_export.json.gz or a file under reports/exports")
    if not (path.name.endswith(".json") or path.name.endswith(".json.gz")):
        raise ValueError("Export output must end in .json or .json.gz")
    return path


def output_file_state(path: Path) -> FileState | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Export destination is not a regular file: {path}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_player_export(payload: dict[str, Any]) -> dict[str, Any]:
    """Check the current player contract before serialization and publication."""
    if payload.get("model_version") != RECIPE_VERSION:
        raise ValueError("Export has an unexpected model version")
    if payload.get("descriptor_recipe") != DESCRIPTOR_RECIPE or payload.get("descriptor_scale") != DESCRIPTOR_SCALE:
        raise ValueError("Export has an unexpected descriptor recipe or scale")
    created = payload.get("created_at")
    try:
        timestamp = datetime.fromisoformat(created) if isinstance(created, str) else None
    except ValueError as exc:
        raise ValueError("Export has an invalid creation timestamp") from exc
    if timestamp is None or timestamp.utcoffset() is None:
        raise ValueError("Export has an invalid creation timestamp")
    tracks = payload.get("tracks")
    count = payload.get("track_count")
    excluded = payload.get("excluded_count")
    if (
        not isinstance(tracks, list)
        or type(count) is not int
        or count != len(tracks)
        or type(excluded) is not int
        or excluded < 0
    ):
        raise ValueError("Export has invalid track or exclusion counts")
    dims = payload.get("dims")
    if type(dims) is not int or dims != (410 if count else 0):
        raise ValueError("Export has an invalid player vector dimension")
    weights = payload.get("weights")
    if (
        not isinstance(weights, dict)
        or set(weights) != set(DEFAULT_WEIGHTS)
        or any(not _number(value) or value < 0 for value in weights.values())
        or not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6)
    ):
        raise ValueError("Export has invalid similarity weights")
    numeric_fields = ("bpm", *EXPORT_DESCRIPTOR_FIELDS, *EXPORT_REGRESSION_FIELDS, *EXPORT_LEVEL_FIELDS)
    seen: set[str] = set()
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            raise ValueError(f"Export track {index} is not an object")
        identity = track.get("audio_hash")
        if not isinstance(identity, str) or not identity.strip() or identity in seen:
            raise ValueError(f"Export track {index} has a missing or duplicate audio_hash")
        seen.add(identity)
        vector = track.get("vector")
        if not isinstance(vector, list) or len(vector) != dims or any(not _number(value) for value in vector):
            raise ValueError(f"Export track {index} has an invalid similarity vector")
        for field in numeric_fields:
            if field not in track or (track[field] is not None and not _number(track[field])):
                raise ValueError(f"Export track {index} has an invalid {field}")
        for field in ("key", "mode", "artist", "album", "genre"):
            if field in track and track[field] is not None and not isinstance(track[field], str):
                raise ValueError(f"Export track {index} has an invalid {field}")
        if "key" not in track or "mode" not in track:
            raise ValueError(f"Export track {index} is missing key or mode")
        if "truncated" in track and track["truncated"] is not True:
            raise ValueError(f"Export track {index} has an invalid truncated flag")
    return {
        "model_version": payload["model_version"],
        "descriptor_recipe": payload["descriptor_recipe"],
        "descriptor_scale": payload["descriptor_scale"],
        "created_at": created,
        "track_count": count,
        "excluded_count": excluded,
        "dims": dims,
    }


def build_player_export(
    features_path: Path,
    output_path: Path,
    *,
    dry_run: bool = True,
    expected_source_state: FileState | None = None,
    expected_output_state: FileState | None = None,
    check_output_state: bool = False,
    extraction_running: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build a temporary validated export and publish it only on an explicit apply."""
    source = features_path.resolve()
    output = output_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Feature store does not exist: {source}")
    if source == output or (output.exists() and os.path.samefile(source, output)):
        raise ValueError("The player export must not overwrite the feature store")
    if not (output.name.endswith(".json") or output.name.endswith(".json.gz")):
        raise ValueError("Export output must end in .json or .json.gz")
    if extraction_running is not None and extraction_running():
        raise RuntimeError("Feature extraction is running; wait for it before exporting")
    source_state = source_file_state(source)
    if expected_source_state is not None and source_state != expected_source_state:
        raise RuntimeError("Feature store changed after the job was queued; no export was written")
    destination_state = output_file_state(output)
    if check_output_state and destination_state != expected_output_state:
        raise RuntimeError("Export destination changed after the job was queued; no export was written")

    frame = pd.read_pickle(source)
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Feature store does not contain a pandas DataFrame")
    if source_file_state(source) != source_state:
        raise RuntimeError("Feature store changed while reading; no export was written")

    if not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".player-export-", dir=None if dry_run else output.parent) as temp_dir:
        staged = Path(temp_dir) / output.name
        metadata: dict[str, Any] = {}

        def validate(payload: dict[str, Any]) -> None:
            metadata.update(validate_player_export(payload))

        export_features(frame, staged, validator=validate)
        if not metadata:
            raise RuntimeError("The player export was not validated")
        size = staged.stat().st_size
        if source_file_state(source) != source_state:
            raise RuntimeError("Feature store changed during export; no export was published")
        if output_file_state(output) != destination_state:
            raise RuntimeError("Export destination changed during export; no export was published")
        if extraction_running is not None and extraction_running():
            raise RuntimeError("Feature extraction started during export; no export was published")
        published_state = None
        if not dry_run:
            if destination_state is not None:
                staged.chmod(stat.S_IMODE(output.stat().st_mode))
            if output_file_state(output) != destination_state or source_file_state(source) != source_state:
                raise RuntimeError("Export input or destination changed before publication; no export was published")
            staged.replace(output)
            published_state = output_file_state(output)
            if published_state is None:
                raise RuntimeError("The export was published but is no longer available")
    return {
        **metadata,
        "validated": True,
        "source": str(source),
        "source_state": source_state,
        "output": str(output),
        "output_state": published_state,
        "size_bytes": size,
        "dry_run": dry_run,
    }

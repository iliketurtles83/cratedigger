"""Inspect the last validated export without reading the feature pickle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cratedigger.analytics import source_file_state
from cratedigger.feature_export import export_output_path, output_file_state
from cratedigger.job_logs import read_log_tail
from lib.descriptors import DESCRIPTOR_RECIPE, DESCRIPTOR_SCALE
from lib.recommender import RECIPE_VERSION

_MARKER = "@@export-result "


def _file_state(value: Any) -> tuple[int, int, int, int]:
    if not isinstance(value, list) or len(value) != 4 or any(type(item) is not int or item < 0 for item in value):
        raise ValueError("Invalid recorded file identity")
    return value[0], value[1], value[2], value[3]


def _read_summary(log_path: Path, logs_dir: Path) -> dict[str, Any]:
    for line in reversed(read_log_tail(log_path, logs_dir)):
        if line.startswith(_MARKER):
            payload = json.loads(line[len(_MARKER):])
            if not isinstance(payload, dict):
                raise ValueError("Invalid export result in job log")
            return payload
    raise ValueError("Validated export result is missing from the job log")


def export_job_status(
    job: dict[str, Any], logs_dir: Path, repo_root: Path, configured_features: Path
) -> dict[str, Any] | None:
    if job.get("operation_id") != "recommend-export" or job.get("mode") != "apply" or job.get("status") != "succeeded":
        return None
    info: dict[str, Any] = {
        "job_id": str(job["id"]),
        "state": "unknown",
        "message": "",
        "available": False,
    }
    try:
        params = job["params"]
        argv = job["argv"]
        if not isinstance(params, dict) or not isinstance(argv, list):
            raise ValueError("Invalid saved export job parameters")
        source = Path(params["features"]).resolve()
        output = export_output_path(params["output"], repo_root)
        info["output"] = str(output)
        current_output = output_file_state(output)
        try:
            current_source = source_file_state(source)
        except FileNotFoundError:
            current_source = None
        if current_output is not None and current_source is not None and current_output[:2] == current_source[:2]:
            raise ValueError("The export destination is the feature store")
        info["available"] = current_output is not None
        summary = _read_summary(Path(job["log_path"]), logs_dir)
        if (
            summary.get("validated") is not True
            or summary.get("dry_run") is not False
            or summary.get("source") != str(source)
            or summary.get("output") != str(output)
        ):
            raise ValueError("Job log does not match the published export")
        source_state = _file_state(summary.get("source_state"))
        published_state = _file_state(summary.get("output_state"))
        try:
            expected_source = _file_state(json.loads(argv[argv.index("--expected-source-state") + 1]))
        except (ValueError, IndexError, TypeError) as exc:
            raise ValueError("The job has no valid recorded feature-store identity") from exc
        if source_state != expected_source:
            raise ValueError("Job source identity does not match its export result")
        configured_source = configured_features if configured_features.is_absolute() else repo_root / configured_features
        if current_output is None:
            info.update(state="changed", message="The published output is missing.")
        elif current_output != published_state:
            info.update(state="changed", message="The output changed since this job; its current contents have not been validated.")
        elif current_source is None:
            info.update(state="changed", message="The feature store used by this export is missing.")
        elif current_source != source_state:
            info.update(state="changed", message="The feature store changed since this export; build a new export.")
        elif configured_source.resolve() != source:
            info.update(state="changed", message="This export used a different feature store from the one configured in the UI.")
        elif (
            summary.get("model_version") != RECIPE_VERSION
            or summary.get("descriptor_recipe") != DESCRIPTOR_RECIPE
            or summary.get("descriptor_scale") != DESCRIPTOR_SCALE
        ):
            info.update(state="changed", message="The player export recipe changed; build a new export.")
        else:
            info.update(state="current", message="The validated output and its feature store have not changed since publication.")
        info["track_count"] = summary.get("track_count")
        info["excluded_count"] = summary.get("excluded_count")
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RuntimeError) as exc:
        info["message"] = f"Cannot verify export freshness: {exc}"
    return info

"""Preview and atomically publish recommender feature-store backfills."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Callable, Literal

import pandas as pd

from cratedigger.analytics import source_file_state
from cratedigger.job_logs import read_log_tail
from cratedigger.ops.registry import REPOSITORY_ROOT
from lib.recommend_backfill import embedding_candidates, missing_hash_positions

BackfillKind = Literal["hashes", "embeddings"]
FileState = tuple[int, int, int, int]
_MARKER = "@@backfill-plan "


@dataclass(frozen=True)
class _CandidatePlan:
    indices: tuple[Any, ...]
    entries: tuple[tuple[str, str, FileState | None], ...]
    available: int
    skipped: int
    source_state: FileState
    digest: str


def _track_path(value: Any) -> str:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ValueError("A backfill candidate has an invalid track path")
    return os.fsdecode(value)


def _track_state(value: str) -> FileState | None:
    path = Path(value)
    if not path.is_absolute():
        path = REPOSITORY_ROOT / path
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Backfill track path is not a regular file: {value}")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _digest_entries(
    kind: BackfillKind, max_embeddings: int | None, available: int, source_state: FileState,
    entries: tuple[tuple[str, str, FileState | None], ...],
) -> str:
    digest = hashlib.sha256()
    header = json.dumps([kind, max_embeddings, available, source_state], separators=(",", ":")).encode("utf-8")
    digest.update(len(header).to_bytes(8, "big"))
    digest.update(header)
    for entry in entries:
        encoded = json.dumps(entry, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _inspect_candidates(
    frame: pd.DataFrame, kind: BackfillKind, max_embeddings: int | None, source_state: FileState,
) -> _CandidatePlan:
    if "path" not in frame.columns:
        raise ValueError("Feature store is missing the path column")
    if kind == "hashes":
        indices = missing_hash_positions(frame)
        paths = [_track_path(frame["path"].iloc[index]) for index in indices]
        entries = tuple(("selected", path, _track_state(path)) for path in paths)
        skipped = sum(state is None for _role, _path, state in entries)
        available = len(indices) - skipped
    else:
        if not frame.index.is_unique:
            raise ValueError("Embedding backfill requires unique feature-store row indices")
        wanted, missing = embedding_candidates(frame, base_dir=REPOSITORY_ROOT)
        indices = wanted[:max_embeddings] if max_embeddings is not None else wanted
        selected = [_track_path(frame.at[index, "path"]) for index in indices]
        unavailable = [_track_path(frame.at[index, "path"]) for index in missing]
        entries = tuple(
            (role, path, _track_state(path))
            for role, paths in (("selected", selected), ("skipped", unavailable))
            for path in paths
        )
        if any((role == "selected") != (state is not None) for role, _path, state in entries):
            raise RuntimeError("Track availability changed during candidate inspection; retry the preview")
        available = len(wanted)
        skipped = len(missing)
    return _CandidatePlan(
        tuple(indices), entries, available, skipped, source_state,
        _digest_entries(kind, max_embeddings, available, source_state, entries),
    )


def _current_digest(plan: _CandidatePlan, kind: BackfillKind, max_embeddings: int | None) -> str:
    entries = tuple((role, path, _track_state(path)) for role, path, _state in plan.entries)
    return _digest_entries(kind, max_embeddings, plan.available, plan.source_state, entries)


def reviewed_backfill_plan(
    log_path: Path, logs_dir: Path, source: Path, kind: BackfillKind, max_embeddings: int | None,
) -> str:
    for line in reversed(read_log_tail(log_path, logs_dir)):
        if not line.startswith(_MARKER):
            continue
        try:
            result = json.loads(line[len(_MARKER):])
        except json.JSONDecodeError as exc:
            raise ValueError("Backfill preview log has an invalid recorded plan") from exc
        if (
            not isinstance(result, dict)
            or result.get("source") != str(source)
            or result.get("kind") != kind
            or result.get("max_embeddings") != max_embeddings
            or not isinstance(result.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", result["sha256"])
        ):
            raise ValueError("Backfill preview log does not match the selected operation and feature store")
        return result["sha256"]
    raise ValueError("Backfill preview has no recorded plan; run a fresh preview")


def run_guarded_backfill(
    features_path: Path,
    kind: BackfillKind,
    *,
    max_embeddings: int | None = None,
    dry_run: bool = True,
    expected_plan: str | None = None,
    expected_source_state: FileState | None = None,
    extraction_running: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Inspect candidates, then run the legacy backfill on a staged store and publish atomically."""
    if kind not in {"hashes", "embeddings"}:
        raise ValueError("Backfill kind must be hashes or embeddings")
    if max_embeddings is not None and (kind != "embeddings" or type(max_embeddings) is not int or max_embeddings < 1):
        raise ValueError("Maximum embeddings must be a positive integer for an embedding backfill")
    if not dry_run and (not isinstance(expected_plan, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_plan)):
        raise ValueError("A successful reviewed backfill preview is required before applying")
    source = features_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Feature store does not exist: {source}")

    def check_extraction() -> None:
        if extraction_running is not None and extraction_running():
            raise RuntimeError("Feature extraction is running or a standalone backfill is running; wait before backfilling")

    check_extraction()
    source_state = source_file_state(source)
    if expected_source_state is not None and source_state != expected_source_state:
        raise RuntimeError("Feature store changed after the job was queued; no backfill was written")
    frame = pd.read_pickle(source)
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Feature store does not contain a pandas DataFrame")
    plan = _inspect_candidates(frame, kind, max_embeddings, source_state)
    row_count = len(frame)
    del frame
    if source_file_state(source) != source_state or _current_digest(plan, kind, max_embeddings) != plan.digest:
        raise RuntimeError("Feature store or track files changed during preview; no backfill was written")
    check_extraction()

    selected_count = len(plan.indices)
    print(
        f"Backfill {kind}: {selected_count} selected, {plan.available} available, "
        f"{plan.skipped} unavailable and skipped. Preview does not compute features.",
        flush=True,
    )
    for role, path, state in plan.entries[:20]:
        print(f"  {role}: {json.dumps(path, ensure_ascii=True)}{' (unavailable)' if state is None else ''}", flush=True)
    if len(plan.entries) > 20:
        print(f"  ... {len(plan.entries) - 20} more paths omitted from the log.", flush=True)
    summary: dict[str, Any] = {
        "kind": kind, "source": str(source), "max_embeddings": max_embeddings,
        "sha256": plan.digest, "selected": selected_count, "available": plan.available,
        "skipped": plan.skipped, "updated": 0, "published": False, "dry_run": dry_run,
    }
    if dry_run:
        print(_MARKER + json.dumps(summary, ensure_ascii=True, separators=(",", ":")), flush=True)
        return summary
    if plan.digest != expected_plan:
        raise RuntimeError("The backfill plan changed since preview; no feature store was written")
    if not selected_count or (kind == "hashes" and not plan.available):
        print("No available tracks require this backfill; the feature store was not replaced.", flush=True)
        return summary

    with tempfile.TemporaryDirectory(prefix=".feature-backfill-", dir=source.parent) as temp_dir:
        staged = Path(temp_dir) / source.name
        shutil.copyfile(source, staged)
        if _current_digest(plan, kind, max_embeddings) != plan.digest or source_file_state(source) != source_state:
            raise RuntimeError("Feature store or track files changed while staging; no backfill was published")
        check_extraction()
        command = [
            sys.executable, str(REPOSITORY_ROOT / "07_recommend.py"),
            "--store", str(staged), f"--backfill-{kind}", "--strict-backfill",
        ]
        if max_embeddings is not None:
            command.extend(("--max-embeddings", str(max_embeddings)))
        try:
            subprocess.run(command, cwd=REPOSITORY_ROOT, stdin=subprocess.DEVNULL, check=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Backfill failed; the original feature store was not changed") from exc
        result = pd.read_pickle(staged)
        if not isinstance(result, pd.DataFrame) or len(result) != row_count:
            raise RuntimeError("Backfill produced an invalid feature store; no changes were published")
        if kind == "hashes":
            remaining = set(missing_hash_positions(result))
            updated = sum(index not in remaining for index in plan.indices)
            if any(index in remaining and state is not None for index, (_, _, state) in zip(plan.indices, plan.entries)):
                raise RuntimeError("Backfill left an available track unhashed; no changes were published")
        else:
            remaining, _missing = embedding_candidates(result, base_dir=REPOSITORY_ROOT)
            if set(plan.indices).intersection(remaining):
                raise RuntimeError("Backfill left a selected embedding incomplete; no changes were published")
            updated = selected_count
        del result
        if _current_digest(plan, kind, max_embeddings) != plan.digest or source_file_state(source) != source_state:
            raise RuntimeError("Feature store or track files changed during backfill; no changes were published")
        check_extraction()
        if updated:
            staged.chmod(stat.S_IMODE(source.stat().st_mode))
            if _current_digest(plan, kind, max_embeddings) != plan.digest or source_file_state(source) != source_state:
                raise RuntimeError("Backfill inputs changed before publication; no changes were published")
            check_extraction()
            if source_file_state(source) != source_state:
                raise RuntimeError("Feature store changed before publication; no changes were published")
            staged.replace(source)
            summary["published"] = True
        summary["updated"] = updated
    print(
        f"Backfill complete: {summary['updated']} rows updated; "
        f"{summary['skipped']} unavailable paths skipped. Published: {summary['published']}.",
        flush=True,
    )
    return summary

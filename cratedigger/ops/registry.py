"""Typed, allowlisted command registry for the operations UI."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
import sys
from typing import Any, Literal

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

FieldKind = Literal["text", "integer", "number", "boolean", "choice"]
Risk = Literal["read_only", "writes_tags", "moves_files", "writes_library", "writes_features", "writes_analytics", "network"]


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    kind: FieldKind = "text"
    flag: str | None = None
    default: str | int | float | bool | None = None
    help: str = ""
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationSpec:
    id: str
    title: str
    group: str
    script: str
    risk: Risk
    fields: tuple[ParamSpec, ...] = ()
    fixed_args: tuple[str, ...] = ()
    locks: tuple[str, ...] = ()
    preview_flag: bool = True
    live_flags: tuple[str, ...] = ("--no-dry-run",)
    apply_requires: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    help: str = ""
    legacy: bool = True

    @property
    def can_apply(self) -> bool:
        return self.risk != "read_only"


def _text(name: str, label: str, flag: str | None = None, help: str = "") -> ParamSpec:
    return ParamSpec(name, label, "text", flag or f"--{name.replace('_', '-')}", help=help)


def _int(
    name: str,
    label: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> ParamSpec:
    return ParamSpec(name, label, "integer", f"--{name.replace('_', '-')}", default, minimum=minimum, maximum=maximum)


def _number(
    name: str,
    label: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> ParamSpec:
    return ParamSpec(name, label, "number", f"--{name.replace('_', '-')}", default, minimum=minimum, maximum=maximum)


def _bool(
    name: str, label: str, flag: str | None = None, default: bool = False, help: str = ""
) -> ParamSpec:
    return ParamSpec(name, label, "boolean", flag or f"--{name.replace('_', '-')}", default, help=help)


def _choice(name: str, label: str, choices: tuple[str, ...]) -> ParamSpec:
    return ParamSpec(name, label, "choice", None, choices=choices)


_FOLDER = _text("folder", "Folder scope", help="Folder name or path relative to the configured library root.")
_REPORT = _text("report", "Reviewed report path", help="Path to a reviewed report file.")
_LIBRARY_ROOT = _text("library_root", "Library root override", help="Optional library root override.")
_FEATURES = _text("features", "Feature store", "--features")

_OPS = (
    OperationSpec("intake", "Process incoming music", "Intake", "intake.py", "moves_files",
                  (_bool("overwrite", "Overwrite existing tags"), _bool("fix_suspicious", "Fix suspicious tags"),
                   _bool("legacy_move", "Use legacy staging flow"), _bool("pre_tag", "Tag before beets import"),
                   _bool("no_enrich", "Skip artwork enrichment")),
                  locks=("music_root:write", "mb_network", "beets"), help="Preview or process incoming releases."),
    OperationSpec("tag", "Tag audio files", "Tagging", "01_tag.py", "writes_tags",
                  (_FOLDER, _bool("overwrite", "Overwrite existing tags"), _bool("fix_suspicious", "Fix suspicious tags"),
                   _bool("fix_track_mismatch", "Fix track mismatches"), _bool("no_bpm", "Skip BPM lookup"),
                   _bool("no_mb", "Skip MusicBrainz"), _bool("no_consistency", "Skip consistency checks"),
                   _bool("remap_genres", "Remap genres")),
                  locks=("music_root:write", "mb_network"), help="Tag selected library files."),
    OperationSpec("rename", "Normalize filenames", "Layout", "02_rename.py", "moves_files", (_FOLDER,),
                  locks=("music_root:write",), help="Rename files from their tags."),
    OperationSpec("folders", "Normalize album folders", "Layout", "03_folders.py", "moves_files", (_FOLDER,),
                  locks=("music_root:write",), help="Normalize album folder names."),
    OperationSpec("route", "Route and promote music", "Layout", "04_move.py", "moves_files",
                  (_choice("route_mode", "Route mode", ("intake", "restructure", "promote", "to-artist-root", "relocate-compilations")),
                   _FOLDER),
                  locks=("music_root:write",), required=("route_mode",),
                  help="Run one selected 04_move operation mode."),
    OperationSpec("analyze", "Extract audio features", "Analysis", "06_analyze.py", "writes_features",
                  (_FOLDER, _text("output", "Feature store output"), _bool("force", "Re-analyze all tracks"),
                   _int("workers", "Workers", minimum=1, maximum=32),
                   _number("checkpoint_seconds", "Checkpoint interval (seconds)", minimum=10, maximum=86400),
                   _int("model_threads", "Model threads", minimum=1, maximum=32)),
                  locks=("features_store", "music_root:read"), preview_flag=True, live_flags=(),
                  help="Incrementally extract audio features; the preview performs a dry run."),
    OperationSpec("analytics-snapshot", "Build insights snapshot", "Analysis", "scripts/build_analytics.py", "writes_analytics",
                  (_FEATURES,), locks=("features_store", "analytics_store"),
                  help="Create a derived SQLite snapshot without changing the feature store. Wait for extraction to finish.",
                  legacy=False),
    OperationSpec("recommend-evaluate", "Evaluate recommender quality", "Analysis", "07_recommend.py", "read_only",
                  (_text("store", "Feature store"),), fixed_args=("--evaluate",), preview_flag=False,
                  locks=("features_store",), help="Run the holdout evaluation without changing the feature store."),
    OperationSpec("recommend-tune", "Tune recommender weights", "Analysis", "07_recommend.py", "read_only",
                  (_text("store", "Feature store"),), fixed_args=("--tune-weights",), preview_flag=False,
                  locks=("features_store",), help="Evaluate candidate similarity weights."),
    OperationSpec("recommend-clusters", "Inspect recommendation clusters", "Analysis", "07_recommend.py", "read_only",
                  (_text("store", "Feature store"), _bool("all", "Use the whole library"),
                   _int("n_clusters", "Cluster count", minimum=2, maximum=1000)),
                  fixed_args=("--clusters",), preview_flag=False, locks=("features_store",)),
    OperationSpec("recommend-query", "Find similar tracks", "Analysis", "07_recommend.py", "read_only",
                  (_text("store", "Feature store"), _text("seed", "Seed track query"),
                   _int("top_n", "Number of results", default=5, minimum=1, maximum=1000)),
                  preview_flag=False, locks=("features_store",), required=("seed",),
                  help="Search for tracks similar to a seed query."),
    OperationSpec("audit-beets", "Audit Beets matches", "Metadata", "scripts/audit_beets_matches.py", "read_only",
                  (_text("bucket", "Letter bucket"), _FOLDER, _bool("online", "Query MusicBrainz"),
                   _text("report", "Report output path")), locks=("music_root:read", "mb_network"), preview_flag=False,
                  live_flags=(), help="Audit unmatched releases; online requests are rate limited."),
    OperationSpec("apply-beets", "Apply reviewed Beets evidence", "Metadata", "scripts/apply_beets_matches.py", "writes_tags",
                  (_REPORT, _text("release_id", "MusicBrainz release ID"), _text("result", "Result path")),
                  locks=("music_root:write",), preview_flag=False, live_flags=("--fix", "--no-dry-run"),
                  apply_requires=("report", "release_id"), required=("report", "release_id"),
                  help="Apply a specific reviewed match."),
    OperationSpec("audit-repair", "Audit and repair tags", "Repair", "scripts/audit_repair.py", "writes_tags",
                  (_FOLDER, _bool("fix", "Apply repairs"), _text("target_root", "Target root"),
                   _text("backup_root", "Backup root")), locks=("music_root:write",),
                  apply_requires=("fix",), help="Audit tags against a backup and optionally repair."),
    OperationSpec("consolidate", "Consolidate small releases", "Layout", "scripts/consolidate_small_releases.py", "moves_files",
                  (_FOLDER, _bool("fix", "Apply consolidation"), _bool("all", "Include all candidates"),
                   _int("threshold", "Track threshold", minimum=1, maximum=1000), _text("report", "Report output path")),
                  locks=("music_root:write",), apply_requires=("fix",), help="Consolidate reviewed small releases."),
    OperationSpec("convert-lists", "Convert curated lists to playlists", "Playlists", "scripts/convert_lists.py", "moves_files",
                  (_FOLDER, _bool("retag", "Retag tracks"),
                   _bool("remove_duplicates", "Remove duplicates", help="Permanently deletes duplicates; this is not undoable."),
                   _bool("all", "Process all lists"), _LIBRARY_ROOT,
                   _text("playlists_dir", "Playlists directory"), _text("playlist_name", "Playlist name")),
                  locks=("music_root:write",), help="Create playlists from curated list folders."),
    OperationSpec("enrich-covers", "Find and embed cover art", "Artwork", "scripts/enrich_covers.py", "writes_tags",
                  (_FOLDER, _text("bucket", "Letter bucket"), _text("buckets", "Buckets (comma separated)"),
                   _bool("all_buckets", "Process all buckets"), _bool("singles", "Process singles"),
                   _bool("albums_only", "Albums only"), _int("max_size", "Maximum image size", minimum=1, maximum=10000),
                   _bool("force_lossy_hash", "Allow whole-file hash changes",
                         help="Embedding art in Ogg, Opus, or WMA changes audio_hash and player identity."), _LIBRARY_ROOT),
                  locks=("music_root:write", "mb_network"), help="Fetch and normalize album artwork."),
    OperationSpec("genre-playlists", "Generate genre playlists", "Playlists", "scripts/generate_genre_playlists.py", "writes_library",
                  (_bool("fix", "Write playlists"), _int("min_tracks", "Minimum tracks", minimum=1, maximum=100000)),
                  locks=("music_root:write",), apply_requires=("fix",), help="Generate singles playlists by genre."),
    OperationSpec("migrate-judgement", "Migrate judgement folders", "Layout", "scripts/migrate_judgement_folders.py", "moves_files",
                  (_FOLDER, _bool("fix", "Apply migration")), locks=("music_root:write",), apply_requires=("fix",)),
    OperationSpec("normalize-covers", "Normalize cover filenames", "Artwork", "scripts/normalize_covers.py", "moves_files",
                  (_FOLDER, _LIBRARY_ROOT), locks=("music_root:write",)),
    OperationSpec("quarantine-duplicates", "Quarantine duplicate albums", "Duplicates", "scripts/quarantine_album_duplicates.py", "moves_files",
                  (_FOLDER, _bool("fix", "Quarantine duplicates"), _text("quarantine_dir", "Quarantine directory")),
                  locks=("music_root:write",), apply_requires=("fix",)),
    OperationSpec("reconcile-artists", "Reconcile artist folders", "Artists", "scripts/reconcile_artists.py", "moves_files",
                  (_bool("fix", "Apply reviewed changes"), _int("min_tracks", "Minimum tracks", minimum=1, maximum=10000),
                   _bool("online_release_evidence", "Fetch release evidence"), _bool("online_recording_evidence", "Fetch recording evidence"),
                   _bool("collision_report", "Produce collision report"), _bool("resolve_collisions", "Resolve reviewed collisions")),
                  locks=("music_root:write", "mb_network"), preview_flag=False, live_flags=("--no-dry-run",),
                  apply_requires=("fix",), help="Reconcile folders with evidence and reviewable reports."),
    OperationSpec("reconcile-genres", "Reconcile genre taxonomy", "Genres", "scripts/reconcile_genres.py", "writes_tags",
                  (_FOLDER, _bool("fix", "Apply reviewed genre changes"), _text("report", "Report output path")),
                  locks=("music_root:write",), apply_requires=("fix",)),
    OperationSpec("repair-bpm", "Repair BPM tags", "Repair", "scripts/repair_bpm.py", "writes_tags",
                  (_FOLDER, _LIBRARY_ROOT, _bool("suspect_only", "Only inspect suspect BPM values"), _FEATURES,
                   _text("apply_report", "Reviewed actions report"),
                   _text("actions", "Action selection", help="Space- or comma-separated BPM repair actions."),
                   _number("tolerance", "Tolerance", default=0.1, minimum=0, maximum=10),
                   _number("min_confidence", "Minimum confidence", default=1.2, minimum=0),
                   _bool("no_clear_beatless", "Keep beatless BPM values"), _bool("fill_missing", "Fill missing BPM"),
                   _bool("include_whole_hash", "Include whole-file-hashed tracks",
                         help="Retagging Ogg, Opus, or WMA changes audio_hash and player identity."),
                   _int("workers", "Workers", minimum=1, maximum=32),
                   _int("limit", "Maximum tracks", minimum=1, maximum=100000), _text("report", "Report output path")),
                  locks=("music_root:write", "features_store"), live_flags=("--fix", "--no-dry-run"),
                  help="Audit or repair BPM tags."),
    OperationSpec("repair-folder-parse", "Repair folder parsing", "Repair", "scripts/repair_folder_parse.py", "moves_files",
                  (_bool("tags", "Repair tags"), _bool("structure", "Repair structure"), _FOLDER),
                  locks=("music_root:write",), help="Repair folder-parse damage."),
    OperationSpec("repair-track-identity", "Repair track identity", "Repair", "scripts/repair_track_identity.py", "moves_files",
                  (_text("artist", "Artist filter"), _bool("online_fingerprints", "Fetch fingerprints"),
                   _number("min_score", "Minimum match score", default=0.8, minimum=0, maximum=1),
                   _bool("fix", "Apply reviewed repairs"), _bool("relocate", "Relocate repaired tracks"),
                   _bool("misattributions_only", "Only repair misattributions")),
                  locks=("music_root:write", "mb_network"), preview_flag=False, apply_requires=("fix",)),
    OperationSpec("repair-windows-paths", "Repair Windows paths", "Repair", "scripts/repair_windows_paths.py", "moves_files",
                  (_bool("fix", "Apply repairs"), _bool("names_only", "Normalize invalid names only")),
                  locks=("music_root:write",), preview_flag=False, apply_requires=("fix",)),
    OperationSpec("retag-lists", "Retag curated lists", "Playlists", "scripts/retag_lists.py", "writes_tags",
                  (_FOLDER, _bool("as_compilation", "Treat as compilation"), _text("album_title", "List album title"),
                   _bool("all", "Process all lists"), _LIBRARY_ROOT),
                  locks=("music_root:write",)),
)

OPERATIONS = {op.id: op for op in _OPS}


def _parse_value(spec: ParamSpec, value: Any) -> str | int | float | bool | None:
    if spec.kind == "boolean":
        if isinstance(value, bool):
            return value
        if value in (None, "", "0", "false", "False", "off"):
            return False
        if value in ("1", "true", "True", "on"):
            return True
        raise ValueError(f"{spec.label} must be a boolean")
    if value is None or value == "":
        return spec.default
    if spec.kind == "integer":
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.label} must be an integer") from exc
        if spec.minimum is not None and result < spec.minimum:
            raise ValueError(f"{spec.label} must be at least {spec.minimum}")
        if spec.maximum is not None and result > spec.maximum:
            raise ValueError(f"{spec.label} must be at most {spec.maximum}")
        return result
    if spec.kind == "number":
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{spec.label} must be a number") from exc
        if not math.isfinite(result):
            raise ValueError(f"{spec.label} must be a finite number")
        if spec.minimum is not None and result < spec.minimum:
            raise ValueError(f"{spec.label} must be at least {spec.minimum}")
        if spec.maximum is not None and result > spec.maximum:
            raise ValueError(f"{spec.label} must be at most {spec.maximum}")
        return result
    if spec.kind == "choice":
        if value is None or value == "":
            return spec.default
        result = str(value)
        if result not in spec.choices:
            raise ValueError(f"{spec.label} must be one of: {', '.join(spec.choices)}")
        return result
    return str(value).strip()


def normalize_params(
    op: OperationSpec, raw: dict[str, Any], *, mode: Literal["preview", "apply"] = "preview"
) -> dict[str, str | int | float | bool]:
    known = {field.name for field in op.fields}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")
    result: dict[str, str | int | float | bool] = {}
    for field in op.fields:
        value = _parse_value(field, raw.get(field.name, field.default))
        if value is not None:
            result[field.name] = value
    for required in op.required:
        if not result.get(required):
            raise ValueError(f"{required.replace('_', ' ').title()} is required")
    if mode == "apply":
        for required in op.apply_requires:
            if not result.get(required):
                raise ValueError(f"{required.replace('_', ' ').title()} is required to apply this operation")
    return result


def build_argv(
    op_id: str,
    raw: dict[str, Any],
    *,
    mode: Literal["preview", "apply"],
    repo_root: Path = REPOSITORY_ROOT,
    music_root: Path | None = None,
) -> list[str]:
    """Build argv exclusively from the registry; no shell or user-selected executable."""
    try:
        op = OPERATIONS[op_id]
    except KeyError as exc:
        raise ValueError(f"Unknown operation: {op_id}") from exc
    if mode == "apply" and not op.can_apply:
        raise ValueError(f"{op.title} is read-only and has no apply mode")
    params = normalize_params(op, raw, mode=mode)
    args = [sys.executable, str((repo_root / op.script).resolve())]
    args.extend(op.fixed_args)
    for field in op.fields:
        value = params.get(field.name)
        if value is None or field.flag is None:
            continue
        if field.kind == "boolean":
            if value:
                args.append(field.flag)
        elif op.id == "repair-bpm" and field.name == "actions":
            actions = [item for item in re.split(r"[\s,]+", str(value)) if item]
            allowed = {"replace_corrupt", "clear_corrupt", "clear_beatless", "update_mismatch", "fill_missing"}
            if not actions or any(action not in allowed for action in actions):
                raise ValueError(f"Actions must be selected from: {', '.join(sorted(allowed))}")
            args.extend([field.flag, *actions])
        else:
            args.extend([field.flag, str(value)])
    if op.id == "route":
        selected_mode = params["route_mode"]
        args.append("--" + str(selected_mode))
    if mode == "apply":
        args.extend(op.live_flags)
    elif op.preview_flag:
        args.append("--dry-run")
    return args


def public_operation(op: OperationSpec) -> dict[str, Any]:
    """Return a template-safe description of an operation and its parameters."""
    return {
        "id": op.id,
        "title": op.title,
        "group": op.group,
        "risk": op.risk,
        "help": op.help,
        "can_apply": op.can_apply,
        "legacy": op.legacy,
        "fields": [
            {
                "name": f.name,
                "label": f.label,
                "kind": f.kind,
                "default": f.default,
                "help": f.help,
                "minimum": f.minimum,
                "maximum": f.maximum,
                "choices": f.choices,
                "required": f.name in op.required,
            }
            for f in op.fields
        ],
    }


def validate_scope_value(value: str, music_root: Path) -> str:
    """Reject absolute, traversal, or control-character scope values."""
    raw = value.strip()
    if not raw:
        raise ValueError("Folder scope cannot be empty")
    if Path(raw).is_absolute():
        raise ValueError("Folder scope must be relative to the configured library root")
    if re.search(r"[\x00-\x1f\x7f]", raw):
        raise ValueError("Folder scope contains invalid characters")
    candidate = (music_root / raw).resolve()
    if not candidate.is_relative_to(music_root.resolve()):
        raise ValueError("Folder scope must stay inside the configured library root")
    return raw

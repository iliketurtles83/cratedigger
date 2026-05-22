"""Preference helpers for scoped overrides, remapping, and labels."""

from __future__ import annotations

import json
from pathlib import Path

import config
from lib.genres import normalise_genre


def _normalise_key(value: str | None) -> str:
    return (value or "").strip().lower()


def _get_preference_overrides() -> dict:
    raw = getattr(config, "PREFERENCE_OVERRIDES", {})
    return raw if isinstance(raw, dict) else {}


def _scope_value(
    scope_map: dict,
    key: str,
    setting: str,
) -> object | None:
    scoped = scope_map.get(_normalise_key(key))
    if isinstance(scoped, dict):
        return scoped.get(setting)
    return scoped


def _coerce_positive_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def resolve_artist_folder_threshold(
    *,
    artist: str | None,
    genre_folder: str | None,
    default: int,
) -> int:
    """Resolve artist-folder threshold with artist > genre > global precedence."""
    overrides = _get_preference_overrides()
    setting = "artist_folder_threshold"

    resolved: object | None = None

    global_scope = overrides.get("global", {})
    if isinstance(global_scope, dict):
        resolved = global_scope.get(setting)

    genre_scope = overrides.get("genre", {})
    if isinstance(genre_scope, dict) and genre_folder:
        resolved_genre = _scope_value(genre_scope, genre_folder, setting)
        if resolved_genre is not None:
            resolved = resolved_genre

    artist_scope = overrides.get("artist", {})
    if isinstance(artist_scope, dict) and artist:
        resolved_artist = _scope_value(artist_scope, artist, setting)
        if resolved_artist is not None:
            resolved = resolved_artist

    return _coerce_positive_int(resolved) or default


def _genre_remap_table() -> dict[str, str]:
    raw = getattr(config, "GENRE_REMAP_RULES", {})
    if not isinstance(raw, dict):
        return {}

    remap: dict[str, str] = {}
    for source, target in raw.items():
        source_norm = _normalise_key(normalise_genre(str(source)))
        target_norm = normalise_genre(str(target).strip())
        if source_norm and target_norm:
            remap[source_norm] = target_norm
    return remap


def remap_genre(genre: str) -> str:
    """Return remapped genre when configured, otherwise normalized input."""
    normalised = normalise_genre(genre)
    mapped = _genre_remap_table().get(_normalise_key(normalised))
    return mapped or normalised


def remap_genre_value(genre_value: str | None) -> str | None:
    """Apply genre remapping to a slash-separated genre value."""
    if not genre_value:
        return None

    seen: set[str] = set()
    parts: list[str] = []
    for raw in genre_value.split("/"):
        candidate = remap_genre(raw.strip())
        if not candidate:
            continue
        key = _normalise_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        parts.append(candidate)

    return " / ".join(parts) if parts else None


def _collect_labels_from_scope(
    scope_map: dict,
    key: str,
    *,
    label_setting: str,
) -> list[str]:
    scoped = scope_map.get(_normalise_key(key))
    if isinstance(scoped, dict):
        raw_labels = scoped.get(label_setting, [])
    else:
        raw_labels = scoped

    if not isinstance(raw_labels, list):
        return []

    labels: list[str] = []
    for label in raw_labels:
        text = str(label).strip()
        if text:
            labels.append(text)
    return labels


def resolve_preference_labels(
    *,
    artist: str | None,
    genre_value: str | None,
) -> list[str]:
    """Resolve labels from global + genre + artist scopes."""
    overrides = _get_preference_overrides()
    label_setting = "preference_labels"
    resolved: list[str] = []
    seen: set[str] = set()

    def _append(values: list[str]) -> None:
        for value in values:
            key = _normalise_key(value)
            if key and key not in seen:
                seen.add(key)
                resolved.append(value)

    global_scope = overrides.get("global", {})
    if isinstance(global_scope, dict):
        global_labels = global_scope.get(label_setting, [])
        if isinstance(global_labels, list):
            _append([str(item).strip() for item in global_labels if str(item).strip()])

    genre_scope = overrides.get("genre", {})
    primary_genre = None
    remapped = remap_genre_value(genre_value)
    if remapped:
        primary_genre = remapped.split("/")[0].strip()
    if isinstance(genre_scope, dict) and primary_genre:
        _append(
            _collect_labels_from_scope(
                genre_scope,
                primary_genre,
                label_setting=label_setting,
            )
        )

    artist_scope = overrides.get("artist", {})
    if isinstance(artist_scope, dict) and artist:
        _append(
            _collect_labels_from_scope(
                artist_scope,
                artist,
                label_setting=label_setting,
            )
        )

    return resolved


def write_preference_labels(
    path: Path,
    labels: list[str],
    *,
    dry_run: bool,
) -> bool:
    """Write file labels to a sidecar JSON store. Returns True when changed."""
    if not labels:
        return False

    store_path = Path(getattr(config, "PREFERENCE_LABELS_PATH", Path("preference_labels.json")))

    payload: dict[str, object] = {"version": 1, "labels": {}}
    if store_path.exists():
        try:
            loaded = json.loads(store_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (json.JSONDecodeError, OSError):
            pass

    labels_map = payload.get("labels")
    if not isinstance(labels_map, dict):
        labels_map = {}
        payload["labels"] = labels_map

    key = str(path)
    current = labels_map.get(key)
    if current == labels:
        return False

    if dry_run:
        return True

    labels_map[key] = labels
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return True
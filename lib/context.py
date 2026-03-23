"""Folder context and classification helpers (single source of truth)."""

from dataclasses import dataclass
from pathlib import Path
import re

import config


_DISC_PATTERN = re.compile(r"^(cd|disc|disk)\s*\d+$", re.IGNORECASE)


@dataclass
class FolderContext:
    top: str
    genre: str | None
    subgenre: str | None
    folder_kind: str
    is_genre_folder: bool
    is_special_folder: bool
    is_compilation: bool
    is_soundtrack: bool
    needs_bpm: bool
    skip: bool
    depth: int


def is_disc_subfolder(name: str) -> bool:
    return bool(_DISC_PATTERN.match(name))


def classify_folder(folder: Path) -> str:
    """Classify folder shape for move/rename logic.

    Returns: album | artist | artist_mixed | artist_flat |
             subgenre | local_special | disc | unknown
    """
    name = folder.name

    try:
        children = list(folder.iterdir()) if folder.is_dir() else []
    except OSError:
        return "unknown"

    has_audio = any(
        child.is_file() and child.suffix.lower() in config.AUDIO_EXTENSIONS
        for child in children
    )
    has_album_subdirs = any(
        child.is_dir() and classify_folder(child) in ("album", "artist", "artist_flat")
        for child in children
    )

    starts_with_0 = name.startswith("0")
    has_separator = " - " in name

    if is_disc_subfolder(name):
        return "disc"
    if starts_with_0 and name in config.SUBGENRE_BUCKETS:
        return "subgenre"
    if starts_with_0:
        return "local_special"
    if has_separator:
        return "album"
    if has_album_subdirs and not has_audio:
        return "artist"
    if has_album_subdirs and has_audio:
        return "artist_mixed"
    if not has_album_subdirs and has_audio:
        return "artist_flat"
    return "unknown"


def get_folder_context(path: Path) -> FolderContext:
    """Build folder context for an audio file path or folder path."""
    candidate = path if path.is_dir() else path.parent

    try:
        rel = candidate.relative_to(config.MUSIC_ROOT)
        parts = rel.parts
    except ValueError:
        parts = ()

    top = parts[0].lower() if parts else ""
    depth = len(parts)

    is_genre_folder = top in config.GENRE_FOLDERS
    is_special_folder = top in config.SPECIAL_FOLDERS

    genre: str | None = None
    subgenre: str | None = None

    if is_genre_folder:
        genre = config.FOLDER_TO_GENRE.get(top)
        if len(parts) >= 2:
            bucket = parts[1]
            if bucket in config.SUBGENRE_BUCKETS:
                subgenre = config.SUBGENRE_BUCKETS[bucket]
    elif top in {"0random", "0random_good"} and len(parts) >= 2:
        genre = config.FOLDER_TO_GENRE.get(parts[1].lower())

    # Compilation context: top-level ALBUMARTIST_FOLDERS OR local
    # 0compilations/0various subfolders inside genre folders.
    local_parts = {p.lower() for p in parts}
    is_compilation = (
        top in config.ALBUMARTIST_FOLDERS
        or bool(local_parts & {"0compilations", "0various"})
    )
    is_soundtrack = top == "soundtrack"
    needs_bpm = top not in config.NO_BPM_FOLDERS
    skip = top in config.SKIP_FOLDERS

    folder_kind = classify_folder(candidate) if candidate.exists() else "unknown"

    return FolderContext(
        top=top,
        genre=genre,
        subgenre=subgenre,
        folder_kind=folder_kind,
        is_genre_folder=is_genre_folder,
        is_special_folder=is_special_folder,
        is_compilation=is_compilation,
        is_soundtrack=is_soundtrack,
        needs_bpm=needs_bpm,
        skip=skip,
        depth=depth,
    )

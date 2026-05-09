"""Folder context and classification helpers (single source of truth)."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re

import config
from lib.tags import read_tags


_DISC_PATTERN = re.compile(r"^(cd|disc|disk)\s*\d+$", re.IGNORECASE)
_COMPILATION_ALBUMARTISTS = {
    "va",
    "various",
    "various artists",
    "original soundtrack",
    "soundtrack",
}


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


def _normalise_tag_value(value: str | None) -> str | None:
    if not value:
        return None
    normalised = " ".join(value.split()).strip().lower()
    return normalised or None


def infer_compilation_folder(
    folder: Path,
    *,
    file_tag_dicts: list[dict[str, str | None]] | None = None,
) -> bool:
    """Infer whether *folder* should be treated as a compilation album.

    Explicit compilation/soundtrack folders are detected from path context.
    Genre-local compilations are inferred from the tags inside the folder.
    """
    ctx = get_folder_context(folder)
    if ctx.is_compilation or ctx.is_soundtrack:
        return True

    if ctx.folder_kind not in ("album", "artist_flat", "disc"):
        return False

    if file_tag_dicts is None:
        file_tag_dicts = []
        for child in sorted(folder.rglob("*")):
            if not child.is_file() or child.suffix.lower() not in config.AUDIO_EXTENSIONS:
                continue
            tags = read_tags(child)
            if tags is not None:
                file_tag_dicts.append(tags)

    if len(file_tag_dicts) < 2:
        return False

    artist_values = [
        artist
        for tags in file_tag_dicts
        if (artist := _normalise_tag_value(tags.get("artist")))
    ]
    album_values = {
        album
        for tags in file_tag_dicts
        if (album := _normalise_tag_value(tags.get("album")))
    }
    year_values = {
        year[:4]
        for tags in file_tag_dicts
        if (year := _normalise_tag_value(tags.get("year"))) and re.match(r"^\d{4}", year)
    }
    albumartist_values = {
        albumartist
        for tags in file_tag_dicts
        if (albumartist := _normalise_tag_value(tags.get("albumartist")))
    }

    if len(album_values) > 1 or len(year_values) > 1:
        # For artist_flat folders, different albums/years means genuinely mixed
        # content — not a single compilation.  For "album" folders (name has
        # " - "), inconsistent tags are likely sloppy metadata, not different
        # albums, so we continue checking artist distribution.
        if ctx.folder_kind != "album":
            return False

    # Require COMPILATION_ARTIST_THRESHOLD distinct track artists before
    # evaluating any compilation signal.  This ensures sloppy albumartist tags
    # (e.g. albumartist="Sergio Mendes Featuring X" on a single-artist album,
    # or one rogue track tagged albumartist="Various Artists") never trigger
    # compilation logic on a genuine artist album.
    artist_counts = Counter(artist_values)
    if len(artist_counts) < config.COMPILATION_ARTIST_THRESHOLD:
        return False

    # Enough distinct track artists — albumartist tags are now reliable signals.
    if albumartist_values & _COMPILATION_ALBUMARTISTS:
        return True

    if len(albumartist_values) == 1:
        albumartist = next(iter(albumartist_values))
        if albumartist not in set(artist_values):
            return True

    dominant_share = artist_counts.most_common(1)[0][1] / len(artist_values)
    return dominant_share <= 0.5


_BEST_OF_KEYWORDS = re.compile(
    r"\b(best\s+of|greatest\s+hits?|anthology|retrospective|"
    r"the\s+collection|essential|singles\s+collection|"
    r"complete\s+collection|rarities|b[\-\s]?sides)\b",
    re.IGNORECASE,
)

# Any 4-digit year (1900–2099) in a folder name signals a single dated release
_FOLDER_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def infer_best_of_folder(
    folder: Path,
    *,
    file_tag_dicts: list[dict[str, str | None]] | None = None,
) -> bool:
    """Return True when *folder* looks like a single-artist best-of/anthology.

    Detection order:
    1. Folder name contains best-of keywords (e.g. "Greatest Hits") → True.
    2. Folder name contains a year (e.g. "[1979]", "(2003)") → False;
       a release year in the name means it is a regular dated album, not a
       retrospective, even if per-track years are messy.
    3. Exactly one distinct track artist AND year tags spanning ≥ 5 years →
       True (tracks carry original recording dates, a hallmark of best-ofs).
    """
    folder_name = folder.name

    # Rule 1 — keyword match: always a best-of regardless of year in name
    if _BEST_OF_KEYWORDS.search(folder_name):
        # Still require single artist in tags to exclude multi-artist comps
        if file_tag_dicts is None:
            file_tag_dicts = []
            for child in sorted(folder.rglob("*")):
                if not child.is_file() or child.suffix.lower() not in config.AUDIO_EXTENSIONS:
                    continue
                tags = read_tags(child)
                if tags is not None:
                    file_tag_dicts.append(tags)
        artist_values = {
            _normalise_tag_value(tags.get("artist"))
            for tags in file_tag_dicts
            if _normalise_tag_value(tags.get("artist"))
        }
        if len(artist_values) == 1:
            return True

    if file_tag_dicts is None:
        file_tag_dicts = []
        for child in sorted(folder.rglob("*")):
            if not child.is_file() or child.suffix.lower() not in config.AUDIO_EXTENSIONS:
                continue
            tags = read_tags(child)
            if tags is not None:
                file_tag_dicts.append(tags)

    if len(file_tag_dicts) < 2:
        return False

    # Rule 2 — folder name has a year → regular dated release, not a best-of
    if _FOLDER_YEAR.search(folder_name):
        return False

    artist_values = {
        _normalise_tag_value(tags.get("artist"))
        for tags in file_tag_dicts
        if _normalise_tag_value(tags.get("artist"))
    }
    if len(artist_values) != 1:
        return False

    # Rule 3 — year-span heuristic
    years = [
        int(year[:4])
        for tags in file_tag_dicts
        if (year := _normalise_tag_value(tags.get("year"))) and re.match(r"^\d{4}", year)
    ]
    if len(years) < 2:
        return False

    return (max(years) - min(years)) >= 5


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

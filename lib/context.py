"""Folder context and classification helpers (single source of truth)."""

import functools
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterator

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

# Subdirectory names skipped when collecting audio tags (artwork, extras, etc.)
IGNORE_SUBDIRS: frozenset[str] = frozenset({
    "artwork", "covers", "scans", "bonus", "extras",
    ".stfolder", ".stversions",
})

# Folder kinds that qualify a child as an album-level folder.
# Keeping "artist" out of this set prevents unbounded recursion: album folders
# are identified by name pattern (" - ") and never re-enter classify_folder
# deeply; artist_flat and disc are shallow terminal classifications.
VALID_ALBUM_KINDS: frozenset[str] = frozenset({"album", "artist_flat", "disc"})


@dataclass
class CompilationResult:
    """Compilation inference result with confidence score and diagnostic reason.

    Evaluates as a bool equal to ``is_compilation`` so existing call sites
    that treat the return value as a boolean continue to work unchanged.
    """

    is_compilation: bool
    score: float
    reason: str

    def __bool__(self) -> bool:
        return self.is_compilation


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
        child.is_dir() and classify_folder(child) in VALID_ALBUM_KINDS
        for child in children
    )

    starts_with_0 = name.startswith("0")
    has_separator = " - " in name

    # Artist-root: single A-Z letter bucket or symbol bucket (#)
    _letter_buckets = getattr(config, "LETTER_BUCKETS", set())
    _symbol_bucket  = getattr(config, "SYMBOL_BUCKET", "#")
    if name == _symbol_bucket or (len(name) == 1 and name.upper() in _letter_buckets):
        return "letter_bucket"

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


# ---------------------------------------------------------------------------
# Tag caching
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=4096)
def _read_tags_cached(path: Path) -> tuple[tuple[str, str | None], ...] | None:
    """Read tags from *path*, caching the result for the process lifetime."""
    result = read_tags(path)
    if result is None:
        return None
    return tuple(sorted(result.items()))


def _get_tags(path: Path) -> dict[str, str | None] | None:
    """Return cached tags for *path* as a plain dict."""
    cached = _read_tags_cached(path)
    return dict(cached) if cached is not None else None


def _iter_audio_files(folder: Path) -> Iterator[Path]:
    """Yield audio files under *folder*, skipping IGNORE_SUBDIRS subtrees."""
    for child in sorted(folder.rglob("*")):
        if child.is_dir():
            continue
        rel_parts = child.relative_to(folder).parts
        if any(part in IGNORE_SUBDIRS for part in rel_parts[:-1]):
            continue
        if child.suffix.lower() in config.AUDIO_EXTENSIONS:
            yield child


def infer_compilation_folder(
    folder: Path,
    *,
    file_tag_dicts: list[dict[str, str | None]] | None = None,
) -> CompilationResult:
    """Infer whether *folder* should be treated as a compilation album.

    Explicit compilation/soundtrack folders are detected from path context.
    Genre-local compilations are inferred from the tags inside the folder.

    Returns a :class:`CompilationResult` with ``is_compilation`` (bool),
    ``score`` (float 0.0–1.0), and ``reason`` (str).  The result evaluates
    as a bool equal to ``is_compilation`` for backward compatibility.
    """
    ctx = get_folder_context(folder)
    if ctx.is_compilation or ctx.is_soundtrack:
        return CompilationResult(is_compilation=True, score=1.0, reason="path_context")

    if ctx.folder_kind not in ("album", "artist_flat", "disc"):
        return CompilationResult(is_compilation=False, score=0.0, reason="wrong_folder_kind")

    if file_tag_dicts is None:
        file_tag_dicts = [
            tags
            for audio_path in _iter_audio_files(folder)
            if (tags := _get_tags(audio_path)) is not None
        ]

    if len(file_tag_dicts) < 2:
        return CompilationResult(is_compilation=False, score=0.0, reason="insufficient_files")

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
            return CompilationResult(is_compilation=False, score=0.0, reason="mixed_albums")

    # Require COMPILATION_ARTIST_THRESHOLD distinct track artists before
    # evaluating any compilation signal.  This ensures sloppy albumartist tags
    # (e.g. albumartist="Sergio Mendes Featuring X" on a single-artist album,
    # or one rogue track tagged albumartist="Various Artists") never trigger
    # compilation logic on a genuine artist album.
    artist_counts = Counter(artist_values)
    if len(artist_counts) < config.COMPILATION_ARTIST_THRESHOLD:
        return CompilationResult(is_compilation=False, score=0.0, reason="low_tag_coverage")

    # --- Weighted scoring (components sum to 1.0 max) ---
    threshold = config.COMPILATION_ARTIST_THRESHOLD

    # 0.4: artist diversity relative to threshold
    distinct_artist_score = min(len(artist_counts) / threshold, 1.0) * 0.4

    # 0.5: albumartist tag is a known VA/compilation value, or is a single
    #      albumartist that differs from all per-track artists.
    albumartist_match = bool(albumartist_values & _COMPILATION_ALBUMARTISTS)
    single_albumartist_mismatch = (
        not albumartist_match
        and len(albumartist_values) == 1
        and next(iter(albumartist_values)) not in set(artist_values)
    )
    albumartist_score = 0.5 if (albumartist_match or single_albumartist_mismatch) else 0.0

    # 0.3: no single dominant artist (tracks spread evenly)
    dominant_share = artist_counts.most_common(1)[0][1] / len(artist_values)
    distribution_score = 0.3 if dominant_share <= 0.5 else 0.0

    compilation_score = distinct_artist_score + albumartist_score + distribution_score
    is_compilation = albumartist_match or single_albumartist_mismatch or dominant_share <= 0.5

    return CompilationResult(
        is_compilation=is_compilation,
        score=round(compilation_score, 3),
        reason="scored",
    )


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
            file_tag_dicts = [
                tags
                for audio_path in _iter_audio_files(folder)
                if (tags := _get_tags(audio_path)) is not None
            ]
        artist_values = {
            _normalise_tag_value(tags.get("artist"))
            for tags in file_tag_dicts
            if _normalise_tag_value(tags.get("artist"))
        }
        if len(artist_values) == 1:
            return True

    if file_tag_dicts is None:
        file_tag_dicts = [
            tags
            for audio_path in _iter_audio_files(folder)
            if (tags := _get_tags(audio_path)) is not None
        ]

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
    # Note: 0random / 0mixes genre is resolved via SPECIAL_FOLDER_GENRE_POLICY
    # in 01_tag.py, not from the subfolder path.  No genre is path-inferred here.

    # Compilation context:
    # 1) top-level ALBUMARTIST_FOLDERS (0compilations, 0various, soundtrack)
    # 2) explicit local compilation buckets directly under a genre folder (legacy)
    local_compilation_bucket = (
        is_genre_folder
        and len(parts) >= 2
        and parts[1].lower() in {"0compilations", "0various"}
    )
    is_compilation = (
        top in config.ALBUMARTIST_FOLDERS
        or top in {"0compilations", "0various"}
        or local_compilation_bucket
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

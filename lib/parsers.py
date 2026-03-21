"""Filename and folder name pattern parsing."""

import re
from pathlib import Path


# Patterns ordered from most specific to least specific.
_PATTERNS = [
    # 01 - Artist Name - Song Title.ext
    re.compile(
        r"^(?P<track>\d{1,3})\s*-\s*(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
    ),
    # Artist Name - 01 - Song Title.ext
    re.compile(
        r"^(?P<artist>.+?)\s*-\s*(?P<track>\d{1,3})\s*-\s*(?P<title>.+)$"
    ),
    # Artist Name - Song Title.ext
    re.compile(
        r"^(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
    ),
    # 01 Song Title.ext  (no artist)
    re.compile(
        r"^(?P<track>\d{1,3})\s+(?P<title>.+)$"
    ),
]

# Folder pattern: Artist Name - Album Title (Year)
_FOLDER_PATTERN = re.compile(
    r"^(?P<artist>.+?)\s*-\s*(?P<album>.+?)\s*\((?P<year>\d{4})\)$"
)


def parse_filename(path: Path) -> dict[str, str | None]:
    """Parse an audio filename into ``{artist, title, track}`` (any may be None)."""
    stem = path.stem
    for pat in _PATTERNS:
        m = pat.match(stem)
        if m:
            groups = m.groupdict()
            return {
                "artist": groups.get("artist", "").strip() or None,
                "title": groups.get("title", "").strip() or None,
                "track": groups.get("track", "").strip().lstrip("0") or None,
            }
    # Fallback — treat the whole stem as title
    return {"artist": None, "title": stem.strip() or None, "track": None}


def parse_folder_name(name: str) -> dict[str, str | None]:
    """Parse ``Artist - Album (Year)`` into ``{artist, album, year}``."""
    m = _FOLDER_PATTERN.match(name.strip())
    if m:
        return {
            "artist": m.group("artist").strip(),
            "album": m.group("album").strip(),
            "year": m.group("year"),
        }
    return {"artist": None, "album": None, "year": None}


def build_filename(track: str | None, artist: str | None, title: str | None,
                   ext: str = ".mp3") -> str:
    """Build the canonical filename: ``01 - Artist Name - Song Title.ext``.

    Falls back gracefully when fields are missing.
    """
    parts: list[str] = []
    if track is not None:
        parts.append(track.zfill(2))
    if artist:
        parts.append(artist)
    if title:
        parts.append(title)
    if not parts:
        return f"unknown{ext}"
    return " - ".join(parts) + ext

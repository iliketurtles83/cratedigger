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
    # 04-Song Title.ext  (dash without spaces, ripper format)
    re.compile(
        r"^(?P<track>\d{1,3})-(?P<title>.+)$"
    ),
    # Artist Name - Song Title.ext
    re.compile(
        r"^(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
    ),
    # 08.Song Title.ext  (dot-separated track, no artist)
    re.compile(
        r"^(?P<track>\d{1,3})\.(?P<title>.+)$"
    ),
    # 01 Song Title.ext  (space-separated track, no artist)
    re.compile(
        r"^(?P<track>\d{1,3})\s+(?P<title>.+)$"
    ),
]

# Folder pattern: Artist Name - Album Title (Year)
_FOLDER_PATTERN = re.compile(
    r"^(?P<artist>.+?)\s*-\s*(?P<album>.+?)\s*\((?P<year>\d{4})\)$"
)

# Characters illegal in filenames on Linux/macOS/Windows
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# Common ripper annotations to strip
_RIPPER_ANNOTATIONS = re.compile(
    r'\s*\[\s*[-#*]+\s*\]\s*'  # marker-only brackets (e.g. [-], [#], [*])
    r'|\s*\[\s*\]\s*'       # empty brackets left after cleanup
    r'|\s*\[DM\]\s*'         # ripper group
    r'|\s*\[DDR\]\s*'        # ripper group
    r'|\s*\[NoFS\]\s*'       # ripper group
    r'|\s*\[raw\]\s*'        # quality tag
    r'|\s*\[ChattChitto RG\]\s*'  # ripper group
    r'|\s*\[www\.[^\]]+\]\s*'     # any URL watermark
    r'|\s*\[plixid\.com\]\s*',    # watermark
    re.IGNORECASE
)

def _sanitise(value: str) -> str:
    """Remove ripper annotations and illegal filename characters."""
    # Strip ripper damage/incomplete markers
    value = _RIPPER_ANNOTATIONS.sub("", value)
    # Replace forward slash with dash
    value = value.replace("/", "-")
    # Remove remaining illegal characters
    value = _ILLEGAL_CHARS.sub("", value)
    # Collapse multiple spaces
    value = re.sub(r" {2,}", " ", value)
    return value.strip()

def _clean_track(track: str | None) -> str | None:
    """Normalise track number — strip total tracks (e.g. '1/12' → '1')."""
    if not track:
        return None
    return track.split("/")[0].strip().lstrip("0") or "0"


def parse_filename(path: Path) -> dict[str, str | None]:
    """Parse an audio filename into ``{artist, title, track}`` (any may be None)."""
    stem = path.stem
    for pat in _PATTERNS:
        m = pat.match(stem)
        if m:
            groups = m.groupdict()
            return {
                "artist": groups.get("artist", "").strip() or None,
                "title":  groups.get("title",  "").strip() or None,
                "track":  _clean_track(groups.get("track")),
            }
    # Fallback — treat the whole stem as title
    return {"artist": None, "title": stem.strip() or None, "track": None}


def parse_folder_name(name: str) -> dict[str, str | None]:
    """Parse ``Artist - Album (Year)`` into ``{artist, album, year}``."""
    m = _FOLDER_PATTERN.match(name.strip())
    if m:
        return {
            "artist": m.group("artist").strip(),
            "album":  m.group("album").strip(),
            "year":   m.group("year"),
        }
    return {"artist": None, "album": None, "year": None}


def build_filename(
    track: str | None,
    artist: str | None,
    title: str | None,
    ext: str = ".mp3",
) -> str:
    """Build the canonical filename: ``01 - Artist Name - Song Title.ext``.

    Falls back gracefully when fields are missing.
    Sanitises illegal filename characters from all fields.
    """
    parts: list[str] = []

    if track is not None:
        clean_track = _clean_track(track)
        if clean_track:
            parts.append(clean_track.zfill(2))

    if artist:
        parts.append(_sanitise(artist))

    if title:
        parts.append(_sanitise(title))

    if not parts:
        return f"unknown{ext}"

    return " - ".join(parts) + ext

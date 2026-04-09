"""Mutagen read / write wrappers for all supported audio formats."""

from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TBPM, TCON, TDRC, TIT2, TALB, TPE1, TPE2, TPOS, TRCK, TSRC


# ---------------------------------------------------------------------------
# Field name → tag key mapping per format family
# ---------------------------------------------------------------------------
_ID3_MAP = {
    "title":    "TIT2",
    "artist":   "TPE1",
    "albumartist": "TPE2",
    "album":    "TALB",
    "year":     "TDRC",
    "genre":    "TCON",
    "bpm":      "TBPM",
    "track":    "TRCK",
    "disc":     "TPOS",
    "isrc":     "TSRC",
}

_VORBIS_MAP = {
    "title":    "title",
    "artist":   "artist",
    "albumartist": "albumartist",
    "album":    "album",
    "year":     "date",
    "genre":    "genre",
    "bpm":      "bpm",
    "track":    "tracknumber",
    "disc":     "discnumber",
    "isrc":     "isrc",
}

_M4A_MAP = {
    "title":    "\xa9nam",
    "artist":   "\xa9ART",
    "albumartist": "aART",
    "album":    "\xa9alb",
    "year":     "\xa9day",
    "genre":    "\xa9gen",
    "track":    "trkn",
    "disc":     "disk",
    # BPM not supported in M4A
    # ISRC not supported in M4A
}


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def _first(val: object) -> str | None:
    """Extract the first string value from a mutagen tag value."""
    if val is None:
        return None
    if isinstance(val, list):
        return str(val[0]).strip() if val else None
    return str(val).strip() or None


def read_tags(path: Path) -> dict[str, str | None] | None:
    """Read standard tag fields from *path*, returning a dict.

    Returns ``None`` and logs a warning if the file cannot be read.
    Keys when successful: title, artist, albumartist, album, year,
    genre, bpm, track, isrc.
    Values are ``None`` when absent.
    """
    result: dict[str, str | None] = {
        k: None for k in ("title", "artist", "albumartist", "album", "year",
                          "genre", "bpm", "track", "disc", "isrc")
    }

    try:
        audio = MutagenFile(path, easy=False)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Cannot read tags from %s: %s", path, exc)
        return None
    if audio is None:
        return result

    suffix = path.suffix.lower()

    if suffix == ".mp3":
        tags = audio.tags
        if tags is None:
            return result
        for field, frame_id in _ID3_MAP.items():
            frame = tags.get(frame_id)
            if frame is not None:
                result[field] = _first(frame.text if hasattr(frame, "text") else frame)
    elif suffix in {".flac", ".ogg", ".opus"}:
        tags = audio.tags or {}
        for field, key in _VORBIS_MAP.items():
            result[field] = _first(tags.get(key))
    elif suffix in {".m4a", ".aac"}:
        tags = audio.tags or {}
        for field, key in _M4A_MAP.items():
            val = tags.get(key)
            if field in ("track", "disc") and isinstance(val, list) and val:
                # trkn/disk store [(num, total)]
                result[field] = str(val[0][0]) if isinstance(val[0], tuple) else str(val[0])
            else:
                result[field] = _first(val)

    return result


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _ensure_id3(audio) -> ID3:
    if audio.tags is None:
        audio.add_tags()
    return audio.tags


def write_tags(path: Path, tags: dict[str, str | None], *, dry_run: bool = True) -> dict[str, str]:
    """Write *tags* to *path*. Only non-None values are written.

    Returns a dict of ``{field: value}`` actually written.
    Respects *dry_run*: when True, returns what *would* be written without saving.
    """
    written: dict[str, str] = {}
    suffix = path.suffix.lower()

    audio = MutagenFile(path, easy=False)
    if audio is None:
        return written

    if suffix == ".mp3":
        id3 = _ensure_id3(audio)
        frame_classes = {
            "title": TIT2, "artist": TPE1, "albumartist": TPE2, "album": TALB,
            "year": TDRC, "genre": TCON, "bpm": TBPM,
            "track": TRCK, "disc": TPOS, "isrc": TSRC,
        }
        for field, val in tags.items():
            if val is None or field not in frame_classes:
                continue
            cls = frame_classes[field]
            id3[cls.__name__] = cls(encoding=3, text=[val])
            written[field] = val

    elif suffix in {".flac", ".ogg", ".opus"}:
        if audio.tags is None:
            audio.add_tags()
        for field, val in tags.items():
            if val is None or field not in _VORBIS_MAP:
                continue
            audio.tags[_VORBIS_MAP[field]] = [val]
            written[field] = val

    elif suffix in {".m4a", ".aac"}:
        if audio.tags is None:
            audio.add_tags()
        for field, val in tags.items():
            if val is None or field not in _M4A_MAP:
                continue
            key = _M4A_MAP[field]
            if field in ("track", "disc"):
                # trkn/disk expect [(num, total)]
                try:
                    audio.tags[key] = [(int(val), 0)]
                except (ValueError, TypeError):
                    continue
            else:
                audio.tags[key] = [val]
            written[field] = val

    if not dry_run and written:
        audio.save()

    return written

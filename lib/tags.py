"""Mutagen read / write wrappers for all supported audio formats."""

from pathlib import Path
import re

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

_NUMBER_PAIR = re.compile(r"^\s*(\d+)\s*(?:/\s*(\d+)\s*)?$")
_ID3_SAVE_VERSION = 3  # Compatibility-first: write ID3v2.3 tags.


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


def _normalise_number_pair(value: object) -> str | None:
    """Canonicalise track/disc values to ``num`` or ``num/total``.

    Zero values are treated as invalid. Leading zeroes and extra spaces are removed.
    """
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    match = _NUMBER_PAIR.match(text)
    if not match:
        return None

    number = int(match.group(1))
    if number <= 0:
        return None

    total_raw = match.group(2)
    if total_raw is None:
        return str(number)

    total = int(total_raw)
    if total <= 0:
        return str(number)

    return f"{number}/{total}"


def _normalise_bpm(value: object) -> str | None:
    """Canonicalise BPM to a positive integer string."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        bpm = round(float(text))
    except (TypeError, ValueError):
        return None

    return str(bpm) if bpm > 0 else None


def _m4a_pair_to_text(value: object) -> str | None:
    """Normalise M4A ``trkn``/``disk`` atoms to text form."""
    if not isinstance(value, list) or not value:
        return None

    first = value[0]
    if isinstance(first, tuple) and first:
        number = first[0]
        total = first[1] if len(first) > 1 else 0
        if number is None:
            return None
        text = str(int(number))
        if int(total) > 0:
            text += f"/{int(total)}"
        return _normalise_number_pair(text)

    return _normalise_number_pair(first)


def _text_to_m4a_pair(value: object) -> tuple[int, int] | None:
    """Convert canonical text form into M4A ``(num, total)`` tuple."""
    normalised = _normalise_number_pair(value)
    if not normalised:
        return None

    parts = normalised.split("/", 1)
    number = int(parts[0])
    total = int(parts[1]) if len(parts) > 1 else 0
    return (number, total)


def _prepare_written_value(field: str, value: str | None) -> str | None:
    """Normalise outgoing tag values for standards-compliant writes."""
    if value is None:
        return None
    if field in {"track", "disc"}:
        return _normalise_number_pair(value)
    if field == "bpm":
        return _normalise_bpm(value)
    return value


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
                value = _first(frame.text if hasattr(frame, "text") else frame)
                if field in {"track", "disc"}:
                    value = _normalise_number_pair(value)
                elif field == "bpm":
                    value = _normalise_bpm(value)
                result[field] = value
    elif suffix in {".flac", ".ogg", ".opus"}:
        tags = audio.tags or {}
        for field, key in _VORBIS_MAP.items():
            value = _first(tags.get(key))
            if field in {"track", "disc"}:
                value = _normalise_number_pair(value)
            elif field == "bpm":
                value = _normalise_bpm(value)
            result[field] = value
    elif suffix in {".m4a", ".aac"}:
        tags = audio.tags or {}
        for field, key in _M4A_MAP.items():
            val = tags.get(key)
            if field in ("track", "disc"):
                result[field] = _m4a_pair_to_text(val)
            else:
                value = _first(val)
                if field == "bpm":
                    value = _normalise_bpm(value)
                result[field] = value

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
            if field not in frame_classes:
                continue
            prepared = _prepare_written_value(field, val)
            if prepared is None:
                continue
            cls = frame_classes[field]
            id3[cls.__name__] = cls(encoding=3, text=[prepared])
            written[field] = prepared

    elif suffix in {".flac", ".ogg", ".opus"}:
        if audio.tags is None:
            audio.add_tags()
        for field, val in tags.items():
            if field not in _VORBIS_MAP:
                continue
            prepared = _prepare_written_value(field, val)
            if prepared is None:
                continue
            audio.tags[_VORBIS_MAP[field]] = [prepared]
            written[field] = prepared

    elif suffix in {".m4a", ".aac"}:
        if audio.tags is None:
            audio.add_tags()
        for field, val in tags.items():
            if field not in _M4A_MAP:
                continue
            key = _M4A_MAP[field]
            if field in ("track", "disc"):
                # trkn/disk expect [(num, total)]
                pair = _text_to_m4a_pair(val)
                if pair is None:
                    continue
                audio.tags[key] = [pair]
                written[field] = _normalise_number_pair(val)  # type: ignore[arg-type]
            else:
                prepared = _prepare_written_value(field, val)
                if prepared is None:
                    continue
                audio.tags[key] = [prepared]
                written[field] = prepared

    if not dry_run and written:
        if suffix == ".mp3":
            audio.save(v2_version=_ID3_SAVE_VERSION)
        else:
            audio.save()

    return written

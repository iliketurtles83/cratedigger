"""Mutagen read / write wrappers for all supported audio formats."""

from pathlib import Path
import re

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TBPM, TCON, TDRC, TIT2, TALB, TPE1, TPE2, TPOS, TRCK, TSRC, TCMP, UFID, TXXX, POPM
from mutagen.mp4 import MP4FreeForm


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
    "compilation": "TCMP",
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
    "compilation": "compilation",
    "mb_trackid": "musicbrainz_trackid",
    "mb_albumid": "musicbrainz_albumid",
    "mb_artistid": "musicbrainz_artistid",
    "mb_albumartistid": "musicbrainz_albumartistid",
    "mb_releasegroupid": "musicbrainz_releasegroupid",
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
    "compilation": "cpil",
    # BPM not supported in M4A
    # ISRC not supported in M4A
}

_ASF_MAP = {
    "title":       "Title",
    "artist":      "Author",
    "album":       "WM/AlbumTitle",
    "albumartist": "WM/AlbumArtist",
    "year":        "WM/Year",
    "genre":       "WM/Genre",
    "bpm":         "WM/BeatsPerMinute",
    "track":       "WM/TrackNumber",
    "compilation": "WM/IsCompilation",
}

_MB_ID3_TXXX = {
    "mb_albumid": "MusicBrainz Album Id",
    "mb_artistid": "MusicBrainz Artist Id",
    "mb_albumartistid": "MusicBrainz Album Artist Id",
    "mb_releasegroupid": "MusicBrainz Release Group Id",
}

_MB_M4A_FREEFORM = {
    "mb_trackid": "----:com.apple.iTunes:MusicBrainz Track Id",
    "mb_albumid": "----:com.apple.iTunes:MusicBrainz Album Id",
    "mb_artistid": "----:com.apple.iTunes:MusicBrainz Artist Id",
    "mb_albumartistid": "----:com.apple.iTunes:MusicBrainz Album Artist Id",
    "mb_releasegroupid": "----:com.apple.iTunes:MusicBrainz Release Group Id",
}

_NUMBER_PAIR = re.compile(r"^\s*(\d+)\s*(?:/\s*(\d+)\s*)?$")
_ID3_SAVE_VERSION = 4  # Standard ID3v2.4 (aligns with beets id3v23: no and modern players)


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


def _normalise_compilation(value: object) -> str | None:
    if value is None:
        return None
    val = str(value).strip().lower()
    if val in {"1", "true", "yes"}:
        return "1"
    if val in {"0", "false", "no"}:
        return "0"
    return None


def _normalise_rating(value: object) -> str | None:
    """Normalise rating to a float string in 0.00..1.00 (e.g. 5 stars = 1.0, 4 stars = 0.8)."""
    if value is None:
        return None
    try:
        val = float(str(value).strip())
        if val < 0:
            return None
        if val > 1.0 and val <= 5.0:
            val = val / 5.0
        elif val > 5.0 and val <= 255.0:
            val = val / 255.0
        return str(round(val, 2))
    except (ValueError, TypeError):
        return None


def _prepare_written_value(field: str, value: str | None) -> str | None:
    """Normalise outgoing tag values for standards-compliant writes."""
    if value is None:
        return None
    if field in {"track", "disc"}:
        return _normalise_number_pair(value)
    if field == "bpm":
        return _normalise_bpm(value)
    if field == "compilation":
        return _normalise_compilation(value)
    if field == "rating":
        return _normalise_rating(value)
    return str(value).strip() or None


def read_tags(path: Path) -> dict[str, str | None] | None:
    """Read standard tag fields from *path*, returning a dict.

    Returns ``None`` and logs a warning if the file cannot be read.
    Keys when successful: title, artist, albumartist, album, year,
    genre, bpm, track, disc, isrc, compilation, mb_trackid, mb_albumid,
    mb_artistid, mb_albumartistid, mb_releasegroupid, rating, playcount.
    Values are ``None`` when absent.
    """
    result: dict[str, str | None] = {
        k: None for k in (
            "title", "artist", "albumartist", "album", "year",
            "genre", "bpm", "track", "disc", "isrc",
            "compilation", "mb_trackid", "mb_albumid", "mb_artistid",
            "mb_albumartistid", "mb_releasegroupid", "rating", "playcount",
        )
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
                elif field == "compilation":
                    value = _normalise_compilation(value)
                result[field] = value

        # MusicBrainz Track ID: UFID or TXXX
        ufid = tags.get("UFID:http://musicbrainz.org")
        if ufid is not None and getattr(ufid, "data", None):
            result["mb_trackid"] = ufid.data.decode("ascii", errors="ignore").strip() or None
        if not result.get("mb_trackid"):
            txxx_trkid = tags.get("TXXX:MusicBrainz Track Id") or tags.get("TXXX:MUSICBRAINZ_TRACKID")
            if txxx_trkid is not None:
                result["mb_trackid"] = _first(txxx_trkid.text)

        # Other MusicBrainz IDs in TXXX
        for field, desc in _MB_ID3_TXXX.items():
            frame = tags.get(f"TXXX:{desc}") or tags.get(f"TXXX:{desc.upper()}")
            if frame is not None:
                result[field] = _first(frame.text)

        # Rating: TXXX:FMPS_Rating or POPM
        fmps_r = tags.get("TXXX:FMPS_Rating") or tags.get("TXXX:FMPS_RATING")
        if fmps_r is not None:
            result["rating"] = _normalise_rating(_first(fmps_r.text))
        else:
            for k in tags.keys():
                if k.startswith("POPM"):
                    p = tags[k]
                    if hasattr(p, "rating") and p.rating:
                        result["rating"] = _normalise_rating(p.rating)
                        break

        # Playcount: TXXX:FMPS_Playcount
        fmps_pc = tags.get("TXXX:FMPS_Playcount") or tags.get("TXXX:FMPS_PLAYCOUNT")
        if fmps_pc is not None:
            result["playcount"] = _first(fmps_pc.text)

    elif suffix in {".flac", ".ogg", ".opus"}:
        tags = audio.tags or {}
        for field, key in _VORBIS_MAP.items():
            value = _first(tags.get(key))
            if field in {"track", "disc"}:
                value = _normalise_number_pair(value)
            elif field == "bpm":
                value = _normalise_bpm(value)
            elif field == "compilation":
                value = _normalise_compilation(value)
            result[field] = value

        r_val = _first(tags.get("fmps_rating") or tags.get("rating"))
        if r_val is not None:
            result["rating"] = _normalise_rating(r_val)

        pc_val = _first(tags.get("fmps_playcount") or tags.get("playcount"))
        if pc_val is not None:
            result["playcount"] = pc_val

    elif suffix in {".m4a", ".aac"}:
        tags = audio.tags or {}
        for field, key in _M4A_MAP.items():
            val = tags.get(key)
            if field in ("track", "disc"):
                result[field] = _m4a_pair_to_text(val)
            elif field == "compilation":
                if val is True or val == [True] or val == [1]:
                    result[field] = "1"
                elif val is False or val == [False] or val == [0]:
                    result[field] = "0"
            else:
                value = _first(val)
                if field == "bpm":
                    value = _normalise_bpm(value)
                result[field] = value

        for field, atom in _MB_M4A_FREEFORM.items():
            val = tags.get(atom)
            if val is not None:
                first = val[0] if isinstance(val, list) and val else val
                if isinstance(first, bytes):
                    result[field] = first.decode("utf-8", errors="ignore").strip() or None
                elif hasattr(first, "decode"):
                    result[field] = first.decode("utf-8", errors="ignore").strip() or None
                else:
                    result[field] = str(first).strip() or None

    elif suffix == ".wma":
        tags = audio.tags or {}
        for field, key in _ASF_MAP.items():
            val = tags.get(key)
            if val is not None:
                value = _first(val)
                if field == "track":
                    value = str(value)
                elif field == "bpm":
                    value = _normalise_bpm(str(value))
                elif field == "compilation":
                    value = _normalise_compilation(value)
                result[field] = str(value) if value is not None else None

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
            if val == "":
                if field in frame_classes:
                    cls = frame_classes[field]
                    id3.delall(cls.__name__)
                    written[field] = ""
                elif field == "compilation":
                    id3.delall("TCMP")
                    written[field] = ""
                elif field in _MB_ID3_TXXX:
                    desc = _MB_ID3_TXXX[field]
                    id3.delall(f"TXXX:{desc}")
                    written[field] = ""
                continue

            prepared = _prepare_written_value(field, val)
            if prepared is None:
                continue

            if field in frame_classes:
                cls = frame_classes[field]
                id3[cls.__name__] = cls(encoding=3, text=[prepared])
                written[field] = prepared
            elif field == "compilation":
                id3["TCMP"] = TCMP(encoding=3, text=[prepared])
                written[field] = prepared
            elif field == "mb_trackid":
                id3["UFID:http://musicbrainz.org"] = UFID(
                    owner="http://musicbrainz.org",
                    data=prepared.encode("ascii", errors="ignore"),
                )
                id3["TXXX:MusicBrainz Track Id"] = TXXX(
                    encoding=3, desc="MusicBrainz Track Id", text=[prepared]
                )
                written[field] = prepared
            elif field in _MB_ID3_TXXX:
                desc = _MB_ID3_TXXX[field]
                id3[f"TXXX:{desc}"] = TXXX(encoding=3, desc=desc, text=[prepared])
                written[field] = prepared
            elif field == "rating":
                r_float = float(prepared)
                popm_rating = round(r_float * 255)
                id3["TXXX:FMPS_Rating"] = TXXX(encoding=3, desc="FMPS_Rating", text=[prepared])
                id3["POPM:"] = POPM(email="", rating=popm_rating, count=0)
                written[field] = prepared
            elif field == "playcount":
                id3["TXXX:FMPS_Playcount"] = TXXX(encoding=3, desc="FMPS_Playcount", text=[prepared])
                written[field] = prepared

    elif suffix in {".flac", ".ogg", ".opus"}:
        if audio.tags is None:
            audio.add_tags()
        for field, val in tags.items():
            if val == "":
                if field in _VORBIS_MAP:
                    key = _VORBIS_MAP[field]
                    if key in audio.tags:
                        del audio.tags[key]
                        written[field] = ""
                continue

            prepared = _prepare_written_value(field, val)
            if prepared is None:
                continue

            if field in _VORBIS_MAP:
                audio.tags[_VORBIS_MAP[field]] = [prepared]
                written[field] = prepared
            elif field == "rating":
                audio.tags["FMPS_RATING"] = [prepared]
                audio.tags["RATING"] = [prepared]
                written[field] = prepared
            elif field == "playcount":
                audio.tags["FMPS_PLAYCOUNT"] = [prepared]
                written[field] = prepared

    elif suffix in {".m4a", ".aac"}:
        if audio.tags is None:
            audio.add_tags()
        for field, val in tags.items():
            if val == "":
                if field in _M4A_MAP:
                    key = _M4A_MAP[field]
                    if key in audio.tags:
                        del audio.tags[key]
                        written[field] = ""
                elif field == "compilation":
                    if "cpil" in audio.tags:
                        del audio.tags["cpil"]
                        written[field] = ""
                continue

            prepared = _prepare_written_value(field, val)
            if prepared is None:
                continue

            if field == "compilation":
                audio.tags["cpil"] = True if prepared == "1" else False
                written[field] = prepared
            elif field in ("track", "disc"):
                pair = _text_to_m4a_pair(prepared)
                if pair is None:
                    continue
                audio.tags[_M4A_MAP[field]] = [pair]
                written[field] = prepared
            elif field in _M4A_MAP:
                audio.tags[_M4A_MAP[field]] = [prepared]
                written[field] = prepared
            elif field in _MB_M4A_FREEFORM:
                atom = _MB_M4A_FREEFORM[field]
                audio.tags[atom] = [MP4FreeForm(prepared.encode("utf-8"))]
                written[field] = prepared

    if not dry_run and written:
        if suffix == ".mp3":
            audio.save(v2_version=_ID3_SAVE_VERSION)
        else:
            audio.save()

    return written

"""Filename and folder name pattern parsing."""

import re
import unicodedata
from pathlib import Path


# Patterns ordered from most specific to least specific.
# Disc-track patterns come first to avoid misparsing "1-01" as track=1.
_PATTERNS = [
    # 1-01 - Artist Name - Song Title.ext  (disc-track with artist)
    re.compile(
        r"^(?P<disc>\d{1,2})-(?P<track>\d{2,3})\s*-\s*(?P<artist>.+?)\s+-\s+(?P<title>.+)$"
    ),
    # Artist Name - 1-01 - Song Title.ext  (artist then disc-track)
    re.compile(
        r"^(?P<artist>.+?)\s+-\s+(?P<disc>\d{1,2})-(?P<track>\d{2,3})\s*-\s*(?P<title>.+)$"
    ),
    # 1-01-Song Title.ext  (disc-track, no artist, ripper format)
    re.compile(
        r"^(?P<disc>\d{1,2})-(?P<track>\d{2,3})-(?P<title>.+)$"
    ),
    # 1-01 Song Title.ext  (disc-track, no artist, space-separated)
    re.compile(
        r"^(?P<disc>\d{1,2})-(?P<track>\d{2,3})\s+(?P<title>.+)$"
    ),
    # 01 - Artist Name - Song Title.ext  (spaced dash between artist and title)
    re.compile(
        r"^(?P<track>\d{1,3})\s*-\s*(?P<artist>.+?)\s+-\s+(?P<title>.+)$"
    ),
    # Artist Name - 01 - Song Title.ext
    re.compile(
        r"^(?P<artist>.+?)\s+-\s+(?P<track>\d{1,3})\s*-\s*(?P<title>.+)$"
    ),
    # 01 - Song Title.ext  (track with dash and spaces, no artist)
    re.compile(
        r"^(?P<track>\d{1,3})\s*-\s*(?P<title>.+)$"
    ),
    # 04-Song Title.ext  (dash without spaces, ripper format)
    re.compile(
        r"^(?P<track>\d{1,3})-(?P<title>.+)$"
    ),
    # 13. Artist Name - Song Title.ext  (dot-prefixed track with artist)
    re.compile(
        r"^(?P<track>\d{1,3})\.\s*(?P<artist>.+?)\s+-\s+(?P<title>.+)$"
    ),
    # 06 Artist Name - Song Title.ext  (space-separated track with artist and title)
    re.compile(
        r"^(?P<track>\d{1,3})\s+(?P<artist>.+?)\s+-\s+(?P<title>.+)$"
    ),
    # Artist Name - Song Title.ext
    re.compile(
        r"^(?P<artist>.+?)\s+-\s+(?P<title>.+)$"
    ),
    # Looser fallbacks (dashes without spaces between artist and title)
    re.compile(
        r"^(?P<track>\d{1,3})\s*-\s*(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
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
# The separator requires surrounding whitespace so hyphenated names
# ("A-ha", "AC-DC", "Wu-Tang Clan", "Blink-182") are not split mid-name.
_FOLDER_PATTERN = re.compile(
    r"^(?P<artist>.+?)\s+-\s+(?P<album>.+?)\s*\((?P<year>\d{4})\)$"
)

# Characters illegal in filenames on Linux/macOS/Windows
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$",
    re.IGNORECASE,
)


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

_COMPACT_DISC_TRACK_PREFIX = re.compile(
    r"^(?P<compact>\d{3,4})(?:[\s._-]+)(?P<title>.+)$"
)

# Ripper placeholder patterns for title, album, and artist
_PLACEHOLDER_TITLE_RE = re.compile(
    r"^(?:audio\s+)?track(?:\s*[-_.]?\s*\d+)?$"
    r"|^unknown(?:\s*(?:track|title|\d+))?$"
    r"|^untitled(?:\s*(?:track|\d+))?$",
    re.IGNORECASE,
)
_PLACEHOLDER_ALBUM_RE = re.compile(
    r"^unknown(?:\s*album)?$"
    r"|^untitled(?:\s*album)?$"
    r"|^album(?:\s+title)?$"
    r"|^title$",
    re.IGNORECASE,
)
_PLACEHOLDER_ARTIST_RE = re.compile(
    r"^(?:new|unknown|no)\s*artist$"
    r"|^artist$",
    re.IGNORECASE,
)


def is_suspicious_tag(value: str | None, field: str | None = None) -> bool:
    """Determine if a tag value is a suspicious placeholder or junk.

    Checks known blacklist values (e.g. 'Album Title', 'New Artist', '-')
    and pattern-based ripper defaults (e.g. 'Track 01', 'Audio Track 1').
    """
    if not value:
        return False
    val = value.strip().lower()
    if not val:
        return False

    # Exception: Various Artists and aliases are canonical for albumartist
    if field == "albumartist" and val in {"various artists", "va", "v.a.", "v. a.", "various"}:
        return False

    try:
        import config
        suspicious_set = getattr(config, "SUSPICIOUS_TAG_VALUES", set())
    except ImportError:
        suspicious_set = {
            "unknown artist", "unknown", "track", "untitled",
            "artist", "album artist", "no artist", "various artists",
            "album title", "album", "unknown album", "title", "new artist",
            "-", "--", "...", "?", "none", "n/a",
        }

    if val in suspicious_set:
        return True

    if field == "title":
        return bool(_PLACEHOLDER_TITLE_RE.match(val))
    elif field == "album":
        return bool(_PLACEHOLDER_ALBUM_RE.match(val))
    elif field in ("artist", "albumartist"):
        return bool(_PLACEHOLDER_ARTIST_RE.match(val))

    return bool(
        _PLACEHOLDER_TITLE_RE.match(val)
        or _PLACEHOLDER_ALBUM_RE.match(val)
        or _PLACEHOLDER_ARTIST_RE.match(val)
    )


def validate_albumartist(
    albumartist: str | None,
    artist: str | None = None,
    track: str | None = None,
) -> str | None:
    """Validate albumartist tag and return clean value, or None if bogus.

    Rejects:
    - Empty, whitespace, or blacklisted placeholder values (e.g. 'artist', 'new artist', '-', 'none')
    - URLs or domain names (e.g. 'http://...', 'www...', '.com', '.net')
    - Purely digit strings (e.g. '01', '02', '7') that leak track numbers,
      UNLESS the artist name is identical (e.g. genuine numeric bands like '666', '3', '213').

    Normalises:
    - 'VA', 'V.A.', 'Various', 'Various Artists' variants to 'Various Artists'.
    """
    if not albumartist:
        return None
    val = albumartist.strip()
    if not val:
        return None

    low = val.lower()
    _VA_ALIASES = frozenset({"va", "v.a.", "v. a.", "various", "various artists"})
    if low in _VA_ALIASES:
        return "Various Artists"

    if is_suspicious_tag(val, field="albumartist"):
        return None

    junk_tokens = {
        "artist", "unknown", "unknown artist", "album artist", "no artist",
        "new artist", "album title", "album", "unknown album", "title",
        "-", "--", "...", "?", "none", "n/a",
    }
    if low in junk_tokens:
        return None

    # URLs and web domains
    if low.startswith(("http://", "https://", "www.")) or ".com" in low or ".net" in low or ".org" in low:
        return None

    # Purely digit strings: check if artist is a genuine numeric band name
    if val.isdigit():
        clean_art = (artist or "").strip()
        if clean_art == val:
            return val
        return None

    # Strip featured artists from albumartist (albumartist is the release's main artist, never a featured credit)
    val = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", val, flags=re.IGNORECASE)[0].strip()

    return val


def validate_year(year: str | int | None) -> str | None:
    """Validate and canonicalise release year tag.

    - Accepts 4-digit years in range 1880..2026 (or ISO dates like '1998-05-12').
    - Expands 2-digit years ('98' → '1998', '04' → '2004').
    - Rejects 1-digit ('8'), 3-digit ('198'), future (>2026), and bogus years, returning None.
    """
    if year is None:
        return None
    val = str(year).strip()
    if not val:
        return None

    # Match 4-digit year (standalone or prefix of ISO date YYYY-MM-DD)
    m4 = re.match(r"^(\d{4})(?:-\d{1,2})?(?:-\d{1,2})?$", val)
    if m4:
        y = int(m4.group(1))
        if 1880 <= y <= 2026:
            return str(y)
        return None

    # Match 2-digit year
    m2 = re.match(r"^(\d{2})$", val)
    if m2:
        y = int(m2.group(1))
        if y >= 20:   # 1920..1999
            return str(1900 + y)
        elif y <= 26: # 2000..2026
            return str(2000 + y)
        return None

    return None


def artist_comparison_key(name: str | None) -> str:
    """Fold artist name for grouping and duplicate detection.

    Folds leading articles ('The', 'A', 'An'), normalises '&' to 'and',
    strips punctuation, collapses whitespace, and lowercases.
    """
    if not name:
        return ""
    s = re.sub(r"^(the|a|an)\s+", "", name.strip(), flags=re.IGNORECASE)
    s = s.replace("&", "and")
    s = re.sub(r"[.\'\"`\-_]", "", s)
    return " ".join(s.split()).lower()


def normalise_title(title: str | None) -> str:
    """Fold a release/recording/track title for catalog comparison.

    Applies NFKC normalisation, case folding, and punctuation stripping so
    typographic variants ('’' vs "'", 'pt.1' vs 'Part 1' casing) compare equal.
    """
    if not title:
        return ""
    value = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def title_case_clean(text: str | None) -> str | None:
    """Apply standard title-casing to titles/names without an MB match.

    Preserves short lowercase words ('and', 'of', 'the', etc.) unless
    at the start or end, respects 'feat.' / 'ft.', and keeps all-caps acronyms.
    """
    if not text:
        return None
    words = text.strip().split()
    if not words:
        return None

    short_words = {
        "a", "an", "the", "and", "but", "or", "for", "nor", "on", "at",
        "to", "from", "by", "of", "in", "with",
    }
    result = []
    for i, word in enumerate(words):
        # Keep acronyms / Roman numerals like REM, TOOL, ABBA, II, AC/DC
        if word.isupper() and len(word) > 1:
            result.append(word)
            continue
        # Preserve feat. / ft. / vs.
        if word.lower() in {"feat.", "ft.", "vs.", "aka"}:
            result.append(word.lower())
            continue
        # Lowercase short words unless first or last word
        if i > 0 and i < len(words) - 1 and word.lower() in short_words:
            result.append(word.lower())
        else:
            if "-" in word:
                result.append("-".join(p if any(c.isupper() for c in p[1:]) and not p.isupper() else p.capitalize() for p in word.split("-")))
            elif any(c.isupper() for c in word[1:]) and not word.isupper():
                result.append(word)
            else:
                result.append(word.capitalize())
    return " ".join(result)


def sanitise_name(value: str) -> str:
    """Clean a single tag-derived field used to build a path component.

    Field values keep meaningful trailing periods ("R.E.M.", "Dinosaur Jr."),
    because only a whole path component may not end in a period on Windows.
    Apply :func:`sanitise_component` to the assembled component.
    """
    # Unescape backslashes before quotes (e.g. B-52\'s → B-52's)
    value = re.sub(r'\\([\'"])', r'\1', value)
    # Strip ripper damage/incomplete markers
    value = _RIPPER_ANNOTATIONS.sub("", value)
    # Replace forward slash with dash (slash is path-separator on Linux/macOS)
    value = value.replace("/", "-")
    # Remove remaining illegal characters
    value = _ILLEGAL_CHARS.sub("", value)
    # Strip leading dots (prevents hidden directory/file creation on Linux/Android)
    value = value.lstrip(".")
    # Strip leading/trailing underscores from tokens (e.g. _Heroes_ → Heroes)
    value = re.sub(r'(?:^|\s)_(.+?)_(?:\s|$)', r' \1 ', value)
    # Collapse multiple spaces
    value = re.sub(r" {2,}", " ", value)
    return value.strip()


def sanitise_component(name: str) -> str:
    """Return *name* as a single path component accepted by Windows and POSIX.

    Windows rejects reserved device names and silently drops trailing periods
    and spaces, so a component ending in "." would alias a different path.
    """
    value = _ILLEGAL_CHARS.sub("", name.replace("/", "-"))
    value = value.lstrip(".").rstrip(" .")
    value = re.sub(r" {2,}", " ", value).strip()
    if _WINDOWS_RESERVED_NAME.fullmatch(value):
        value = f"_{value}"
    return value or "_"


_sanitise = sanitise_name  # kept for internal callers

def _clean_track(track: str | None) -> str | None:
    """Normalise track number — strip total tracks (e.g. '1/12' → '1')."""
    if not track:
        return None
    return track.split("/")[0].strip().lstrip("0") or "0"


def _clean_disc(disc: str | None) -> str | None:
    """Normalise disc number — strip total (e.g. '1/2' → '1')."""
    if not disc:
        return None
    num = disc.split("/")[0].strip().lstrip("0")
    return num or None


def split_compact_disc_track(number: str) -> tuple[str | None, str | None]:
    """Split compact disc-track number like ``101`` into (disc, track).

    Returns ``(None, None)`` when the value is not a supported compact form.
    """
    if not number.isdigit() or len(number) not in (3, 4):
        return (None, None)

    disc_raw = number[:-2].lstrip("0")
    track_raw = number[-2:]

    if not disc_raw:
        return (None, None)

    try:
        track_num = int(track_raw)
    except ValueError:
        return (None, None)

    if track_num <= 0:
        return (None, None)

    return (disc_raw, str(track_num))


def parse_compact_disc_track_candidate(path: Path) -> dict[str, str | None]:
    """Parse compact disc-track prefix from filename stem when present.

    Example: ``101-Track Title.mp3`` -> ``{"disc": "1", "track": "1", ...}``.
    """
    stem = path.stem.strip()
    m = _COMPACT_DISC_TRACK_PREFIX.match(stem)
    if not m:
        return {
            "compact": None,
            "disc": None,
            "track": None,
            "title": None,
        }

    compact = m.group("compact")
    disc, track = split_compact_disc_track(compact)
    if not disc or not track:
        return {
            "compact": compact,
            "disc": None,
            "track": None,
            "title": m.group("title").strip() or None,
        }

    return {
        "compact": compact,
        "disc": disc,
        "track": track,
        "title": m.group("title").strip() or None,
    }


def parse_filename(path: Path) -> dict[str, str | None]:
    """Parse an audio filename into ``{artist, title, track, disc}`` (any may be None)."""
    stem = path.stem
    for pat in _PATTERNS:
        m = pat.match(stem)
        if m:
            groups = m.groupdict()
            art = groups.get("artist", "").strip() or None
            tit = groups.get("title", "").strip() or None
            return {
                "artist": sanitise_name(art) if art else None,
                "title":  sanitise_name(tit) if tit else None,
                "track":  _clean_track(groups.get("track")),
                "disc":   _clean_disc(groups.get("disc")),
            }
    # Fallback — treat the whole stem as title
    clean_stem = sanitise_name(stem.strip()) if stem.strip() else None
    return {"artist": None, "title": clean_stem, "track": None, "disc": None}


_FOLDER_YEAR_ALBUM = re.compile(
    r"^(?P<year>\d{4})\s*-\s*(?P<album>.+)$"
)
_FOLDER_ALBUM_YEAR = re.compile(
    r"^(?P<album>.+?)\s*\((?P<year>\d{4})\)$"
)
_FOLDER_ARTIST_ALBUM = re.compile(
    r"^(?P<artist>.+?)\s+-\s+(?P<album>.+)$"
)


def parse_folder_name(name: str) -> dict[str, str | None]:
    """Parse folder name into ``{artist, album, year}``.

    Supports:
    - ``Artist - Album (Year)``
    - ``Year - Album`` (e.g. ``2010 - Spiral Shadow``)
    - ``Album (Year)`` (e.g. ``Spiral Shadow (2010)``)
    - ``Artist - Album`` (e.g. ``Kylesa - Spiral Shadow``)
    """
    s = name.strip()
    m = _FOLDER_PATTERN.match(s)
    if m:
        return {
            "artist": m.group("artist").strip(),
            "album":  m.group("album").strip(),
            "year":   m.group("year"),
        }
    m = _FOLDER_YEAR_ALBUM.match(s)
    if m:
        return {
            "artist": None,
            "album":  m.group("album").strip(),
            "year":   m.group("year"),
        }
    m = _FOLDER_ALBUM_YEAR.match(s)
    if m:
        return {
            "artist": None,
            "album":  m.group("album").strip(),
            "year":   m.group("year"),
        }
    m = _FOLDER_ARTIST_ALBUM.match(s)
    if m:
        art = m.group("artist").strip()
        if not (art.isdigit() and len(art) == 4):
            return {
                "artist": art,
                "album":  m.group("album").strip(),
                "year":   None,
            }
    return {"artist": None, "album": None, "year": None}


def build_filename(
    track: str | None,
    artist: str | None,
    title: str | None,
    ext: str = ".mp3",
    disc: str | None = None,
) -> str:
    """Build the canonical filename.

    Single-disc:  ``01 - Artist Name - Song Title.ext``
    Multi-disc:   ``1-01 - Artist Name - Song Title.ext``

    Falls back gracefully when fields are missing.
    Sanitises illegal filename characters from all fields.
    """
    parts: list[str] = []

    if track is not None:
        clean_track = _clean_track(track)
        if clean_track:
            clean_disc = _clean_disc(disc) if disc is not None else None
            if clean_disc:
                parts.append(f"{clean_disc}-{clean_track.zfill(2)}")
            else:
                parts.append(clean_track.zfill(2))

    if artist:
        parts.append(_sanitise(artist))

    if title:
        parts.append(_sanitise(title))

    if not parts:
        return f"unknown{ext}"

    return sanitise_component(" - ".join(parts) + ext)

"""Genre normalisation, merge logic, and parent lookup from genre-tree.txt."""

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Normalisation map — add all spelling / alias fixes here, never inline.
# ---------------------------------------------------------------------------
GENRE_NORMALISE: dict[str, str] = {
    "hiphop": "Hip-Hop",
    "hip hop": "Hip-Hop",
    "hip-hop/rap": "Hip-Hop",
    "rap": "Hip-Hop",
    "rnb": "R&B",
    "r&b": "R&B",
    "rhythm and blues": "R&B",
    "drum n bass": "Drum & Bass",
    "dnb": "Drum & Bass",
    "drum and bass": "Drum & Bass",
    "d&b": "Drum & Bass",
    "triphop": "Trip-Hop",
    "trip hop": "Trip-Hop",
    "post punk": "Post-Punk",
    "post rock": "Post-Rock",
    "synth pop": "Synth-Pop",
    "synthpop": "Synth-Pop",
    "idm": "IDM",
    "ebm": "EBM",
    "edm": "Electronic",
    "electronica": "Electronic",
    "singer songwriter": "Singer-Songwriter",
    "singer/songwriter": "Singer-Songwriter",
    "disco": "Disco",
    "chill": "Chill",
    "chillout": "Chill",
    "estonian": "Estonian",
    "soundtracks": "Soundtrack",
    "ost": "Soundtrack",
    "rocknroll": "Rock & Roll",
    "rock n roll": "Rock & Roll",
    "rock 'n' roll": "Rock & Roll",
    "rock and roll": "Rock & Roll",
    "alt. rock": "Alternative Rock",
    "alt rock": "Alternative Rock",
    "general alternative": "Alternative Rock",
    "alternative & punk": "Rock / Punk",
    "alternative punk": "Punk Rock",
    "melodic death metal": "Death Metal",
    "jangle pop": "Indie Pop",
    "neo-psychedelia": "Psychedelic Rock",
    "acid jazz": "Acid Jazz",
    "acid house": "House",
    "euro house": "Eurodance",
    "chiptune": "Chiptune",
    "blues rock": "Blues Rock",
    "country & folk": "Folk / Country",
}

# ---------------------------------------------------------------------------
# Blacklist — MB tags that are meaningless as genres, silently discarded.
# Add to this list as you encounter more junk tags from MusicBrainz.
# ---------------------------------------------------------------------------
GENRE_BLACKLIST: set[str] = {
    "other",
    "unknown",
    "miscellaneous",
    "misc",
    "seen live",
    "favorites",
    "favourite",
    "good",
    "audio",
    "music",
    "track",
    "untitled",
    "none",
    "(none)",
    "n/a",
    "general",
    "all",
    "default",
    "genre",
    "genres",
    "songsdl.com",
    "compilation",
    "compilations",
    "various",
    "harmony in my head",
    "retro",
    "ensembles",
    "traditional",
    "instrumental",
    "vocal",
    "50s",
    "60s",
    "70s",
    "80s",
    "90s",
    "00s",
    "2000s",
    "10s",
    "2010s",
}

# ---------------------------------------------------------------------------
# Genre tree — CHILD_TO_PARENT and CANONICAL_GENRES from genre-tree.txt
# ---------------------------------------------------------------------------
CHILD_TO_PARENT: dict[str, str] = {}
CANONICAL_GENRES: dict[str, str] = {}

_TREE_PATH = Path(__file__).resolve().parent.parent / "genre-tree.txt"


def _parse_genre_tree(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Parse indented genre-tree.txt into ``({child_lower: Parent}, {name_lower: Canonical})``."""
    mapping: dict[str, str] = {}
    canonical: dict[str, str] = {}
    current_parent: str | None = None

    if not path.exists():
        return mapping, canonical

    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        canonical[stripped.lower()] = stripped
        if raw.startswith("  "):
            # indented → child of current parent
            if current_parent is not None:
                mapping[stripped.lower()] = current_parent
        else:
            current_parent = stripped
    return mapping, canonical


_child_map, _canon_map = _parse_genre_tree(_TREE_PATH)
CHILD_TO_PARENT.update(_child_map)
CANONICAL_GENRES.update(_canon_map)

# Root-level genres are those that appear as parents but never as children.
ROOT_GENRES: set[str] = set(CHILD_TO_PARENT.values())


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def smart_genre_title(s: str) -> str:
    """Format an unknown genre token with clean title-casing while preserving acronyms."""
    cleaned = s.strip()
    if not cleaned:
        return ""
    if cleaned.isupper() and len(cleaned) <= 5:
        return cleaned
    parts = re.split(r"([ -])", cleaned)
    out = []
    for p in parts:
        if p in (" ", "-"):
            out.append(p)
        elif p.lower() in ("and", "of", "the", "in", "a"):
            out.append(p.lower())
        else:
            out.append(p.capitalize())
    res = "".join(out)
    return res[0].upper() + res[1:] if res else ""


def normalise_genre(genre: str) -> str:
    """Normalise a single genre string via the alias map, canonical tree casing,
    preserving case/formatting for unknown genres."""
    cleaned = genre.strip()
    key = cleaned.lower()
    if key in GENRE_NORMALISE:
        return GENRE_NORMALISE[key]
    if key in CANONICAL_GENRES:
        return CANONICAL_GENRES[key]
    return smart_genre_title(cleaned)


def is_meaningful_genre(genre: str | None) -> bool:
    """Return True when *genre* is non-empty and not blacklisted."""
    if not genre:
        return False
    normed = normalise_genre(genre)
    return bool(normed) and normed.lower() not in GENRE_BLACKLIST


def has_meaningful_genres(genre_value: str | None) -> bool:
    """Return True when a slash-separated genre tag contains any valid genre."""
    return any(
        is_meaningful_genre(part.strip())
        for part in (genre_value or "").split("/")
        if part.strip()
    )


def parent_genre(genre: str) -> str | None:
    """Return the root parent for *genre*, or ``None`` if already root / unknown."""
    return CHILD_TO_PARENT.get(genre.strip().lower())


def merge_genres(folder_genre: str | None, mb_genres: list[str]) -> str:
    """Build the final slash-separated genre string.

    1. *folder_genre* (if any) always comes first.
    2. MusicBrainz genres appended in order, blacklisted terms discarded.
    3. Deduplicated case-insensitively.
    4. Joined with `` / ``.
    """
    seen: set[str] = set()
    result: list[str] = []

    def _add(g: str) -> None:
        normed = normalise_genre(g)
        if not is_meaningful_genre(normed):
            return
        key = normed.lower()
        if key not in seen:
            seen.add(key)
            result.append(normed)

    if folder_genre:
        _add(folder_genre)

    for g in mb_genres:
        _add(g)

    return " / ".join(result)
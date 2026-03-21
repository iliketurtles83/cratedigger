"""Genre normalisation, merge logic, and parent lookup from genre-tree.txt."""

from pathlib import Path

# ---------------------------------------------------------------------------
# Normalisation map — add all spelling / alias fixes here, never inline.
# ---------------------------------------------------------------------------
GENRE_NORMALISE: dict[str, str] = {
    "hiphop": "Hip-Hop",
    "hip hop": "Hip-Hop",
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
}

# ---------------------------------------------------------------------------
# Genre tree — CHILD_TO_PARENT built by parsing genre-tree.txt
# ---------------------------------------------------------------------------
CHILD_TO_PARENT: dict[str, str] = {}

_TREE_PATH = Path(__file__).resolve().parent.parent / "genre-tree.txt"


def _parse_genre_tree(path: Path) -> dict[str, str]:
    """Parse indented genre-tree.txt into ``{child_lower: Parent}`` mapping."""
    mapping: dict[str, str] = {}
    current_parent: str | None = None

    if not path.exists():
        return mapping

    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw.startswith("  "):
            # indented → child of current parent
            if current_parent is not None:
                mapping[stripped.lower()] = current_parent
        else:
            current_parent = stripped
    return mapping


CHILD_TO_PARENT.update(_parse_genre_tree(_TREE_PATH))

# Root-level genres are those that appear as parents but never as children.
ROOT_GENRES: set[str] = set(CHILD_TO_PARENT.values())


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def normalise_genre(genre: str) -> str:
    """Normalise a single genre string via the alias map, preserving case
    for unknown genres."""
    key = genre.strip().lower()
    return GENRE_NORMALISE.get(key, genre.strip())


def parent_genre(genre: str) -> str | None:
    """Return the root parent for *genre*, or ``None`` if already root / unknown."""
    return CHILD_TO_PARENT.get(genre.strip().lower())


def merge_genres(folder_genre: str | None, mb_genres: list[str]) -> str:
    """Build the final slash-separated genre string.

    1. *folder_genre* (if any) always comes first.
    2. MusicBrainz genres appended in order.
    3. Deduplicated case-insensitively.
    4. Joined with `` / ``.
    """
    seen: set[str] = set()
    result: list[str] = []

    def _add(g: str) -> None:
        normed = normalise_genre(g)
        key = normed.lower()
        if key not in seen:
            seen.add(key)
            result.append(normed)

    if folder_genre:
        _add(folder_genre)

    for g in mb_genres:
        _add(g)

    return " / ".join(result)

"""MusicBrainz / AcoustID lookup with rate limiting.

Degrades gracefully when pyacoustid is not installed or ACOUSTID_API_KEY
is not set — falls back to folder-genre-only tagging.
"""

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY", "")
MB_USER_AGENT_EMAIL = os.getenv("MB_USER_AGENT_EMAIL", "")

# Try importing pyacoustid — optional dependency
try:
    import acoustid  # type: ignore[import-untyped]
    _HAS_ACOUSTID = True
except ImportError:
    _HAS_ACOUSTID = False
    log.warning("pyacoustid not installed — fingerprint lookups disabled")

# Try importing musicbrainzngs — optional dependency
try:
    import musicbrainzngs  # type: ignore[import-untyped]
    _HAS_MB = True
    musicbrainzngs.set_useragent("cratedigger", "0.1", MB_USER_AGENT_EMAIL)
except ImportError:
    _HAS_MB = False
    log.warning("musicbrainzngs not installed — MB metadata lookups disabled")


_last_mb_call: float = 0.0


def _rate_limit(min_seconds: float) -> None:
    """Block until at least *min_seconds* since the last MB API call."""
    global _last_mb_call
    elapsed = time.monotonic() - _last_mb_call
    if elapsed < min_seconds:
        time.sleep(min_seconds - elapsed)
    _last_mb_call = time.monotonic()


def _release_sort_key(release: dict[str, object]) -> tuple[str, str, str]:
    """Stable key for deterministic release fallback selection."""
    date = str(release.get("date") or "")
    title = str(release.get("title") or "")
    release_id = str(release.get("id") or "")
    # Prefer dated releases first; then earliest date lexicographically.
    return ("1" if not date else "0", date, f"{title}|{release_id}")


def _release_albumartist(release: dict[str, object]) -> str | None:
    """Extract release-level album artist from artist-credit."""
    artist_credit = release.get("artist-credit", [])
    if not isinstance(artist_credit, list) or not artist_credit:
        return None
    first_credit = artist_credit[0]
    if isinstance(first_credit, dict):
        artist_obj = first_credit.get("artist", {})
        if isinstance(artist_obj, dict):
            name = artist_obj.get("name")
            return str(name) if name else None
    return None


def _find_recording_track_number(release: dict[str, object], recording_id: str) -> str | None:
    """Return track number whose embedded recording id matches recording_id."""
    media = release.get("medium-list", [])
    if not isinstance(media, list):
        return None

    for medium in media:
        if not isinstance(medium, dict):
            continue
        tracks = medium.get("track-list", [])
        if not isinstance(tracks, list):
            continue
        for track in tracks:
            if not isinstance(track, dict):
                continue
            rec_obj = track.get("recording")
            if isinstance(rec_obj, dict) and rec_obj.get("id") == recording_id:
                number = track.get("number")
                return str(number) if number else None
            if track.get("recording-id") == recording_id:
                number = track.get("number")
                return str(number) if number else None

    return None


# ---------------------------------------------------------------------------
# AcoustID fingerprint lookup
# ---------------------------------------------------------------------------

def fingerprint_lookup(
    path: Path,
    *,
    min_score: float = 0.8,
    rate_limit_seconds: float = 1.1,
    existing_artist: str | None = None,
    existing_title: str | None = None,
) -> dict[str, str | None]:
    """Fingerprint *path* via AcoustID and return best-match metadata."""
    empty: dict[str, str | None] = {
        "recording_id": None, "title": None, "artist": None,
    }

    if not _HAS_ACOUSTID:
        log.warning("pyacoustid unavailable — skipping fingerprint for %s", path.name)
        return empty

    if not ACOUSTID_API_KEY:
        log.warning("ACOUSTID_API_KEY not set — skipping fingerprint for %s", path.name)
        return empty

    _rate_limit(rate_limit_seconds)

    try:
        results = acoustid.match(
            ACOUSTID_API_KEY, str(path),
            meta="recordings",
            parse=False,
        )
    except acoustid.WebServiceError as exc:
        log.error("AcoustID error for %s: %s", path.name, exc)
        return empty
    except Exception as exc:
        log.error("Fingerprint error for %s: %s", path.name, exc)
        return empty

    if not results or results.get("status") != "ok":
        log.warning("AcoustID returned non-ok status for %s: %s", path.name, results)
        return empty

    log.debug("AcoustID results for %s: %d results found", path.name, len(results.get("results", [])))

    # Collect all qualifying recordings across results, then prefer those
    # that match existing artist/title tags for disambiguation.
    candidates: list[dict] = []
    for res in results.get("results", []):
        score = res.get("score", 0)
        log.debug("  Result score: %.2f (threshold: %.2f)", score, min_score)
        if score < min_score:
            continue
        for rec in res.get("recordings", []):
            candidates.append(rec)

    if not candidates:
        log.warning("No matches above threshold for %s", path.name)
        return empty

    def _match_score(rec: dict) -> int:
        s = 0
        if existing_artist:
            artists = rec.get("artists", [])
            ra = (artists[0].get("name") or "") if artists else ""
            if ra.lower() == existing_artist.lower():
                s += 1
        if existing_title:
            if (rec.get("title") or "").lower() == existing_title.lower():
                s += 1
        return s

    if existing_artist or existing_title:
        candidates.sort(key=_match_score, reverse=True)

    best_rec = candidates[0]
    title = best_rec.get("title")
    artists = best_rec.get("artists", [])
    artist = artists[0].get("name") if artists else None
    log.info("Match found: %s by %s", title, artist)
    return {
        "recording_id": best_rec.get("id"),
        "title": title,
        "artist": artist,
    }


# ---------------------------------------------------------------------------
# MusicBrainz genre tags for a recording
# ---------------------------------------------------------------------------

def mb_genres(
    recording_id: str,
    *,
    min_votes: int = 2,
    max_genres: int = 5,
    rate_limit_seconds: float = 1.1,
) -> list[str]:
    """Fetch genre tags from MusicBrainz for *recording_id*.

    Only returns tags with vote count >= *min_votes*, capped at *max_genres*.
    """
    if not _HAS_MB:
        log.warning("musicbrainzngs unavailable — skipping MB genre lookup")
        return []

    if not recording_id:
        return []

    _rate_limit(rate_limit_seconds)

    try:
        data = musicbrainzngs.get_recording_by_id(
            recording_id, includes=["tags"]
        )
    except Exception as exc:
        log.error("MB lookup error for %s: %s", recording_id, exc)
        return []

    recording = data.get("recording", {})
    tag_list = recording.get("tag-list", [])

    # Filter by vote count and sort descending
    qualified = [
        t for t in tag_list
        if int(t.get("count", 0)) >= min_votes
    ]
    qualified.sort(key=lambda t: int(t.get("count", 0)), reverse=True)

    return [t["name"] for t in qualified[:max_genres]]


# ---------------------------------------------------------------------------
# MusicBrainz recording metadata (album, year, track, albumartist, isrc)
# ---------------------------------------------------------------------------

def mb_recording_metadata(
    recording_id: str,
    *,
    rate_limit_seconds: float = 1.1,
) -> dict[str, str | None]:
    """Fetch album, year, track, albumartist, and isrc from MusicBrainz.

    Returns ``{album, year, track, albumartist, isrc}`` — values ``None``
    when unavailable.
    """
    result: dict[str, str | None] = {
        "album": None, "year": None, "track": None,
        "albumartist": None, "isrc": None,
    }

    if not _HAS_MB or not recording_id:
        return result

    _rate_limit(rate_limit_seconds)

    try:
        data = musicbrainzngs.get_recording_by_id(
            recording_id, includes=["releases", "isrcs"]
        )
    except Exception as exc:
        log.error("MB metadata error for %s: %s", recording_id, exc)
        return result

    recording = data.get("recording", {})

    # ISRC from recording level
    isrc_list = recording.get("isrc-list", [])
    if isrc_list:
        result["isrc"] = isrc_list[0]

    releases = recording.get("release-list", [])
    if not releases:
        return result

    # Prefer the earliest-dated release where we can match the recording in
    # track lists.  Sort first so that when multiple releases have a track
    # match, the earliest-dated one wins rather than an arbitrary API order.
    dict_releases = [r for r in releases if isinstance(r, dict)]
    if not dict_releases:
        return result
    sorted_releases = sorted(dict_releases, key=_release_sort_key)

    selected_release: dict[str, object] | None = None
    selected_track: str | None = None
    for release in sorted_releases:
        track_number = _find_recording_track_number(release, recording_id)
        if track_number:
            selected_release = release
            selected_track = track_number
            break

    if selected_release is None:
        selected_release = sorted_releases[0]

    result["album"] = str(selected_release.get("title") or "") or None
    date = str(selected_release.get("date") or "")
    result["year"] = date[:4] or None
    result["albumartist"] = _release_albumartist(selected_release)
    result["track"] = selected_track

    return result

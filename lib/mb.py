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


# ---------------------------------------------------------------------------
# AcoustID fingerprint lookup
# ---------------------------------------------------------------------------

def fingerprint_lookup(
    path: Path,
    *,
    min_score: float = 0.8,
    rate_limit_seconds: float = 1.1,
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

    for res in results.get("results", []):
        score = res.get("score", 0)
        log.debug("  Result score: %.2f (threshold: %.2f)", score, min_score)
        if score < min_score:
            continue
        for rec in res.get("recordings", []):
            title = rec.get("title")
            artists = rec.get("artists", [])
            artist = artists[0].get("name") if artists else None
            log.info("Match found: %s by %s (score: %.2f)", title, artist, score)
            return {
                "recording_id": rec.get("id"),
                "title": title,
                "artist": artist,
            }

    log.warning("No matches above threshold for %s", path.name)
    return empty


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

    release = releases[0]
    result["album"] = release.get("title")
    result["year"] = release.get("date", "")[:4] or None

    # Album artist from the release artist-credit
    artist_credit = release.get("artist-credit", [])
    if artist_credit:
        first_credit = artist_credit[0]
        if isinstance(first_credit, dict):
            artist_obj = first_credit.get("artist", {})
            result["albumartist"] = artist_obj.get("name")

    # Track number from the medium list
    media = release.get("medium-list", [])
    if media:
        tracks = media[0].get("track-list", [])
        if tracks:
            result["track"] = tracks[0].get("number")

    return result

"""MusicBrainz / AcoustID lookup with rate limiting.

Degrades gracefully when pyacoustid is not installed or ACOUSTID_API_KEY
is not set — falls back to folder-genre-only tagging.
"""

import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

try:
    import config
except ImportError:
    config = None  # type: ignore[assignment]

load_dotenv()

log = logging.getLogger(__name__)

ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY", "")
MB_USER_AGENT_EMAIL = os.getenv("MB_USER_AGENT_EMAIL", "")

# Try importing pyacoustid — optional dependency
acoustid = None
try:
    import acoustid  # type: ignore[import-untyped]
    _HAS_ACOUSTID = True
except ImportError:
    _HAS_ACOUSTID = False
    log.warning("pyacoustid not installed — fingerprint lookups disabled")

# Try importing musicbrainzngs — optional dependency
musicbrainzngs = None
try:
    import musicbrainzngs  # type: ignore[import-untyped]
    _HAS_MB = True
    musicbrainzngs.set_useragent("cratedigger", "0.1", MB_USER_AGENT_EMAIL)
except ImportError:
    _HAS_MB = False
    log.warning("musicbrainzngs not installed — MB metadata lookups disabled")


_last_mb_call: float = 0.0
_adaptive_mb_delay: float = 0.0
MB_MAX_RETRIES = int(getattr(config, "MB_MAX_RETRIES", 3))
MB_BACKOFF_BASE = float(getattr(config, "MB_BACKOFF_BASE", 2.0))
MB_ADAPTIVE_MAX_DELAY = float(getattr(config, "MB_ADAPTIVE_MAX_DELAY", 8.0))
MB_MAX_QUEUE_SECONDS = float(getattr(config, "MB_MAX_QUEUE_SECONDS", 20.0))


def _rate_limit(min_seconds: float) -> bool:
    """Block until enough spacing has elapsed for the next MB request.

    Returns False when bounded queueing rejects the request.
    """
    global _last_mb_call
    effective_delay = max(min_seconds, _adaptive_mb_delay)
    elapsed = time.monotonic() - _last_mb_call
    wait = max(0.0, effective_delay - elapsed)

    if wait > MB_MAX_QUEUE_SECONDS:
        log.warning(
            "Skipping MB request because queued wait %.2fs exceeds limit %.2fs",
            wait,
            MB_MAX_QUEUE_SECONDS,
        )
        return False

    if wait > 0:
        time.sleep(wait)
    _last_mb_call = time.monotonic()
    return True


def _record_mb_success(min_seconds: float) -> None:
    """Gradually relax adaptive delay after a successful MB call."""
    global _adaptive_mb_delay
    floor = max(0.0, min_seconds)
    if _adaptive_mb_delay <= floor:
        _adaptive_mb_delay = floor
        return
    _adaptive_mb_delay = max(floor, _adaptive_mb_delay * 0.8)


def _record_mb_failure(min_seconds: float, attempt: int) -> None:
    """Increase adaptive delay after transient MB failures."""
    global _adaptive_mb_delay
    floor = max(0.0, min_seconds)
    candidate = max(floor, floor * (MB_BACKOFF_BASE ** (attempt + 1)))
    _adaptive_mb_delay = min(MB_ADAPTIVE_MAX_DELAY, max(_adaptive_mb_delay, candidate))


def _mb_call_with_retry(fn, *args, rate_limit_seconds: float = 1.1, **kwargs):
    """Call a MusicBrainz client function with retry/backoff on transient failures."""
    if not _HAS_MB or musicbrainzngs is None:
        return None

    transient_errors = (musicbrainzngs.WebServiceError, musicbrainzngs.NetworkError)

    for attempt in range(MB_MAX_RETRIES + 1):
        try:
            if not _rate_limit(rate_limit_seconds):
                return None
            response = fn(*args, **kwargs)
            _record_mb_success(rate_limit_seconds)
            return response
        except transient_errors as exc:
            _record_mb_failure(rate_limit_seconds, attempt)
            if attempt >= MB_MAX_RETRIES:
                log.error("MB call failed after %d retries: %s", MB_MAX_RETRIES, exc)
                return None

            backoff = MB_BACKOFF_BASE ** attempt
            log.warning(
                "Transient MB error (%s). Retrying in %.2fs (%d/%d)",
                exc,
                backoff,
                attempt + 1,
                MB_MAX_RETRIES,
            )
            time.sleep(backoff)


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

    if not _HAS_ACOUSTID or acoustid is None:
        log.warning("pyacoustid unavailable — skipping fingerprint for %s", path.name)
        return empty

    acoustid_client = acoustid

    if not ACOUSTID_API_KEY:
        log.warning("ACOUSTID_API_KEY not set — skipping fingerprint for %s", path.name)
        return empty

    if not _rate_limit(rate_limit_seconds):
        return empty

    try:
        results = acoustid_client.match(
            ACOUSTID_API_KEY, str(path),
            meta="recordings",
            parse=False,
        )
    except acoustid_client.WebServiceError as exc:
        log.error("AcoustID error for %s: %s", path.name, exc)
        return empty
    except Exception as exc:
        log.error("Fingerprint error for %s: %s", path.name, exc)
        return empty

    if not isinstance(results, dict) or results.get("status") != "ok":
        log.warning("AcoustID returned non-ok status for %s: %s", path.name, results)
        return empty

    log.debug("AcoustID results for %s: %d results found", path.name, len(results.get("results", [])))

    # Collect all qualifying recordings across results, then prefer those
    # that match existing artist/title tags for disambiguation.
    candidates: list[dict] = []
    best_score = 0.0
    for res in results.get("results", []):
        score = float(res.get("score", 0) or 0)
        if score > best_score:
            best_score = score
        log.debug("  Result score: %.2f (threshold: %.2f)", score, min_score)
        if score < min_score:
            continue
        for rec in res.get("recordings", []):
            candidates.append(rec)

    if not candidates:
        if best_score > 0:
            log.info(
                "No AcoustID matches above threshold for %s (best=%.2f, threshold=%.2f); "
                "tag-based MB fallback eligible",
                path.name,
                best_score,
                min_score,
            )
        else:
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


def mb_recording_search(
    artist: str,
    title: str,
    *,
    genre_hints: list[str] | None = None,
    rate_limit_seconds: float = 1.1,
) -> dict[str, str | None]:
    """Search MusicBrainz by artist/title and return best-match recording metadata."""
    empty: dict[str, str | None] = {
        "recording_id": None,
        "title": None,
        "artist": None,
    }

    if not _HAS_MB or musicbrainzngs is None:
        log.warning("musicbrainzngs unavailable — skipping MB recording search")
        return empty

    mb_client = musicbrainzngs

    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return empty

    data = _mb_call_with_retry(
        mb_client.search_recordings,
        artist=artist,
        recording=title,
        limit=5,
        rate_limit_seconds=rate_limit_seconds,
    )
    if data is None:
        log.error("MB recording search exhausted retries for %r - %r", artist, title)
        return empty

    recordings = data.get("recording-list", [])
    if not isinstance(recordings, list) or not recordings:
        return empty

    def _match_score(rec: dict) -> int:
        score = 0
        rec_title = str(rec.get("title") or "")
        if rec_title.lower() == title.lower():
            score += 2

        credits = rec.get("artist-credit", [])
        if isinstance(credits, list) and credits:
            first = credits[0]
            if isinstance(first, dict):
                rec_artist_obj = first.get("artist", {})
                if isinstance(rec_artist_obj, dict):
                    rec_artist = str(rec_artist_obj.get("name") or "")
                    if rec_artist.lower() == artist.lower():
                        score += 2
        return score

    top_score = max(_match_score(rec) for rec in recordings)
    tied = [rec for rec in recordings if _match_score(rec) == top_score]

    hint_set = {
        str(genre).strip().lower()
        for genre in (genre_hints or [])
        if str(genre).strip()
    }

    def _recording_tag_summary(recording_id: str) -> tuple[set[str], int]:
        if not recording_id:
            return set(), 0
        details = _mb_call_with_retry(
            mb_client.get_recording_by_id,
            recording_id,
            includes=["tags"],
            rate_limit_seconds=rate_limit_seconds,
        )
        if details is None:
            return set(), 0
        recording = details.get("recording", {})
        tag_list = recording.get("tag-list", [])
        names: set[str] = set()
        votes = 0
        for tag in tag_list:
            if not isinstance(tag, dict):
                continue
            name = str(tag.get("name") or "").strip().lower()
            if not name:
                continue
            names.add(name)
            votes += int(tag.get("count", 0) or 0)
        return names, votes

    best = None
    best_key: tuple[int, int, int, str, str] | None = None
    for rec in tied:
        rec_id = str(rec.get("id") or "")
        rec_title = str(rec.get("title") or "")
        rec_tags, rec_votes = _recording_tag_summary(rec_id)
        overlap = len(hint_set & rec_tags) if hint_set else 0
        key = (
            _match_score(rec),
            overlap,
            rec_votes,
            rec_title.lower(),
            rec_id,
        )
        if best is None or best_key is None or key > best_key:
            best = rec
            best_key = key

    if best is None:
        return empty

    best_title = str(best.get("title") or "") or None
    best_artist = None
    credits = best.get("artist-credit", [])
    if isinstance(credits, list) and credits:
        first = credits[0]
        if isinstance(first, dict):
            rec_artist_obj = first.get("artist", {})
            if isinstance(rec_artist_obj, dict):
                name = rec_artist_obj.get("name")
                best_artist = str(name) if name else None

    return {
        "recording_id": str(best.get("id") or "") or None,
        "title": best_title,
        "artist": best_artist,
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
    if not _HAS_MB or musicbrainzngs is None:
        log.warning("musicbrainzngs unavailable — skipping MB genre lookup")
        return []

    mb_client = musicbrainzngs

    if not recording_id:
        return []

    data = _mb_call_with_retry(
        mb_client.get_recording_by_id,
        recording_id,
        includes=["tags"],
        rate_limit_seconds=rate_limit_seconds,
    )
    if data is None:
        log.error("MB genre lookup exhausted retries for %s", recording_id)
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

    if not _HAS_MB or musicbrainzngs is None or not recording_id:
        return result

    mb_client = musicbrainzngs

    data = _mb_call_with_retry(
        mb_client.get_recording_by_id,
        recording_id,
        includes=["releases", "isrcs"],
        rate_limit_seconds=rate_limit_seconds,
    )
    if data is None:
        log.error("MB metadata lookup exhausted retries for %s", recording_id)
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

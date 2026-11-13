#!/usr/bin/env python3
"""Repair misidentified tracks using cached AcoustID fingerprint evidence.

Scans Tier 1 artist folders for tracks whose title and album are both absent
from that artist's cached MusicBrainz catalogs, then classifies each one from
fingerprint evidence:

- ok: the fingerprint confirms the artist and the stored title
- title: the fingerprint confirms the artist and supplies a corrected title
  that is present in the artist's cached recording catalog
- misattributed: the fingerprint names a different artist
- unmatched: AcoustID returned no usable match
- unknown: no fingerprint evidence is cached for the track yet

Fingerprints are fetched only during dry-runs (with ``--online-fingerprints``)
and cached by audio hash, so a reviewed live run can never acquire new
evidence. Dry-run is always the default; both ``--fix`` and ``--no-dry-run``
are required to write tags or move files. Relocating misattributed audio also
requires ``--relocate``.

Usage:
    python scripts/repair_track_identity.py --online-fingerprints
    python scripts/repair_track_identity.py --fix
    python scripts/repair_track_identity.py --fix --relocate --no-dry-run
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.audiohash import audio_hash
from lib.context import artist_letter_bucket, is_letter_bucket
from lib.mb import fingerprint_lookup
from lib.parsers import (
    artist_comparison_key,
    normalise_title,
    sanitise_component,
    sanitise_name,
)
from lib.path_updates import record_folder_move, update_downstream_paths
from lib.tags import read_tags, write_tags

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logging.getLogger("musicbrainzngs").setLevel(logging.WARNING)
log = logging.getLogger("repair_track_identity")

# MusicBrainz placeholder titles that must never be baked into tags.
PLACEHOLDER_TITLES = {"[unknown]", "[untitled]", "[silence]", "[data track]"}

# Fraction of an album folder's tracks that must agree on one foreign artist
# before the folder relocates as a unit rather than track by track.
ALBUM_RELOCATION_RATIO = 0.6


@dataclass
class TrackCandidate:
    path: Path
    artist_folder: Path
    expected_artist: str
    artist_mb_id: str
    title: str
    album: str
    digest: str


@dataclass
class TrackDecision:
    candidate: TrackCandidate
    action: str
    reason: str
    fingerprint_artist: str = ""
    fingerprint_title: str = ""
    recording_id: str = ""


@dataclass
class ArtistCatalogs:
    mb_id: str
    releases: set[str] = field(default_factory=set)
    recordings: set[str] = field(default_factory=set)

    @property
    def usable(self) -> bool:
        """Only a recording catalog can account for individual track titles."""
        return bool(self.recordings)


def get_identity_cache_path() -> Path:
    return Path(
        getattr(
            config,
            "ACOUSTID_IDENTITY_CACHE",
            Path.home()
            / ".cache"
            / "cratedigger"
            / "acoustid-track-identity.json",
        )
    )


def _load_catalog_cache(cache_path: Path) -> dict[str, set[str]]:
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        log.warning("Could not load MusicBrainz catalog cache %s: %s", cache_path.name, exc)
        return {}
    if not isinstance(payload, dict):
        log.warning("Ignoring invalid MusicBrainz catalog cache %s", cache_path.name)
        return {}
    return {
        str(artist_id): {normalise_title(str(title)) for title in titles}
        for artist_id, titles in payload.items()
        if isinstance(titles, list)
    }


def load_identity_cache(cache_path: Path) -> dict[str, dict | None]:
    """Load fingerprint evidence keyed by audio hash."""
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        log.warning("Could not load AcoustID identity cache: %s", exc)
        return {}
    if not isinstance(payload, dict):
        log.warning("Ignoring invalid AcoustID identity cache")
        return {}
    return {
        str(key): value
        for key, value in payload.items()
        if value is None or isinstance(value, dict)
    }


def save_identity_cache(cache_path: Path, cache: dict[str, dict | None]) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(dict(sorted(cache.items())), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not save AcoustID identity cache: %s", exc)


def iter_artist_folders(root: Path) -> list[Path]:
    """Return every Tier 1 artist folder under a letter bucket."""
    folders: list[Path] = []
    for bucket in sorted(root.iterdir()):
        if not bucket.is_dir() or not is_letter_bucket(bucket.name):
            continue
        folders.extend(sorted(path for path in bucket.iterdir() if path.is_dir()))
    return folders


def _folder_identity_counts(folder: Path) -> tuple[Counter[str], Counter[str]]:
    """Count album-artist spellings and MusicBrainz IDs inside one folder."""
    names: Counter[str] = Counter()
    ids: Counter[str] = Counter()
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(path)
        if not tags:
            continue
        albumartist = (tags.get("albumartist") or "").strip()
        if albumartist:
            names[albumartist] += 1
        mb_id = (tags.get("mb_albumartistid") or "").strip()
        if mb_id and "/" not in mb_id:
            ids[mb_id] += 1
    return names, ids


def _folder_artist_identity(folder: Path) -> tuple[str, str]:
    """Return the dominant album-artist spelling and MusicBrainz ID."""
    names, ids = _folder_identity_counts(folder)
    name = names.most_common(1)[0][0] if names else folder.name
    mb_id = ids.most_common(1)[0][0] if ids else ""
    return name, mb_id


def group_artist_identity(folders: list[Path]) -> tuple[str, str]:
    """Resolve one identity for folders that share an artist comparison key.

    Duplicate spellings are frequently the folders missing MusicBrainz IDs, so
    evidence is pooled across the whole group before a catalog is selected.
    """
    names: Counter[str] = Counter()
    ids: Counter[str] = Counter()
    for folder in folders:
        folder_names, folder_ids = _folder_identity_counts(folder)
        names.update(folder_names)
        ids.update(folder_ids)
    name = names.most_common(1)[0][0] if names else folders[0].name
    mb_id = ids.most_common(1)[0][0] if ids else ""
    return name, mb_id


def collect_candidates(
    folder: Path,
    catalogs: ArtistCatalogs,
    *,
    expected_artist: str | None = None,
) -> list[TrackCandidate]:
    """Return tracks in *folder* that no cached catalog entry accounts for."""
    if expected_artist is None:
        expected_artist, _ = _folder_artist_identity(folder)
    candidates: list[TrackCandidate] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(path)
        title = (tags.get("title") or "").strip()
        album = (tags.get("album") or "").strip()
        if normalise_title(title) in catalogs.recordings:
            continue
        if album and normalise_title(album) in catalogs.releases:
            continue
        candidates.append(
            TrackCandidate(
                path=path,
                artist_folder=folder,
                expected_artist=expected_artist,
                artist_mb_id=catalogs.mb_id,
                title=title,
                album=album,
                digest=audio_hash(path) or "",
            )
        )
    return candidates


def classify_candidate(
    candidate: TrackCandidate,
    cache: dict[str, dict | None],
    catalogs: ArtistCatalogs,
) -> TrackDecision:
    """Turn cached fingerprint evidence for one track into a repair decision."""
    if not candidate.digest or candidate.digest not in cache:
        return TrackDecision(candidate, "unknown", "no cached fingerprint evidence")

    entry = cache[candidate.digest]
    if not entry:
        return TrackDecision(candidate, "unmatched", "AcoustID returned no match")

    fingerprint_artist = str(entry.get("artist") or "").strip()
    fingerprint_title = str(entry.get("title") or "").strip()
    recording_id = str(entry.get("recording_id") or "").strip()
    if not fingerprint_artist or not fingerprint_title:
        return TrackDecision(candidate, "unmatched", "incomplete fingerprint evidence")

    same_artist = artist_comparison_key(fingerprint_artist) == artist_comparison_key(
        candidate.expected_artist
    )
    if not same_artist:
        return TrackDecision(
            candidate,
            "misattributed",
            f"fingerprint credits {fingerprint_artist!r}",
            fingerprint_artist,
            fingerprint_title,
            recording_id,
        )

    if normalise_title(fingerprint_title) == normalise_title(candidate.title):
        return TrackDecision(
            candidate,
            "ok",
            "fingerprint confirms the stored title",
            fingerprint_artist,
            fingerprint_title,
            recording_id,
        )

    if fingerprint_title.strip().casefold() in PLACEHOLDER_TITLES:
        return TrackDecision(
            candidate,
            "unverified",
            f"fingerprint returned the placeholder title {fingerprint_title!r}",
            fingerprint_artist,
            fingerprint_title,
            recording_id,
        )

    if catalogs.recordings and normalise_title(fingerprint_title) not in catalogs.recordings:
        return TrackDecision(
            candidate,
            "unverified",
            f"corrected title {fingerprint_title!r} is absent from the cached catalog",
            fingerprint_artist,
            fingerprint_title,
            recording_id,
        )

    return TrackDecision(
        candidate,
        "title",
        f"fingerprint corrects the title to {fingerprint_title!r}",
        fingerprint_artist,
        fingerprint_title,
        recording_id,
    )


def apply_title_fix(decision: TrackDecision, *, dry_run: bool) -> bool:
    """Write the fingerprint-confirmed title and recording ID to one track."""
    updates: dict[str, str] = {"title": decision.fingerprint_title}
    if decision.recording_id:
        updates["mb_trackid"] = decision.recording_id
    if dry_run:
        return True
    return bool(write_tags(decision.candidate.path, updates, dry_run=False))


def _album_relocations(
    decisions: list[TrackDecision],
) -> tuple[dict[Path, str], list[TrackDecision]]:
    """Split misattributions into whole-album moves and loose-track moves.

    An album folder relocates as a unit when at least two of its tracks, and a
    dominant share of all its audio, are credited to the same foreign artist.
    Soundtracks therefore move intact while their guest credits stay with the
    release; an isolated stray track is moved on its own instead.
    """
    by_directory: dict[Path, list[TrackDecision]] = defaultdict(list)
    for decision in decisions:
        by_directory[decision.candidate.path.parent].append(decision)

    albums: dict[Path, str] = {}
    for directory, group in by_directory.items():
        if directory == group[0].candidate.artist_folder:
            continue
        audio_files = [
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in config.AUDIO_EXTENSIONS
        ]
        if not audio_files:
            continue
        artist, count = Counter(
            decision.fingerprint_artist for decision in group
        ).most_common(1)[0]
        if count >= 2 and count / len(audio_files) >= ALBUM_RELOCATION_RATIO:
            albums[directory] = artist

    loose = [
        decision
        for decision in decisions
        if decision.candidate.path.parent not in albums
    ]
    return albums, loose


def _relocated_album_name(source: Path, old_artist: str, new_artist: str) -> str:
    prefix = f"{old_artist} - "
    if source.name.casefold().startswith(prefix.casefold()):
        return sanitise_component(f"{sanitise_name(new_artist)} - {source.name[len(prefix):]}")
    return sanitise_component(source.name)


def _unique_path(path: Path) -> Path:
    candidate = path
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({number}){path.suffix}")
        number += 1
    return candidate


def relocate_album(
    source: Path,
    new_artist: str,
    old_artist: str,
    path_map: dict[Path, Path],
    *,
    track_credits: dict[Path, str] | None = None,
    dry_run: bool,
) -> Path:
    """Move a misattributed album folder to the correct artist root.

    Every track takes the new album artist. Per-track credits come from
    fingerprint evidence where available, so guest tracks on a soundtrack keep
    their own artist while still binding to the release.
    """
    artist_root = (
        config.MUSIC_ROOT
        / artist_letter_bucket(new_artist)
        / sanitise_component(sanitise_name(new_artist))
    )
    destination = artist_root / _relocated_album_name(source, old_artist, new_artist)
    if dry_run:
        return destination

    credits = dict(track_credits or {})
    stale_artist = _dominant_track_artist(source)

    artist_root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Relocation target already exists: {destination}")
    relative_credits = {
        path.relative_to(source): credit
        for path, credit in credits.items()
        if path.is_relative_to(source)
    }
    record_folder_move(
        source,
        destination,
        path_map,
        audio_extensions=config.AUDIO_EXTENSIONS,
    )
    shutil.move(str(source), str(destination))

    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(path)
        updates: dict[str, str] = {"albumartist": new_artist}
        credit = relative_credits.get(path.relative_to(destination))
        if credit:
            updates["artist"] = credit
        elif artist_comparison_key(tags.get("artist")) in {
            artist_comparison_key(old_artist),
            artist_comparison_key(stale_artist),
        }:
            updates["artist"] = new_artist
        write_tags(path, updates, dry_run=False)
    return destination


def _dominant_track_artist(folder: Path) -> str:
    counts: Counter[str] = Counter()
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        artist = (read_tags(path).get("artist") or "").strip()
        if artist:
            counts[artist] += 1
    return counts.most_common(1)[0][0] if counts else ""


def relocate_track(
    decision: TrackDecision,
    path_map: dict[Path, Path],
    *,
    dry_run: bool,
) -> Path:
    """Move one misattributed track to the root singles holding pen."""
    singles = config.MUSIC_ROOT / config.ROOT_SINGLES_FOLDER
    artist = decision.fingerprint_artist
    title = decision.fingerprint_title or decision.candidate.title
    name = sanitise_component(f"{sanitise_name(artist)} - {sanitise_name(title)}{decision.candidate.path.suffix}")
    destination = singles / name
    if dry_run:
        return destination

    singles.mkdir(parents=True, exist_ok=True)
    destination = _unique_path(destination)
    path_map[decision.candidate.path] = destination
    shutil.move(str(decision.candidate.path), str(destination))

    updates: dict[str, str] = {
        "artist": artist,
        "albumartist": artist,
        "title": title,
        "album": "",
    }
    if decision.recording_id:
        updates["mb_trackid"] = decision.recording_id
    write_tags(destination, updates, dry_run=False)
    return destination


def _fetch_missing_fingerprints(
    candidates: list[TrackCandidate],
    cache: dict[str, dict | None],
    *,
    min_score: float,
) -> int:
    fetched = 0
    for candidate in candidates:
        if not candidate.digest or candidate.digest in cache:
            continue
        result = fingerprint_lookup(
            candidate.path,
            min_score=min_score,
            existing_artist=candidate.expected_artist,
            existing_title=candidate.title,
        )
        if result.get("recording_id") or result.get("title"):
            cache[candidate.digest] = {
                "path": str(candidate.path),
                "recording_id": result.get("recording_id"),
                "title": result.get("title"),
                "artist": result.get("artist"),
            }
        else:
            cache[candidate.digest] = None
        fetched += 1
    return fetched


def repair_track_identity(
    *,
    dry_run: bool = True,
    fix: bool = False,
    relocate: bool = False,
    misattributions_only: bool = False,
    artists: list[str] | None = None,
    online_fingerprints: bool = False,
    min_score: float = 0.8,
) -> list[TrackDecision]:
    """Audit, and optionally repair, catalog-unaccounted tracks."""
    root = Path(config.MUSIC_ROOT)
    if not root.exists():
        log.error("Music root does not exist: %s", root)
        return []

    release_catalogs = _load_catalog_cache(
        Path(
            getattr(
                config,
                "MB_RELEASE_GROUP_CACHE",
                Path.home() / ".cache" / "cratedigger" / "mb-artist-release-groups.json",
            )
        )
    )
    recording_catalogs = _load_catalog_cache(
        Path(
            getattr(
                config,
                "MB_RECORDING_CACHE",
                Path.home() / ".cache" / "cratedigger" / "mb-artist-recordings.json",
            )
        )
    )
    identity_cache_path = get_identity_cache_path()
    identity_cache = load_identity_cache(identity_cache_path)

    wanted = {artist_comparison_key(name) for name in artists or []}
    decisions: list[TrackDecision] = []
    catalogs_by_folder: dict[Path, ArtistCatalogs] = {}
    folder_groups: dict[str, list[Path]] = defaultdict(list)
    candidates: list[TrackCandidate] = []

    for folder in iter_artist_folders(root):
        folder_groups[artist_comparison_key(folder.name)].append(folder)

    for key, group in sorted(folder_groups.items()):
        if wanted and key not in wanted:
            continue
        expected_artist, mb_id = group_artist_identity(group)
        catalogs = ArtistCatalogs(
            mb_id=mb_id,
            releases=release_catalogs.get(mb_id, set()),
            recordings=recording_catalogs.get(mb_id, set()),
        )
        if not catalogs.usable:
            continue
        for folder in group:
            catalogs_by_folder[folder] = catalogs
            candidates.extend(
                collect_candidates(folder, catalogs, expected_artist=expected_artist)
            )

    if online_fingerprints:
        if not dry_run:
            log.error("--online-fingerprints is only permitted during dry-runs")
            return []
        fetched = _fetch_missing_fingerprints(candidates, identity_cache, min_score=min_score)
        if fetched:
            save_identity_cache(identity_cache_path, identity_cache)
            log.info("Cached fingerprint evidence for %d new track(s)", fetched)

    for candidate in candidates:
        decisions.append(
            classify_candidate(
                candidate,
                identity_cache,
                catalogs_by_folder[candidate.artist_folder],
            )
        )

    counts = Counter(decision.action for decision in decisions)
    log.info(
        "Scanned %d artist folder(s); %d unaccounted track(s): %s",
        len(catalogs_by_folder),
        len(decisions),
        ", ".join(f"{action}={count}" for action, count in sorted(counts.items())) or "none",
    )
    if misattributions_only:
        decisions = [
            decision
            for decision in decisions
            if decision.action == "misattributed"
        ]

    for decision in decisions:
        if decision.action == "ok":
            continue
        log.info(
            "[%s] %s: %s",
            decision.action,
            decision.candidate.path.relative_to(root),
            decision.reason,
        )

    if not fix:
        return decisions

    mode = "DRY-RUN" if dry_run else "LIVE"
    path_map: dict[Path, Path] = {}
    retitled = 0
    for decision in decisions:
        if decision.action != "title":
            continue
        if apply_title_fix(decision, dry_run=dry_run):
            retitled += 1
            log.info(
                "%s retitle: %s -> %r",
                mode,
                decision.candidate.path.name,
                decision.fingerprint_title,
            )

    moved_albums = 0
    moved_tracks = 0
    if relocate:
        misattributed = [d for d in decisions if d.action == "misattributed"]
        albums, loose = _album_relocations(misattributed)
        credits = {
            d.candidate.path: d.fingerprint_artist
            for d in misattributed
            if d.fingerprint_artist
        }
        for directory, new_artist in sorted(albums.items()):
            old_artist = directory.parent.name
            destination = relocate_album(
                directory,
                new_artist,
                old_artist,
                path_map,
                track_credits=credits,
                dry_run=dry_run,
            )
            moved_albums += 1
            log.info("%s relocate album: %s -> %s", mode, directory, destination)
        for decision in loose:
            destination = relocate_track(decision, path_map, dry_run=dry_run)
            moved_tracks += 1
            log.info(
                "%s relocate track: %s -> %s",
                mode,
                decision.candidate.path,
                destination,
            )

    if not dry_run and path_map:
        update_downstream_paths(path_map, music_root=root, logger=log)

    log.info(
        "Summary (%s): %d retitled, %d album(s) relocated, %d track(s) relocated",
        mode,
        retitled,
        moved_albums,
        moved_tracks,
    )
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artist",
        action="append",
        default=[],
        help="Limit the scan to this artist folder (repeatable)",
    )
    parser.add_argument(
        "--online-fingerprints",
        action="store_true",
        help="Fingerprint uncached candidates; dry-run only",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.8,
        help="Minimum AcoustID match score",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply fingerprint-confirmed title corrections",
    )
    parser.add_argument(
        "--relocate",
        action="store_true",
        help="Also move misattributed audio to the correct artist; requires --fix",
    )
    parser.add_argument(
        "--misattributions-only",
        action="store_true",
        help="Apply/report only fingerprint-confirmed foreign-artist credits",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Apply selected repairs; requires --fix",
    )
    args = parser.parse_args()
    if args.no_dry_run and not args.fix:
        parser.error("--no-dry-run requires --fix")
    if args.relocate and not args.fix:
        parser.error("--relocate requires --fix")
    repair_track_identity(
        dry_run=not args.no_dry_run,
        fix=args.fix,
        relocate=args.relocate,
        misattributions_only=args.misattributions_only,
        artists=args.artist,
        online_fingerprints=args.online_fingerprints,
        min_score=args.min_score,
    )


if __name__ == "__main__":
    main()

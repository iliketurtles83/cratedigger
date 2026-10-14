#!/usr/bin/env python3
"""Audit and safely merge duplicate Tier 1 artist folders.

Groups folders with equivalent artist spellings, gathers identity evidence
from tags and the beets database, and classifies each group:

- confirmed: every folder has the same album-artist ID, complete matching
  track-artist IDs, or exact album-title coverage in a cached MB artist catalog
- partial: one shared ID exists, but at least one folder has no ID evidence
- no_id: no folder has MusicBrainz identity evidence
- conflict: multiple MusicBrainz identities occur in the group

Only confirmed, collision-free groups are eligible for automatic repair.
Dry-run is always the default; both ``--fix`` and ``--no-dry-run`` are required
to write tags or move folders. Online evidence is fetched only during dry-runs
and cached so a reviewed live run cannot acquire new candidates.

Usage:
    python scripts/reconcile_artists.py
    python scripts/reconcile_artists.py --fix
    python scripts/reconcile_artists.py --fix --no-dry-run
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import sys
import re
import unicodedata

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.context import artist_letter_bucket
from lib.mb import artist_release_groups
from lib.parsers import artist_comparison_key, sanitise_name, title_case_clean
from lib.path_updates import record_folder_move, update_downstream_paths
from lib.tags import read_tags, write_tags

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("reconcile_artists")


@dataclass
class ArtistFolderEvidence:
    path: Path
    track_count: int = 0
    albumartists: Counter[str] = field(default_factory=Counter)
    mb_albumartist_ids: set[str] = field(default_factory=set)
    mb_artist_ids: set[str] = field(default_factory=set)
    mb_artist_id_tracks: int = 0
    album_titles_by_unit: dict[str, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )


@dataclass
class ArtistMergePlan:
    key: str
    canonical: str
    target: Path
    folders: list[ArtistFolderEvidence]
    confidence: str
    reason: str
    collisions: list[tuple[Path, Path]] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.confidence == "confirmed" and not self.collisions


def get_beets_db_path() -> Path:
    return Path(
        getattr(
            config,
            "BEETS_DB_PATH",
            Path.home() / ".config" / "beets" / "library.db",
        )
    )


def get_release_evidence_cache_path() -> Path:
    return Path(
        getattr(
            config,
            "MB_RELEASE_GROUP_CACHE",
            Path.home()
            / ".cache"
            / "cratedigger"
            / "mb-artist-release-groups.json",
        )
    )


def choose_canonical_spelling(variants: list[tuple[str, int, int]]) -> str:
    """Choose a canonical spelling from ``(name, count, MB count)`` variants."""
    if not variants:
        raise ValueError("At least one artist spelling is required")

    def score(item: tuple[str, int, int]) -> tuple[int, int, int, int]:
        spelling, count, mb_count = item
        mixed_case = not spelling.islower() and not spelling.isupper()
        return (mb_count, count, int(mixed_case), len(spelling))

    canonical = " ".join(max(variants, key=score)[0].split()).strip()
    if canonical.islower():
        return title_case_clean(canonical) or canonical
    return canonical


def find_artist_folder_groups(root: Path) -> dict[str, list[Path]]:
    """Return comparison-key groups containing multiple Tier 1 artist folders."""
    groups: dict[str, list[Path]] = defaultdict(list)
    buckets = sorted(config.LETTER_BUCKETS | {config.SYMBOL_BUCKET})
    for bucket in buckets:
        bucket_dir = root / bucket
        if not bucket_dir.is_dir():
            continue
        for artist_dir in sorted(path for path in bucket_dir.iterdir() if path.is_dir()):
            key = artist_comparison_key(artist_dir.name)
            if key:
                groups[key].append(artist_dir)
    return {key: paths for key, paths in groups.items() if len(paths) > 1}


def collect_folder_evidence(folder: Path) -> ArtistFolderEvidence:
    """Read album-artist spelling and identity evidence from one artist folder."""
    evidence = ArtistFolderEvidence(path=folder)
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        evidence.track_count += 1
        tags = read_tags(path)
        if not tags:
            continue
        albumartist = (tags.get("albumartist") or "").strip()
        if albumartist:
            evidence.albumartists[albumartist] += 1
        mb_id = (tags.get("mb_albumartistid") or "").strip()
        if mb_id:
            evidence.mb_albumartist_ids.add(mb_id)
        artist_id = (tags.get("mb_artistid") or "").strip()
        if artist_id:
            evidence.mb_artist_ids.add(artist_id)
            evidence.mb_artist_id_tracks += 1
        album = (tags.get("album") or "").strip()
        if album:
            relative = path.relative_to(folder)
            unit = relative.parts[0] if len(relative.parts) > 1 else "."
            evidence.album_titles_by_unit[unit].add(album)
    return evidence


def _beets_spelling_counts(
    database: Path,
    mb_id: str | None,
) -> Counter[str]:
    if not mb_id or not database.exists():
        return Counter()
    try:
        with closing(sqlite3.connect(database)) as connection, connection:
            rows = connection.execute(
                """
                SELECT albumartist, COUNT(*)
                FROM items
                WHERE mb_albumartistid = ? AND albumartist IS NOT NULL
                GROUP BY albumartist
                """,
                (mb_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        log.warning("Could not query beets artist spellings: %s", exc)
        return Counter()
    return Counter({str(name): int(count) for name, count in rows})


def _classify_identity(
    folders: list[ArtistFolderEvidence],
    *,
    release_titles: set[str] | None = None,
) -> tuple[str, str, str | None]:
    per_folder_ids = [folder.mb_albumartist_ids for folder in folders]
    all_ids = set().union(*per_folder_ids)
    if len(all_ids) > 1:
        return "conflict", "multiple MusicBrainz album-artist IDs", None
    if not all_ids:
        return "no_id", "no MusicBrainz album-artist IDs", None

    mb_id = next(iter(all_ids))
    if all(ids == {mb_id} for ids in per_folder_ids):
        return "confirmed", "same MusicBrainz album-artist ID in every folder", mb_id
    if all(
        ids == {mb_id}
        or (
            not ids
            and folder.track_count > 0
            and folder.mb_artist_id_tracks == folder.track_count
            and folder.mb_artist_ids == {mb_id}
        )
        for folder, ids in zip(folders, per_folder_ids)
    ):
        return (
            "confirmed",
            "album-artist ID corroborated by complete track artist-ID coverage",
            mb_id,
        )
    if release_titles is not None and all(
        ids == {mb_id}
        or (
            not ids
            and bool(folder.album_titles_by_unit)
            and all(
                len(titles) == 1
                and _normalise_release_title(next(iter(titles))) in release_titles
                for titles in folder.album_titles_by_unit.values()
            )
        )
        for folder, ids in zip(folders, per_folder_ids)
    ):
        return (
            "confirmed",
            "every unproven album matches the known artist's MusicBrainz catalog",
            mb_id,
        )
    return "partial", "shared MusicBrainz ID with incomplete folder coverage", mb_id


def _normalise_release_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def _canonical_name(
    folders: list[ArtistFolderEvidence],
    *,
    database: Path,
    mb_id: str | None,
) -> str:
    names = {folder.path.name for folder in folders}
    tag_counts: Counter[str] = Counter()
    for folder in folders:
        tag_counts.update(folder.albumartists)
    beets_counts = _beets_spelling_counts(database, mb_id)

    variants: list[tuple[str, int, int]] = []
    for name in names | set(tag_counts) | set(beets_counts):
        if artist_comparison_key(name) != artist_comparison_key(next(iter(names))):
            continue
        folder_tracks = sum(
            folder.track_count for folder in folders if folder.path.name == name
        )
        variants.append(
            (name, tag_counts[name] + folder_tracks, beets_counts[name])
        )
    return choose_canonical_spelling(variants)


def _destination_name(source: Path, canonical: str) -> str:
    prefix = f"{source.parent.name} - "
    if source.name.casefold().startswith(prefix.casefold()):
        return f"{sanitise_name(canonical)} - {source.name[len(prefix):]}"
    return source.name


def _find_collisions(
    folders: list[ArtistFolderEvidence],
    target: Path,
    canonical: str,
) -> list[tuple[Path, Path]]:
    collisions: list[tuple[Path, Path]] = []
    planned: set[Path] = set()
    for folder in folders:
        if folder.path == target:
            continue
        for source in folder.path.iterdir():
            destination = target / _destination_name(source, canonical)
            if destination.exists() or destination in planned:
                collisions.append((source, destination))
            planned.add(destination)
    return collisions


def build_merge_plans(
    root: Path,
    database: Path,
    *,
    release_catalogs: dict[str, set[str]] | None = None,
) -> list[ArtistMergePlan]:
    """Build evidence-backed merge plans for every duplicate folder group."""
    plans: list[ArtistMergePlan] = []
    for key, paths in sorted(find_artist_folder_groups(root).items()):
        folders = [collect_folder_evidence(path) for path in paths]
        confidence, reason, mb_id = _classify_identity(folders)
        if (
            release_catalogs is not None
            and confidence == "partial"
            and mb_id
            and "/" not in mb_id
        ):
            catalog = release_catalogs.get(mb_id, set())
            if catalog:
                confidence, reason, mb_id = _classify_identity(
                    folders,
                    release_titles=catalog,
                )
        canonical = _canonical_name(folders, database=database, mb_id=mb_id)
        target = (
            root
            / artist_letter_bucket(canonical)
            / sanitise_name(canonical)
        )
        plans.append(
            ArtistMergePlan(
                key=key,
                canonical=canonical,
                target=target,
                folders=folders,
                confidence=confidence,
                reason=reason,
                collisions=_find_collisions(folders, target, canonical),
            )
        )
    return plans


def _load_release_catalogs(cache_path: Path) -> dict[str, set[str]]:
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        log.warning("Could not load MusicBrainz release evidence cache: %s", exc)
        return {}
    if not isinstance(payload, dict):
        log.warning("Ignoring invalid MusicBrainz release evidence cache")
        return {}
    return {
        str(artist_id): {str(title) for title in titles}
        for artist_id, titles in payload.items()
        if isinstance(titles, list)
    }


def _save_release_catalogs(
    cache_path: Path,
    catalogs: dict[str, set[str]],
) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    artist_id: sorted(titles)
                    for artist_id, titles in sorted(catalogs.items())
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Could not save MusicBrainz release evidence cache: %s", exc)


def _refresh_release_catalogs(
    plans: list[ArtistMergePlan],
    catalogs: dict[str, set[str]],
) -> None:
    artist_ids = sorted({
        mb_id
        for plan in plans
        if plan.confidence == "partial"
        for folder in plan.folders
        for mb_id in folder.mb_albumartist_ids
        if mb_id and "/" not in mb_id
    })
    for artist_id in artist_ids:
        if artist_id in catalogs:
            continue
        groups = artist_release_groups(artist_id)
        titles = {
            _normalise_release_title(group["title"])
            for group in groups
            if group.get("title")
        }
        if titles:
            catalogs[artist_id] = titles


def _update_beets_artist_names(
    database: Path,
    variants: set[str],
    canonical: str,
    paths: list[Path],
) -> None:
    if not database.exists():
        return
    try:
        with closing(sqlite3.connect(database)) as connection, connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(items)")
            }
            has_album_id = "album_id" in columns
            album_ids: set[int] = set()
            for path in paths:
                relative = path.relative_to(config.MUSIC_ROOT)
                candidates = (
                    str(path),
                    str(path).encode("utf-8"),
                    str(relative),
                    str(relative).encode("utf-8"),
                )
                placeholders = ", ".join("?" for _ in candidates)
                fields = "id, artist, albumartist"
                if has_album_id:
                    fields += ", album_id"
                rows = connection.execute(
                    f"SELECT {fields} FROM items WHERE path IN ({placeholders})",
                    candidates,
                ).fetchall()
                for row in rows:
                    item_id, _artist, albumartist, *album_id = row
                    updates: list[str] = []
                    values: list[object] = []
                    if albumartist in variants and albumartist != canonical:
                        updates.append("albumartist = ?")
                        values.append(canonical)
                    if updates:
                        connection.execute(
                            f"UPDATE items SET {', '.join(updates)} WHERE id = ?",
                            (*values, item_id),
                        )
                    if album_id and album_id[0] is not None:
                        album_ids.add(int(album_id[0]))
            for album_id in album_ids:
                connection.execute(
                    """
                    UPDATE albums
                    SET albumartist = ?
                    WHERE id = ? AND albumartist IN ({})
                    """.format(", ".join("?" for _ in variants)),
                    (canonical, album_id, *sorted(variants)),
                )
    except sqlite3.Error as exc:
        log.warning("Could not update beets artist names: %s", exc)


def apply_merge_plan(
    plan: ArtistMergePlan,
    *,
    database: Path,
    dry_run: bool,
) -> tuple[int, dict[Path, Path]]:
    """Apply one prevalidated artist-folder merge and return tag/path counts."""
    if not plan.eligible:
        raise ValueError(f"Plan {plan.key!r} is not eligible for automatic repair")
    if dry_run:
        return 0, {}

    plan.target.mkdir(parents=True, exist_ok=True)
    path_map: dict[Path, Path] = {}
    for folder in plan.folders:
        if folder.path == plan.target:
            continue
        for source in sorted(folder.path.iterdir()):
            destination = plan.target / _destination_name(source, plan.canonical)
            if source.is_dir():
                record_folder_move(
                    source,
                    destination,
                    path_map,
                    audio_extensions=config.AUDIO_EXTENSIONS,
                )
            elif source.suffix.lower() in config.AUDIO_EXTENSIONS:
                path_map[source] = destination
            shutil.move(str(source), str(destination))
        folder.path.rmdir()

    variants = {folder.path.name for folder in plan.folders}
    updated_tags = 0
    target_audio_paths: list[Path] = []
    for path in sorted(plan.target.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        target_audio_paths.append(path)
        tags = read_tags(path)
        if not tags:
            continue
        updates: dict[str, str] = {}
        albumartist = (tags.get("albumartist") or "").strip()
        if albumartist in variants and albumartist != plan.canonical:
            updates["albumartist"] = plan.canonical
        if updates:
            write_tags(path, updates, dry_run=False)
            updated_tags += 1

    update_downstream_paths(
        path_map,
        music_root=config.MUSIC_ROOT,
        logger=log,
    )
    _update_beets_artist_names(
        database,
        variants,
        plan.canonical,
        target_audio_paths,
    )
    return updated_tags, path_map


def reconcile_artists(
    *,
    dry_run: bool = True,
    fix: bool = False,
    min_tracks: int = 1,
    online_release_evidence: bool = False,
) -> list[ArtistMergePlan]:
    """Audit duplicate artist folders and optionally apply confirmed merges."""
    database = get_beets_db_path()
    release_catalogs: dict[str, set[str]] | None = None
    if online_release_evidence:
        cache_path = get_release_evidence_cache_path()
        release_catalogs = _load_release_catalogs(cache_path)
        if dry_run:
            offline_plans = build_merge_plans(config.MUSIC_ROOT, database)
            _refresh_release_catalogs(offline_plans, release_catalogs)
            _save_release_catalogs(cache_path, release_catalogs)
        elif not release_catalogs:
            log.error(
                "Online release evidence cache is empty; run a dry-run with "
                "--online-release-evidence first"
            )
            return []
    plans = build_merge_plans(
        config.MUSIC_ROOT,
        database,
        release_catalogs=release_catalogs,
    )
    counts = Counter(plan.confidence for plan in plans)
    log.info(
        "Found %d duplicate artist groups: %d confirmed, %d partial, "
        "%d without IDs, %d conflicts",
        len(plans),
        counts["confirmed"],
        counts["partial"],
        counts["no_id"],
        counts["conflict"],
    )

    applied = 0
    for plan in plans:
        tracks = sum(folder.track_count for folder in plan.folders)
        if tracks < min_tracks:
            continue
        variants = " | ".join(folder.path.name for folder in plan.folders)
        collision_suffix = (
            f", {len(plan.collisions)} collision(s)" if plan.collisions else ""
        )
        log.info(
            "[%s] %s -> %s (%d tracks%s): %s",
            plan.confidence.upper(),
            variants,
            plan.canonical,
            tracks,
            collision_suffix,
            plan.reason,
        )
        if plan.collisions:
            for source, destination in plan.collisions:
                log.warning("  Collision: %s -> %s", source, destination)
        if not fix or not plan.eligible:
            continue
        if dry_run:
            log.info("  [DRY-RUN] Would merge into %s", plan.target)
            continue
        updated_tags, path_map = apply_merge_plan(
            plan,
            database=database,
            dry_run=False,
        )
        applied += 1
        log.info(
            "  Merged into %s; updated %d tag file(s), %d path(s)",
            plan.target,
            updated_tags,
            len(path_map),
        )

    mode = "audit" if not fix else ("dry-run" if dry_run else "live")
    log.info("Summary: %d plan(s), %d applied (%s)", len(plans), applied, mode)
    return plans


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Select confirmed, collision-free groups for repair",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Apply selected repairs; requires --fix",
    )
    parser.add_argument(
        "--min-tracks",
        type=int,
        default=1,
        help="Minimum total tracks in a group to report",
    )
    parser.add_argument(
        "--online-release-evidence",
        action="store_true",
        help="Use cached MB artist catalogs; dry-runs fetch missing catalogs",
    )
    args = parser.parse_args()
    if args.no_dry_run and not args.fix:
        parser.error("--no-dry-run requires --fix")
    reconcile_artists(
        dry_run=not args.no_dry_run,
        fix=args.fix,
        min_tracks=args.min_tracks,
        online_release_evidence=args.online_release_evidence,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Audit and repair Windows-incompatible library path components.

Dry-run is the default. Applying changes requires both ``--fix`` and
``--no-dry-run``. The repair normalizes invalid path components, merges
case-insensitive sibling collisions, preserves differing audio as deterministic
alternates, and updates playlists/features.pkl.
"""

import argparse
from collections import defaultdict
import hashlib
import logging
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.audiohash import audio_hash
from lib.parsers import sanitise_component
from lib.path_updates import record_folder_move, update_downstream_paths

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("repair_windows_paths")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audio_identity(path: Path) -> str:
    return audio_hash(path) or _digest(path)


def _alternate_path(path: Path, identity: str) -> Path:
    suffix = identity.split(":", 1)[-1][:8]
    candidate = path.with_name(f"{path.stem} [alternate-{suffix}]{path.suffix}")
    number = 2
    while candidate.exists():
        candidate = path.with_name(
            f"{path.stem} [alternate-{suffix}-{number}]{path.suffix}"
        )
        number += 1
    return candidate


def _casefold_groups(folder: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for child in folder.iterdir():
        groups[child.name.casefold()].append(child)
    return {key: paths for key, paths in groups.items() if len(paths) > 1}


def _casefold_child(folder: Path, name: str) -> Path | None:
    key = name.casefold()
    for child in folder.iterdir():
        if child.name.casefold() == key:
            return child
    return None


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_CANONICAL_ART_NAMES = {
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
}


def _image_quality(path: Path) -> tuple[int, int]:
    """Return (pixel_count, file_size) for image quality comparison."""
    try:
        from PIL import Image

        with Image.open(path) as img:
            return (img.width * img.height, path.stat().st_size)
    except Exception:
        return (0, path.stat().st_size)


def _canonical_child(paths: list[Path]) -> Path:
    """Prefer lowercase for cover art; otherwise prefer most title-cased words, then shortest path."""
    first = paths[0]
    if first.name.casefold() in _CANONICAL_ART_NAMES:
        target_name = first.name.lower()
        for p in paths:
            if p.name == target_name:
                return p
        return paths[0].with_name(target_name)

    return sorted(
        paths,
        key=lambda path: (
            sum(part[:1].isupper() for part in path.name.split()),
            -len(path.name),
            path.name,
        ),
        reverse=True,
    )[0]


def _merge_file(
    source: Path,
    destination: Path,
    path_map: dict[Path, Path],
    *,
    dry_run: bool,
) -> None:
    if destination.exists():
        if _audio_identity(source) == _audio_identity(destination):
            path_map[source] = destination
            if not dry_run:
                source.unlink()
            log.info("%s remove duplicate: %s", "DRY-RUN" if dry_run else "LIVE", source)
            return

        if (
            source.suffix.lower() in _IMAGE_EXTENSIONS
            and destination.suffix.lower() in _IMAGE_EXTENSIONS
        ):
            source_q = _image_quality(source)
            dest_q = _image_quality(destination)
            if source_q > dest_q:
                path_map[source] = destination
                if not dry_run:
                    destination.unlink()
                    shutil.move(str(source), str(destination))
                log.info(
                    "%s upgrade artwork (higher res/quality): %s replaces %s",
                    "DRY-RUN" if dry_run else "LIVE",
                    source,
                    destination,
                )
                return
            else:
                path_map[source] = destination
                if not dry_run:
                    source.unlink()
                log.info(
                    "%s remove lower-quality artwork duplicate: %s (kept %s)",
                    "DRY-RUN" if dry_run else "LIVE",
                    source,
                    destination,
                )
                return

        destination = _alternate_path(destination, _audio_identity(source))
    path_map[source] = destination
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    log.info(
        "%s move file: %s -> %s",
        "DRY-RUN" if dry_run else "LIVE",
        source,
        destination,
    )


def _merge_tree(
    source: Path,
    destination: Path,
    path_map: dict[Path, Path],
    *,
    dry_run: bool,
) -> None:
    for child in sorted(source.iterdir()):
        target = destination / sanitise_component(child.name)
        existing = _casefold_child(destination, target.name) if destination.exists() else None
        if existing is not None:
            target = existing
        if child.is_file():
            _merge_file(child, target, path_map, dry_run=dry_run)
            continue
        if not any(p.is_file() for p in child.rglob("*")):
            if not dry_run:
                shutil.rmtree(child)
            log.info(
                "%s remove empty directory: %s",
                "DRY-RUN" if dry_run else "LIVE",
                child,
            )
            continue
        if target.exists() and target.is_file():
            target = _alternate_path(target, _digest(child))
        if target.exists():
            _merge_tree(child, target, path_map, dry_run=dry_run)
            if not dry_run and child.exists():
                child.rmdir()
        else:
            record_folder_move(
                child,
                target,
                path_map,
                audio_extensions=config.AUDIO_EXTENSIONS,
            )
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(target))
            log.info(
                "%s move directory: %s -> %s",
                "DRY-RUN" if dry_run else "LIVE",
                child,
                target,
            )
    if not dry_run and source.exists():
        source.rmdir()


def _normalize_component_names(
    folder: Path,
    path_map: dict[Path, Path],
    *,
    dry_run: bool,
) -> None:
    """Normalize invalid child names, merging if the target already exists."""
    for child in sorted(list(folder.iterdir()), key=lambda path: path.name):
        safe_name = sanitise_component(child.name)
        if safe_name == child.name:
            continue
        if child.is_dir() and not any(p.is_file() for p in child.rglob("*")):
            if not dry_run:
                shutil.rmtree(child)
            log.info(
                "%s remove empty invalid directory: %s",
                "DRY-RUN" if dry_run else "LIVE",
                child,
            )
            continue
        destination = child.with_name(safe_name)
        if destination.exists() or destination.name.casefold() == child.name.casefold():
            if child.is_dir() and destination.is_dir():
                _merge_tree(child, destination, path_map, dry_run=dry_run)
            elif child.is_file() and destination.is_file():
                _merge_file(child, destination, path_map, dry_run=dry_run)
            continue
        if child.is_dir():
            record_folder_move(
                child,
                destination,
                path_map,
                audio_extensions=config.AUDIO_EXTENSIONS,
            )
        else:
            path_map[child] = destination
        if not dry_run:
            child.rename(destination)
        log.info(
            "%s normalize name: %s -> %s",
            "DRY-RUN" if dry_run else "LIVE",
            child,
            destination,
        )


def repair_windows_paths(
    *,
    dry_run: bool = True,
    fix: bool = False,
    names_only: bool = False,
) -> int:
    root = Path(config.MUSIC_ROOT)
    path_map: dict[Path, Path] = {}
    collisions = 0
    for folder in sorted(
        [root] + [path for path in root.rglob("*") if path.is_dir()],
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if not folder.exists():
            continue
        _normalize_component_names(folder, path_map, dry_run=dry_run)
        if names_only:
            continue
        for paths in _casefold_groups(folder).values():
            collisions += 1
            destination = _canonical_child(paths)
            for source in paths:
                if source != destination:
                    if source.is_dir():
                        _merge_tree(source, destination, path_map, dry_run=dry_run)
                    else:
                        _merge_file(source, destination, path_map, dry_run=dry_run)
    if not dry_run:
        update_downstream_paths(path_map, music_root=root, logger=log)
    log.info(
        "Summary (%s): %d case-insensitive collision group(s), %d path update(s)",
        "DRY-RUN" if dry_run else "LIVE",
        collisions,
        len(path_map),
    )
    return collisions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="Select repairs")
    parser.add_argument(
        "--names-only",
        action="store_true",
        help="Only normalize Windows-invalid component names (trailing dots/spaces, illegal chars)",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Apply repairs; requires --fix",
    )
    args = parser.parse_args()
    if args.no_dry_run and not args.fix:
        parser.error("--no-dry-run requires --fix")
    repair_windows_paths(
        dry_run=not args.no_dry_run,
        fix=args.fix,
        names_only=args.names_only,
    )


if __name__ == "__main__":
    main()

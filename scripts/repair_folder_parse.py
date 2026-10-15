#!/usr/bin/env python3
"""repair_folder_parse.py — Repair damage from the ``_FOLDER_PATTERN`` bug.

Until the fix, ``lib.parsers._FOLDER_PATTERN`` split album folder names on a
bare hyphen (``\\s*-\\s*``) instead of a spaced one.  Hyphenated artists were
truncated at the first hyphen and the remainder leaked into the album name::

    "AC-DC - Stiff Upper Lip (2000)" -> artist "AC",  album "DC - Stiff Upper Lip"
    "A-ha - Hunting High and Low (1985)" -> artist "A", album "ha - Hunting High and Low"

01_tag.py fed those values into the ``albumartist`` and ``album`` tags.

Two independent repair phases, both dry-run by default:

``--tags``
    Rewrite ``albumartist`` / ``album`` tags, but only where the *old* buggy
    regex exactly explains the stored value.  Anything else is left alone.

``--structure``
    Relocate album-shaped folders stranded at artist depth under a letter
    bucket (e.g. ``R/Easy Star All-Stars - Radiodread (2006)``) into
    ``bucket/Artist/Artist - Album (Year)/``, re-bucketing by artist.

Usage:
    python scripts/repair_folder_parse.py --tags --dry-run
    python scripts/repair_folder_parse.py --tags --no-dry-run
    python scripts/repair_folder_parse.py --structure --no-dry-run --folder R
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from lib.context import artist_letter_bucket, classify_folder, is_letter_bucket
from lib.logger import setup_logger
from lib.parsers import parse_folder_name, sanitise_component, sanitise_name
from lib.tags import read_tags, write_tags

log: logging.Logger = logging.getLogger("repair_folder_parse")

# The pre-fix pattern, kept verbatim so repairs only touch values it explains.
_BUGGY_FOLDER_PATTERN = re.compile(
    r"^(?P<artist>.+?)\s*-\s*(?P<album>.+?)\s*\((?P<year>\d{4})\)$"
)


def _buggy_parse(name: str) -> dict[str, str] | None:
    """Return what the pre-fix regex produced for *name*, or None."""
    m = _BUGGY_FOLDER_PATTERN.match(name)
    if not m:
        return None
    return {
        "artist": m.group("artist").strip(),
        "album": m.group("album").strip(),
        "year": m.group("year"),
    }


def _misparsed_folders(root: Path) -> list[tuple[Path, dict[str, str], dict[str, str | None]]]:
    """Find album folders whose name the old regex parsed differently."""
    found = []
    for folder in sorted(root.rglob("*")):
        if not folder.is_dir():
            continue
        bad = _buggy_parse(folder.name)
        if not bad:
            continue
        good = parse_folder_name(folder.name)
        if not good.get("artist"):
            continue
        if bad["artist"] == good["artist"]:
            continue
        found.append((folder, bad, good))
    return found


def _canonical_artist(folder: Path, folder_artist: str) -> str:
    """Resolve the album artist, preferring tag spelling over the folder name.

    Folder names are sanitised (``AC/DC`` → ``AC-DC``, ``*`` stripped), so the
    folder is only a shape hint.  When every track's artist tag sanitises to
    the folder name, that tag carries the real punctuation and wins.  Guest
    credits make the folder spelling the safer choice, so unanimity is
    required.
    """
    spellings: set[str] = set()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(path) or {}
        artist = (tags.get("artist") or "").strip()
        if artist and sanitise_name(artist) == folder_artist:
            spellings.add(artist)
        else:
            return folder_artist

    if len(spellings) == 1:
        return spellings.pop()
    return folder_artist


def repair_tags(root: Path, *, dry_run: bool) -> int:
    """Rewrite albumartist/album tags corrupted by the old folder parse."""
    repaired = 0

    for folder, bad, good in _misparsed_folders(root):
        target_artist = _canonical_artist(folder, good["artist"])

        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
                continue

            tags = read_tags(path)
            if tags is None:
                log.warning("Unreadable, skipping: %s", path)
                continue

            updates: dict[str, str] = {}

            # Only repair values the buggy parse exactly explains — a tag that
            # says something else came from somewhere else and is not ours.
            current_aa = (tags.get("albumartist") or "").strip()
            if current_aa in {bad["artist"], good["artist"]} and current_aa != target_artist:
                updates["albumartist"] = target_artist
            if (tags.get("album") or "").strip() == bad["album"]:
                updates["album"] = good["album"]

            if not updates:
                continue

            for field, value in updates.items():
                log.info("%s: %s %r → %r", path.name, field,
                         (tags.get(field) or "").strip(), value)
            write_tags(path, updates, dry_run=dry_run)
            repaired += 1

    log.info("%s%d file(s) repaired", "[DRY-RUN] " if dry_run else "", repaired)
    return repaired


def _stranded_album_folders(root: Path, folder: str | None) -> list[Path]:
    """Album-shaped folders sitting at artist depth under a letter bucket."""
    if folder:
        buckets = [root / folder]
    else:
        buckets = [p for p in sorted(root.iterdir())
                   if p.is_dir() and is_letter_bucket(p.name)]

    stranded = []
    for bucket in buckets:
        if not bucket.is_dir() or not is_letter_bucket(bucket.name):
            continue
        for child in sorted(bucket.iterdir()):
            if not child.is_dir() or " - " not in child.name:
                continue
            if not parse_folder_name(child.name).get("artist"):
                continue
            # classify_folder() labels anything with " - " an album by name, so
            # test the shape directly: audio files and no album subfolders.
            grandchildren = list(child.iterdir())
            has_audio = any(
                g.is_file() and g.suffix.lower() in config.AUDIO_EXTENSIONS
                for g in grandchildren
            )
            has_album_subdirs = any(
                g.is_dir() and classify_folder(g) in {"album", "artist_flat"}
                for g in grandchildren
            )
            if has_audio and not has_album_subdirs:
                stranded.append(child)
    return stranded


def _resolve_artist(folder: Path) -> str | None:
    """Prefer the albumartist tag, fall back to the folder name parse."""
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in config.AUDIO_EXTENSIONS:
            tags = read_tags(path) or {}
            if (albumartist := (tags.get("albumartist") or "").strip()):
                return albumartist
            break
    return parse_folder_name(folder.name).get("artist")


def repair_structure(root: Path, *, dry_run: bool, folder: str | None) -> int:
    """Nest stranded album folders under their artist and re-bucket them."""
    moved = 0

    for stranded in _stranded_album_folders(root, folder):
        artist = _resolve_artist(stranded)
        if not artist:
            log.warning("No artist resolvable for %s — skipping", stranded)
            continue

        artist_dir = sanitise_component(sanitise_name(artist))
        destination = (
            root / artist_letter_bucket(artist_dir) / artist_dir / stranded.name
        )

        if destination == stranded:
            continue
        if destination.exists():
            log.warning("Target exists, skipping: %s → %s", stranded, destination)
            continue

        if dry_run:
            log.info("[DRY-RUN] Would move: %s → %s", stranded, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            stranded.rename(destination)
            log.info("Moved: %s → %s", stranded, destination)
        moved += 1

    log.info("%s%d folder(s) relocated", "[DRY-RUN] " if dry_run else "", moved)
    return moved


def main() -> None:
    global log

    parser = argparse.ArgumentParser(
        description="Repair damage from the _FOLDER_PATTERN hyphen-split bug.")
    parser.add_argument("--tags", action="store_true",
                        help="Repair corrupted albumartist/album tags")
    parser.add_argument("--structure", action="store_true",
                        help="Relocate album folders stranded at artist depth")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually write tags / move folders")
    parser.add_argument("--folder", type=str, default=None,
                        help="Limit to a single top-level folder")
    parser.add_argument("--log", type=str, default="review.log")
    args = parser.parse_args()

    log = setup_logger("repair_folder_parse", Path(args.log))

    if not args.tags and not args.structure:
        parser.error("choose at least one of --tags / --structure")

    dry_run = not args.no_dry_run
    root = config.MUSIC_ROOT
    scope = (root / args.folder) if args.folder else root

    if not scope.exists():
        log.error("Folder does not exist: %s", scope)
        raise SystemExit(1)

    if args.tags:
        log.info("=== Tag repair (%s) ===", "dry-run" if dry_run else "LIVE")
        repair_tags(scope, dry_run=dry_run)

    if args.structure:
        log.info("=== Structure repair (%s) ===", "dry-run" if dry_run else "LIVE")
        repair_structure(root, dry_run=dry_run, folder=args.folder)


if __name__ == "__main__":
    main()

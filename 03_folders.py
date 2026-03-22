#!/usr/bin/env python3
"""03_folders.py — Normalise folder names (artist/album/year, The-suffix).

Renames album folders to canonical format:
    ``Artist Name - Album Title (Year)/``

In two-level setups:
    ``Artist Name/Artist Name - Album Title (Year)/``

Never renames: SPECIAL_FOLDERS, SKIP_FOLDERS, subgenre buckets, disc
subfolders, or genre root folders themselves.

Usage:
    python 03_folders.py [--dry-run] [--folder NAME] [--log FILE]
"""

import argparse
import json
import logging
import re
import shutil
from pathlib import Path

import config
from lib.logger import setup_logger
from lib.parsers import parse_folder_name
from lib.tags import read_tags

log: logging.Logger = None  # type: ignore[assignment]

_DISC_PATTERN = re.compile(r"^(cd|disc|disk)\s*\d+$", re.IGNORECASE)

# Album folder without year — requires space-dash-space to avoid splitting
# artist names with hyphens (e.g. AC-DC)
_FOLDER_NO_YEAR = re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+)$")

# Year in parentheses embedded in album text
_EMBEDDED_YEAR = re.compile(r"[\(\[](\d{4})[\)\]]")


def _is_disc_subfolder(name: str) -> bool:
    return bool(_DISC_PATTERN.match(name))


def classify_folder(folder: Path) -> str:
    """Classify a folder inside a genre directory.

    Returns one of: ``album``, ``artist``, ``artist_mixed``, ``artist_flat``,
    ``subgenre``, ``local_special``, ``disc``, ``unknown``.
    """
    name = folder.name
    has_audio = any(
        f.suffix.lower() in config.AUDIO_EXTENSIONS
        for f in folder.iterdir()
        if f.is_file()
    )
    has_album_subdirs = any(
        d.is_dir() and classify_folder(d) in ("album", "artist", "artist_flat")
        for d in folder.iterdir()
    )
    starts_with_0 = name.startswith("0")
    has_separator = " - " in name

    if _is_disc_subfolder(name):
        return "disc"
    if starts_with_0 and name in config.SUBGENRE_BUCKETS:
        return "subgenre"
    if starts_with_0:
        return "local_special"
    if has_separator:
        return "album"
    if has_album_subdirs and not has_audio:
        return "artist"
    if has_album_subdirs and has_audio:
        return "artist_mixed"
    if not has_album_subdirs and has_audio:
        return "artist_flat"
    return "unknown"


def _fix_the(name: str) -> str:
    """Convert ', The' / ', A' suffix to prefix."""
    if name.endswith(", The"):
        return "The " + name[:-5]
    if name.endswith(", A"):
        return "A " + name[:-3]
    return name


def _year_from_tags(folder: Path) -> str | None:
    """Read year from the first audio file found in *folder*."""
    for f in sorted(folder.rglob("*")):
        if f.suffix.lower() in config.AUDIO_EXTENSIONS and f.is_file():
            tags = read_tags(f)
            if tags is None:
                continue
            year = tags.get("year", "") or ""
            year = year[:4]
            if re.match(r"^\d{4}$", year):
                return year
    return None


def _parse_album_name(name: str) -> dict[str, str | None] | None:
    """Parse album folder name. Returns {artist, album, year} or None."""
    # Full pattern with year at end
    parsed = parse_folder_name(name)
    if parsed["artist"]:
        return parsed
    # Without year at end
    m = _FOLDER_NO_YEAR.match(name)
    if m:
        artist = m.group("artist").strip()
        album = m.group("album").strip()
        # Extract embedded (YYYY) from album text so it isn't duplicated
        year_m = _EMBEDDED_YEAR.search(album)
        year = None
        if year_m:
            year = year_m.group(1)
            before = album[:year_m.start()].rstrip()
            after = album[year_m.end():].lstrip()
            if before and after:
                album = before + " " + after
            else:
                album = before or after
        return {"artist": artist, "album": album, "year": year}
    return None


def _build_folder_name(artist: str, album: str, year: str | None) -> str:
    if year:
        return f"{artist} - {album} ({year})"
    return f"{artist} - {album}"


def _normalise_folder(
    folder: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Normalise a single album folder name."""
    name = folder.name
    parsed = _parse_album_name(name)

    if parsed is None:
        log.warning("Unknown folder pattern: %s — flagging for review", folder)
        review_items.append({"path": str(folder), "reason": "unknown_pattern"})
        return

    artist = _fix_the(parsed["artist"])
    album = parsed["album"]
    year = parsed["year"]

    # If no year in folder name, try to get from file tags
    if not year:
        year = _year_from_tags(folder)
        if not year:
            log.warning("Cannot determine year for %s — flagging for review", folder)
            review_items.append({"path": str(folder), "reason": "no_year"})
            # Still proceed — The-suffix fix may apply even without year

    new_name = _build_folder_name(artist, album, year)

    if new_name == name:
        log.debug("Already canonical: %s", name)
        return

    new_path = folder.parent / new_name

    if new_path.exists():
        log.warning("Target exists, skipping: %s → %s", name, new_name)
        review_items.append({
            "path": str(folder),
            "reason": "rename_conflict",
            "target": str(new_path),
        })
        return

    if dry_run:
        log.info("[DRY-RUN] Would rename: %s → %s", name, new_name)
    else:
        folder.rename(new_path)
        log.info("Renamed: %s → %s", name, new_name)


def _normalise_album_child(
    folder: Path,
    *,
    artist: str,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Normalise an album-only subfolder inside an artist folder.

    Prepends the artist name from the parent folder to build canonical format.
    """
    album = folder.name

    # If the child name already starts with the artist name, it's probably
    # a malformed "Artist Album" folder missing the separator — flag it.
    if album.strip().lower().startswith(artist.strip().lower()):
        log.warning("Folder may contain embedded artist name: %s "
                     "— flagging for review", folder)
        review_items.append({"path": str(folder), "reason": "unknown_pattern"})
        return

    # Extract embedded (YYYY) from album name to avoid duplication
    year_m = _EMBEDDED_YEAR.search(album)
    year = None
    if year_m:
        year = year_m.group(1)
        before = album[:year_m.start()].rstrip()
        after = album[year_m.end():].lstrip()
        if before and after:
            album = before + " " + after
        else:
            album = (before or after).strip()

    if not year:
        year = _year_from_tags(folder)

    if not year:
        log.warning("Cannot determine year for %s — flagging for review", folder)
        review_items.append({"path": str(folder), "reason": "no_year"})

    new_name = _build_folder_name(artist, album, year)

    if new_name == folder.name:
        log.debug("Already canonical: %s", folder.name)
        return

    new_path = folder.parent / new_name

    if new_path.exists():
        log.warning("Target exists, skipping: %s → %s", folder.name, new_name)
        review_items.append({
            "path": str(folder),
            "reason": "rename_conflict",
            "target": str(new_path),
        })
        return

    if dry_run:
        log.info("[DRY-RUN] Would rename: %s → %s", folder.name, new_name)
    else:
        folder.rename(new_path)
        log.info("Renamed: %s → %s", folder.name, new_name)


def _move_loose_to_singles(
    folder: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Move loose audio files in an artist_mixed folder to 0singles/."""
    singles_dir = folder / "0singles"
    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        dest = singles_dir / f.name
        if dest.exists():
            log.warning("Target exists in 0singles, skipping: %s", f.name)
            review_items.append({
                "path": str(f),
                "reason": "move_conflict",
                "target": str(dest),
            })
            continue
        if dry_run:
            log.info("[DRY-RUN] Would move to 0singles: %s", f.name)
        else:
            singles_dir.mkdir(exist_ok=True)
            shutil.move(str(f), str(dest))
            log.info("Moved to 0singles: %s", f.name)


def _handle_artist_flat(
    folder: Path,
    *,
    review_items: list[dict],
) -> None:
    """Flag artist_flat folders with mixed album tags for review."""
    albums = set()
    for f in sorted(folder.rglob("*")):
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS:
            tags = read_tags(f)
            if tags is None:
                continue
            album = tags.get("album") or ""
            if album:
                albums.add(album)
    if len(albums) > 1:
        log.warning("artist_flat with mixed album tags: %s — flagging for "
                     "review", folder)
        review_items.append({
            "path": str(folder),
            "reason": "artist_flat_mixed",
            "albums": sorted(albums),
        })


def _normalise_artist_folder(
    folder: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Fix artist folder name (The-suffix) and process album children."""
    name = folder.name
    fixed = _fix_the(name)
    current = folder

    if fixed != name:
        new_path = folder.parent / fixed
        if new_path.exists():
            log.warning("Target exists, skipping artist rename: %s → %s",
                        name, fixed)
            review_items.append({
                "path": str(folder),
                "reason": "rename_conflict",
                "target": str(new_path),
            })
        elif dry_run:
            log.info("[DRY-RUN] Would rename artist folder: %s → %s",
                     name, fixed)
        else:
            folder.rename(new_path)
            current = new_path
            log.info("Renamed artist folder: %s → %s", name, fixed)

    # Process album subfolders inside the artist folder
    for child in sorted(current.iterdir()):
        if not child.is_dir():
            continue
        if _is_disc_subfolder(child.name):
            continue
        if child.name.startswith("0"):
            continue

        if " - " in child.name:
            # Standard album folder — normalise normally
            _normalise_folder(child, dry_run=dry_run,
                              review_items=review_items)
        else:
            # Album-only name — prepend artist from parent folder
            effective_artist = fixed
            _normalise_album_child(child, artist=effective_artist,
                                   dry_run=dry_run,
                                   review_items=review_items)


def _process_level2(
    genre_dir: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Process children of a genre folder (or subgenre bucket)."""
    for child in sorted(genre_dir.iterdir()):
        if not child.is_dir():
            continue

        kind = classify_folder(child)

        if kind == "disc":
            continue

        if kind == "subgenre":
            # Process albums inside the bucket — don't rename bucket itself
            _process_level2(child, dry_run=dry_run,
                            review_items=review_items)
            continue

        if kind == "local_special":
            continue

        if kind == "album":
            _normalise_folder(child, dry_run=dry_run,
                              review_items=review_items)

        elif kind == "artist":
            _normalise_artist_folder(child, dry_run=dry_run,
                                     review_items=review_items)

        elif kind == "artist_mixed":
            _move_loose_to_singles(child, dry_run=dry_run,
                                   review_items=review_items)
            _normalise_artist_folder(child, dry_run=dry_run,
                                     review_items=review_items)

        elif kind == "artist_flat":
            _handle_artist_flat(child, review_items=review_items)

        else:  # unknown
            log.warning("Unknown folder pattern: %s — flagging for review",
                        child)
            review_items.append({
                "path": str(child),
                "reason": "unknown_pattern",
            })


def walk_folder(folder: Path, **kwargs) -> list[dict]:
    """Process all subfolders under a genre *folder*."""
    review_items: list[dict] = []
    _process_level2(folder, review_items=review_items, **kwargs)
    return review_items


def main() -> None:
    global log

    parser = argparse.ArgumentParser(
        description="Normalise folder names.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually rename folders")
    parser.add_argument("--folder", type=str, default=None,
                        help="Process a single top-level folder only")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log = setup_logger("03_folders", Path(args.log))

    if args.folder:
        folders = [config.MUSIC_ROOT / args.folder]
    else:
        skip = config.SPECIAL_FOLDERS | config.SKIP_FOLDERS
        folders = sorted(
            p for p in config.MUSIC_ROOT.iterdir()
            if p.is_dir() and p.name.lower() not in skip
        )

    all_review: list[dict] = []
    for folder in folders:
        log.info("=== Folder: %s ===", folder.name)
        items = walk_folder(folder, dry_run=dry_run)
        all_review.extend(items)

    if all_review:
        review_path = Path("review.json")
        existing: list[dict] = []
        if review_path.exists():
            try:
                existing = json.loads(review_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                pass

        if not dry_run:
            existing.extend(all_review)
            review_path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("Flagged %d folders for review", len(all_review))

    log.info("Done (%s)", "DRY-RUN" if dry_run else "LIVE")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""03_folders.py — Normalise folder names (artist/album/year, The-suffix).

Renames album folders to canonical format:
    ``Artist Name - Album Title (Year)/``

In two-level setups:
    ``Artist Name/Artist Name - Album Title (Year)/``

Never renames: SPECIAL_FOLDERS, SKIP_FOLDERS, subgenre buckets, disc
subfolders, or genre root folders themselves.

Does NOT move files. All file movement (singles, intake staging, etc.)
is handled exclusively by 04_move.py.

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
from lib.context import classify_folder, infer_best_of_folder, infer_compilation_folder, is_disc_subfolder
from lib.logger import setup_logger
from lib.parsers import parse_folder_name, sanitise_name
from lib.tags import read_tags

log: logging.Logger = None  # type: ignore[assignment]


# Album folder without year — requires space-dash-space to avoid splitting
# artist names with hyphens (e.g. AC-DC)
_FOLDER_NO_YEAR = re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+)$")

# Year in parentheses embedded in album text
_EMBEDDED_YEAR = re.compile(r"[\(\[](\d{4})[\)\]]")

# Patterns indicating multi-album/multi-era content (compilations, discographies, etc.)
_MULTI_ALBUM_INDICATORS = re.compile(
    r"\b(discography|compilation|collection|anthology|retrospective|"
    r"best of|greatest hits|box set|remaster|reissue|expanded|deluxe)",
    re.IGNORECASE
)


def _normalise_year_for_comparison(year: str) -> str | None:
    """Extract YYYY from year values with dates or annotations.
    
    Examples:
      '1987' -> '1987'
      '1987-05-09' -> '1987'
      '1987 Remaster' -> '1987'
      '2005 (Reissue)' -> '2005'
    """
    if not year:
        return None
    # Try to extract just the year part
    m = re.search(r'\d{4}', year)
    return m.group(0) if m else None


def _has_multi_album_indicators(folder_name: str) -> bool:
    """Detect if folder name suggests multi-album content."""
    return bool(_MULTI_ALBUM_INDICATORS.search(folder_name))


def _fix_the(name: str) -> str:
    """Convert ', The' / ', A' suffix to prefix."""
    if name.endswith(", The"):
        return "The " + name[:-5]
    if name.endswith(", A"):
        return "A " + name[:-3]
    return name


def _collect_consistent_tag_fields(folder: Path) -> dict[str, object]:
    """Collect consistent artist/album/year values from tags in *folder*.

    Returns:
      {
        "values": {"artist": str|None, "album": str|None, "year": str|None},
        "mixed_fields": [field, ...],
        "tag_dicts": [tag_dict, ...],
      }
    """
    # Maps lowercase key → first-seen original value, for case-insensitive dedup.
    field_values: dict[str, dict[str, str]] = {
        "artist": {},
        "album": {},
        "year": {},
    }
    tag_dicts: list[dict[str, str | None]] = []

    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(f)
        if tags is None:
            continue
        tag_dicts.append(tags)

        artist = (tags.get("artist") or "").strip()
        album = (tags.get("album") or "").strip()
        year_raw = (tags.get("year") or "").strip()
        year = _normalise_year_for_comparison(year_raw)

        if artist and artist.lower() not in field_values["artist"]:
            field_values["artist"][artist.lower()] = artist
        if album and album.lower() not in field_values["album"]:
            field_values["album"][album.lower()] = album
        if year and year not in field_values["year"]:
            field_values["year"][year] = year

    mixed_fields = sorted(
        field for field, values in field_values.items() if len(values) > 1
    )
    # next(iter(...)) is deterministic here: guarded by len == 1.
    values = {
        field: next(iter(entries.values())) if len(entries) == 1 else None
        for field, entries in field_values.items()
    }

    return {
        "values": values,
        "mixed_fields": mixed_fields,
        "tag_dicts": tag_dicts,
    }


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
    artist = sanitise_name(artist)
    album = sanitise_name(album)
    if year:
        return f"{artist} - {album} ({year})"
    return f"{artist} - {album}"


def _build_compilation_folder_name(album: str, year: str | None) -> str:
    """Build folder name for soundtracks/compilations: Album (Year)."""
    album = sanitise_name(album)
    if year:
        return f"{album} ({year})"
    return album


def _normalise_folder(
    folder: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Normalise a single album folder name."""
    name = folder.name

    parsed = _parse_album_name(name) or {
        "artist": None,
        "album": None,
        "year": None,
    }
    tag_summary = _collect_consistent_tag_fields(folder)
    is_comp = infer_compilation_folder(
        folder,
        file_tag_dicts=tag_summary["tag_dicts"],
    )
    is_best_of = infer_best_of_folder(
        folder,
        file_tag_dicts=tag_summary["tag_dicts"],
    )
    is_multi_album = _has_multi_album_indicators(name)
    mixed_fields = tag_summary["mixed_fields"]

    # Compilations/soundtracks expect mixed artists — only flag other fields.
    # Album folders (classified by " - " in name) also exclude artist from the
    # mixed check: the folder name already encodes the album artist, so a single
    # guest track with a different artist tag should not block normalisation.
    # Single-artist best-of albums have intentionally varied per-track years
    # (original recording dates) — exclude year from the mixed check too.
    # Album field is also excluded for best-ofs: tracks come from different
    # releases so mixed album tags are expected.
    # Multi-album folders (discographies, collections) also expect mixed year/album.
    _excluded = {"artist"}
    if is_best_of:
        _excluded.add("year")
        _excluded.add("album")
    if is_multi_album:
        _excluded.add("year")
        _excluded.add("album")
    effective_mixed = [f for f in mixed_fields if f not in _excluded]

    if effective_mixed:
        log.warning("Mixed tag fields for %s: %s — flagging for review",
                    folder, ", ".join(effective_mixed))
        review_items.append({
            "path": str(folder),
            "reason": "mixed_tags",
            "fields": effective_mixed,
        })
        return

    tag_values = tag_summary["values"]
    album = (tag_values["album"] or parsed["album"] or "").strip()
    year = tag_values["year"] or parsed["year"]

    if is_comp:
        missing_fields = [
            field
            for field, value in (("album", album), ("year", year))
            if not value
        ]
    else:
        artist = _fix_the((tag_values["artist"] or parsed["artist"] or "").strip())
        missing_fields = [
            field
            for field, value in (("artist", artist), ("album", album), ("year", year))
            if not value
        ]

    if missing_fields:
        log.warning("Incomplete metadata for %s (missing: %s) — flagging for review",
                    folder, ", ".join(missing_fields))
        review_items.append({
            "path": str(folder),
            "reason": "incomplete_metadata",
            "missing": missing_fields,
        })
        return

    if is_comp:
        new_name = _build_compilation_folder_name(album, year)
    else:
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
    For compilation/soundtrack context, uses Album (Year) format instead.
    """
    album = folder.name
    tag_summary = _collect_consistent_tag_fields(folder)
    is_comp = infer_compilation_folder(
        folder,
        file_tag_dicts=tag_summary["tag_dicts"],
    )

    # If the child folder name starts with the artist name it may be a
    # malformed "Artist Album" folder missing the separator.  But if the album
    # tag gives a clean title that does not itself start with the artist name,
    # trust the tag and proceed — the folder just used a bad naming convention.
    if not is_comp and album.strip().lower().startswith(artist.strip().lower()):
        tag_album = (tag_summary["values"]["album"] or "").strip()
        if not tag_album or tag_album.lower().startswith(artist.strip().lower()):
            log.warning("Folder may contain embedded artist name: %s "
                        "— flagging for review", folder)
            review_items.append({"path": str(folder), "reason": "unknown_pattern"})
            return

    # Existing folder-name fallback values
    year_m = _EMBEDDED_YEAR.search(album)
    fallback_year = None
    if year_m:
        fallback_year = year_m.group(1)
        before = album[:year_m.start()].rstrip()
        after = album[year_m.end():].lstrip()
        if before and after:
            album = before + " " + after
        else:
            album = (before or after).strip()

    is_best_of = infer_best_of_folder(
        folder,
        file_tag_dicts=tag_summary["tag_dicts"],
    )
    is_multi_album = _has_multi_album_indicators(album)
    mixed_fields = tag_summary["mixed_fields"]

    # Compilations/soundtracks expect mixed artists — only flag other fields.
    # The artist is always supplied by the parent folder, so mixed track-artist
    # tags should not block a child album rename.
    # Single-artist best-of albums have intentionally varied per-track years
    # and mixed album tags (tracks from different releases).
    # Multi-album folders (discographies, collections) also expect mixed year/album.
    _excluded = {"artist"}
    if is_best_of:
        _excluded.add("year")
        _excluded.add("album")
    if is_multi_album:
        _excluded.add("year")
        _excluded.add("album")
    effective_mixed = [f for f in mixed_fields if f not in _excluded]

    if effective_mixed:
        log.warning("Mixed tag fields for %s: %s — flagging for review",
                    folder, ", ".join(effective_mixed))
        review_items.append({
            "path": str(folder),
            "reason": "mixed_tags",
            "fields": effective_mixed,
        })
        return

    tag_values = tag_summary["values"]
    effective_album = (tag_values["album"] or album or "").strip()
    effective_year = tag_values["year"] or fallback_year

    if is_comp:
        missing_fields = [
            field
            for field, value in (
                ("album", effective_album),
                ("year", effective_year),
            )
            if not value
        ]
    else:
        effective_artist = _fix_the((tag_values["artist"] or artist or "").strip())
        missing_fields = [
            field
            for field, value in (
                ("artist", effective_artist),
                ("album", effective_album),
                ("year", effective_year),
            )
            if not value
        ]

    if missing_fields:
        log.warning("Incomplete metadata for %s (missing: %s) — flagging for review",
                    folder, ", ".join(missing_fields))
        review_items.append({
            "path": str(folder),
            "reason": "incomplete_metadata",
            "missing": missing_fields,
        })
        return

    if is_comp:
        new_name = _build_compilation_folder_name(effective_album, effective_year)
    else:
        new_name = _build_folder_name(effective_artist, effective_album, effective_year)

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
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Propose canonical rename for artist_flat folders from tags only."""
    tag_summary = _collect_consistent_tag_fields(folder)
    is_comp = infer_compilation_folder(
        folder,
        file_tag_dicts=tag_summary["tag_dicts"],
    )
    is_best_of = infer_best_of_folder(
        folder,
        file_tag_dicts=tag_summary["tag_dicts"],
    )
    mixed_fields = tag_summary["mixed_fields"]

    # Compilations/soundtracks expect mixed artists — only flag other fields.
    # Single-artist best-of albums have intentionally varied per-track years
    # and mixed album tags (tracks from different releases).
    _excluded = set()
    if is_comp:
        _excluded.add("artist")
    if is_best_of:
        _excluded.add("year")
        _excluded.add("album")
    effective_mixed = [f for f in mixed_fields if f not in _excluded]

    if effective_mixed:
        log.warning("artist_flat with mixed tag fields: %s (%s) — flagging for "
                    "review", folder, ", ".join(effective_mixed))
        review_items.append({
            "path": str(folder),
            "reason": "artist_flat_mixed",
            "fields": effective_mixed,
        })
        return

    tag_values = tag_summary["values"]
    album = (tag_values["album"] or "").strip()
    year = tag_values["year"]

    if is_comp:
        missing_fields = [
            field
            for field, value in (("album", album), ("year", year))
            if not value
        ]
    else:
        artist = _fix_the((tag_values["artist"] or "").strip())
        missing_fields = [
            field
            for field, value in (("artist", artist), ("album", album), ("year", year))
            if not value
        ]

    if missing_fields:
        log.warning("artist_flat has incomplete tags: %s (missing: %s) — "
                    "flagging for review", folder, ", ".join(missing_fields))
        review_items.append({
            "path": str(folder),
            "reason": "artist_flat_incomplete",
            "missing": missing_fields,
        })
        return

    if is_comp:
        proposed_name = _build_compilation_folder_name(album, year)
    else:
        proposed_name = _build_folder_name(artist, album, year)
    proposed_path = folder.parent / proposed_name

    if proposed_name == folder.name:
        log.debug("artist_flat already canonical: %s", folder)
        return

    if proposed_path.exists():
        log.warning("artist_flat rename conflict: %s → %s", folder.name,
                    proposed_name)
        review_items.append({
            "path": str(folder),
            "reason": "artist_flat_rename_conflict",
            "target": str(proposed_path),
        })
        return

    if dry_run:
        log.info("[DRY-RUN] Would rename: %s → %s", folder.name, proposed_name)
        review_items.append({
            "path": str(folder),
            "reason": "artist_flat_rename_proposal",
            "target": str(proposed_path),
        })
    else:
        folder.rename(proposed_path)
        log.info("Renamed: %s → %s", folder.name, proposed_name)


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
        if is_disc_subfolder(child.name):
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
            # Note: loose files in artist folders are NOT moved here.
            # File movement (singles, etc.) is handled by 04_move.py.
            _normalise_artist_folder(child, dry_run=dry_run,
                                     review_items=review_items)

        elif kind == "artist_flat":
            _handle_artist_flat(child, dry_run=dry_run,
                                review_items=review_items)

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


def _review_item_key(item: dict) -> str:
    """Return stable identity key for review item dedupe."""
    payload = {
        key: item.get(key)
        for key in sorted(item)
        if key not in {"path", "reason"}
    }
    return json.dumps(
        {
            "path": item.get("path"),
            "reason": item.get("reason"),
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


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
            seen = {
                _review_item_key(e)
                for e in existing
                if isinstance(e, dict)
            }
            for item in all_review:
                key = _review_item_key(item)
                if key not in seen:
                    existing.append(item)
                    seen.add(key)
            review_path.write_text(
                json.dumps(existing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("Flagged %d folders for review", len(all_review))

    log.info("Done (%s)", "DRY-RUN" if dry_run else "LIVE")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""01_tag.py — Fingerprint → MusicBrainz → folder genre → write tags.

Reads audio files, fingerprints via AcoustID, fetches metadata / genres
from MusicBrainz, determines folder-derived genre, detects BPM, and
writes all tags.  Existing tags are never overwritten unless ``--overwrite``
is passed.  Genre is always merged (folder + existing + MB), never replaced.

Usage:
    python 01_tag.py [--dry-run] [--folder NAME] [--overwrite] [--fix-suspicious]
                     [--no-bpm] [--no-mb] [--log FILE]
    python 01_tag.py --no-dry-run --folder jazz
"""

import argparse
import json
import logging
import re
import warnings
from collections import Counter
from pathlib import Path

import config
from lib.context import classify_folder, get_folder_context, infer_best_of_folder, infer_compilation_folder, is_disc_subfolder
from lib.genres import has_meaningful_genres, merge_genres, normalise_genre
from lib.logger import setup_logger
from lib.mb import fingerprint_lookup, mb_genres, mb_recording_metadata
from lib.parsers import parse_compact_disc_track_candidate, parse_filename, parse_folder_name
from lib.tags import read_tags, write_tags

log: logging.Logger = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# BPM detection (optional — librosa may not be installed)
# ---------------------------------------------------------------------------

_librosa = None  # module-level sentinel

def detect_bpm(path: Path) -> int | None:
    """Return estimated BPM (rounded int) or None."""
    global _librosa

    if _librosa is None:
        try:
            import librosa as lib
            _librosa = lib
        except ImportError:
            _librosa = False
            log.debug("librosa not installed — BPM detection unavailable")

    if _librosa is False:
        return None

    try:
        with warnings.catch_warnings():
            # libsndfile (soundfile) can't decode M4A/AAC natively; librosa
            # falls back to audioread which is deprecated as of 0.10.0 but
            # still functional.  Suppress both noisy warnings.
            warnings.filterwarnings("ignore", message="PySoundFile failed",
                                    category=UserWarning)
            warnings.filterwarnings("ignore", message=".*audioread.*",
                                    category=FutureWarning)
            y, sr = _librosa.load(str(path), sr=None, duration=60)
        tempo, _ = _librosa.beat.beat_track(y=y, sr=sr)
        bpm = round(float(tempo[0]) if hasattr(tempo, '__len__') else float(tempo))
        return bpm if bpm > 0 else None
    except Exception as exc:
        log.warning("BPM detection failed for %s: %s", path.name, exc)
        return None


# ---------------------------------------------------------------------------
# Process a single file
# ---------------------------------------------------------------------------

def is_suspicious(value: str | None) -> bool:
    return (value or "").strip().lower() in config.SUSPICIOUS_TAG_VALUES


_TRAILING_PAREN = re.compile(r'\(([^)]+)\)\s*$')
_DISC_IN_FOLDER_NAME = re.compile(r"[\(\[]\s*disc\s*\d+\s*[\)\]]", re.IGNORECASE)
_MULTIDISC_PARENT_CACHE: dict[Path, tuple[bool, str | None]] = {}


def _extract_title_suffix(filename_title: str, tag_title: str) -> str | None:
    """Return trailing parenthetical from *filename_title* missing in *tag_title*."""
    m = _TRAILING_PAREN.search(filename_title)
    if not m:
        return None
    suffix = m.group(0).strip()
    if suffix.lower() in tag_title.lower():
        return None
    fn_base = filename_title[:m.start()].strip()
    if fn_base.lower() == tag_title.strip().lower():
        return suffix
    return None


def _normalise_tag_number(value: str | None) -> str | None:
    if not value:
        return None
    return value.split("/")[0].strip().lstrip("0") or "0"


def _infer_multidisc_context_from_parent(parent: Path) -> tuple[bool, str | None]:
    cached = _MULTIDISC_PARENT_CACHE.get(parent)
    if cached is not None:
        return cached

    if is_disc_subfolder(parent.name):
        result = (True, "disc_subfolder")
        _MULTIDISC_PARENT_CACHE[parent] = result
        return result

    if _DISC_IN_FOLDER_NAME.search(parent.name):
        result = (True, "disc_indicator_in_folder_name")
        _MULTIDISC_PARENT_CACHE[parent] = result
        return result

    compact_discs: set[str] = set()
    try:
        children = sorted(parent.iterdir())
    except OSError:
        result = (False, None)
        _MULTIDISC_PARENT_CACHE[parent] = result
        return result

    for child in children:
        if not child.is_file() or child.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue

        sibling_parsed = parse_filename(child)
        if sibling_parsed.get("disc"):
            result = (True, "explicit_disc_track_filename")
            _MULTIDISC_PARENT_CACHE[parent] = result
            return result

        compact = parse_compact_disc_track_candidate(child)
        if compact.get("disc"):
            compact_discs.add(compact["disc"])
            if len(compact_discs) > 1:
                result = (True, "multiple_compact_disc_prefixes")
                _MULTIDISC_PARENT_CACHE[parent] = result
                return result

    result = (False, None)
    _MULTIDISC_PARENT_CACHE[parent] = result
    return result


def _interpret_compact_disc_track(
    path: Path,
    parsed: dict[str, str | None],
) -> tuple[dict[str, str | None], bool, str | None]:
    """Interpret 101-style prefixes as disc-track only in multidisc context."""
    if parsed.get("disc"):
        return parsed, False, None

    compact = parse_compact_disc_track_candidate(path)
    if not compact.get("disc") or not compact.get("track"):
        return parsed, False, None

    has_multidisc, reason = _infer_multidisc_context_from_parent(path.parent)
    if not has_multidisc:
        return parsed, False, None

    interpreted = dict(parsed)
    interpreted["disc"] = compact["disc"]
    interpreted["track"] = compact["track"]
    if not interpreted.get("title") and compact.get("title"):
        interpreted["title"] = compact["title"]
    return interpreted, True, reason


def tag_file(
    path: Path,
    *,
    dry_run: bool = True,
    overwrite: bool = False,
    fix_suspicious: bool = False,
    fix_track_mismatch: bool = False,
    no_bpm: bool = False,
    no_mb: bool = False,
    review_items: list[dict],
) -> None:
    """Tag a single audio file."""
    log.info("Processing %s", path)

    existing = read_tags(path)
    if existing is None:
        log.warning("Skipping unreadable file: %s", path)
        review_items.append({"path": str(path), "reason": "unreadable"})
        return
    new_tags: dict[str, str | None] = {}
    ctx = get_folder_context(path)
    parsed_raw = parse_filename(path)
    parsed, compact_interpreted, compact_reason = _interpret_compact_disc_track(path, parsed_raw)

    effective_existing = dict(existing)
    if fix_suspicious:
        for field in ("artist", "title", "album", "year", "track"):
            if is_suspicious(existing.get(field)):
                effective_existing[field] = None
        # Track mismatch with filename is also suspicious
        parsed_track = parsed.get("track")
        if parsed_track and existing.get("track"):
            existing_norm = existing["track"].split("/")[0].strip().lstrip("0") or "0"
            if existing_norm != parsed_track:
                effective_existing["track"] = None

    if fix_track_mismatch:
        parsed_track = parsed.get("track")
        existing_track = _normalise_tag_number(existing.get("track"))
        if parsed_track and existing_track and existing_track != parsed_track:
            effective_existing["track"] = None

        parsed_disc = parsed.get("disc")
        existing_disc = _normalise_tag_number(existing.get("disc"))
        if parsed_disc and existing_disc and existing_disc != parsed_disc:
            effective_existing["disc"] = None
        elif parsed_disc and not existing_disc:
            effective_existing["disc"] = None

    # --- Fingerprint + MusicBrainz ------------------------------------------
    needs_mb_for_identity = (
        overwrite or
        (fix_suspicious and is_suspicious(existing.get("artist"))) or
        (fix_suspicious and is_suspicious(existing.get("title"))) or
        not existing.get("artist") or
        not existing.get("title")
    )

    needs_mb_for_year = (
        not effective_existing.get("year") and
        not parse_folder_name(path.parent.name).get("year")
    )

    needs_mb_for_genre = not has_meaningful_genres(existing.get("genre"))

    needs_mb = (
        needs_mb_for_identity or
        needs_mb_for_year or
        needs_mb_for_genre
    )

    recording_id = None
    mb_title = None
    mb_artist = None
    mb_meta: dict[str, str | None] = {
        "album": None,
        "year": None,
        "track": None,
        "albumartist": None,
        "isrc": None,
    }
    mb_genre_list: list[str] = []

    if needs_mb and not no_mb:
        log.info("  API lookup: AcoustID fingerprint + MusicBrainz metadata")
        fp = fingerprint_lookup(
            path,
            min_score=config.ACOUSTID_MIN_SCORE,
            rate_limit_seconds=config.MB_RATE_LIMIT_SECONDS,
        )
        recording_id = fp.get("recording_id")
        mb_title = fp.get("title")
        mb_artist = fp.get("artist")

        if recording_id:
            log.info("  API lookup: MusicBrainz recording %s", recording_id)
            mb_meta.update(mb_recording_metadata(
                recording_id,
                rate_limit_seconds=config.MB_RATE_LIMIT_SECONDS,
            ))
            mb_genre_list = mb_genres(
                recording_id,
                min_votes=config.MB_MIN_TAG_VOTES,
                max_genres=config.MB_MAX_GENRES,
                rate_limit_seconds=config.MB_RATE_LIMIT_SECONDS,
            )
        else:
            log.info("  API lookup: no AcoustID recording match")
    elif needs_mb and no_mb:
        log.info("  API lookup required for %s but --no-mb specified — skipping lookup", path.name)

    # --- Fill missing fields from MB ----------------------------------------
    if overwrite or not effective_existing.get("title"):
        if mb_title:
            new_tags["title"] = mb_title
    if overwrite or not effective_existing.get("artist"):
        if mb_artist:
            new_tags["artist"] = mb_artist
    if overwrite or not effective_existing.get("album"):
        if mb_meta.get("album"):
            new_tags["album"] = mb_meta["album"]
    if overwrite or not effective_existing.get("year"):
        if mb_meta.get("year"):
            new_tags["year"] = mb_meta["year"]
    # Track: filename preferred over MB (MB track is release-specific)
    if overwrite or not effective_existing.get("track"):
        if parsed.get("track"):
            new_tags["track"] = parsed["track"]
        elif mb_meta.get("track"):
            new_tags["track"] = mb_meta["track"]
    if overwrite or (fix_track_mismatch and not effective_existing.get("disc")):
        if parsed.get("disc"):
            new_tags["disc"] = parsed["disc"]

    # --- Fallback: parse filename for artist / title if still missing --------
    # Only fills fields empty in BOTH existing tags and new_tags so far.
    if not effective_existing.get("title") and not new_tags.get("title"):
        if parsed.get("title"):
            new_tags["title"] = parsed["title"]
    if not effective_existing.get("artist") and not new_tags.get("artist"):
        if parsed.get("artist"):
            new_tags["artist"] = parsed["artist"]

    # --- Fallback: parse parent folder for album / year if still missing -----
    # Only fills fields empty in BOTH existing tags and new_tags so far.
    folder_info = parse_folder_name(path.parent.name)
    if not effective_existing.get("album") and not new_tags.get("album"):
        if folder_info.get("album"):
            new_tags["album"] = folder_info["album"]
    if not effective_existing.get("year") and not new_tags.get("year"):
        if folder_info.get("year"):
            new_tags["year"] = folder_info["year"]

    # --- Preserve title suffix from filename (e.g. "(Take 3)", "(Live)") -----
    final_title = new_tags.get("title") or effective_existing.get("title")
    parsed_title = parsed.get("title")
    if final_title and parsed_title:
        suffix = _extract_title_suffix(parsed_title, final_title)
        if suffix:
            enhanced = final_title + " " + suffix
            if enhanced != existing.get("title"):
                new_tags["title"] = enhanced

    # --- Flag track mismatch for review --------------------------------------
    if (parsed.get("track") and existing.get("track")
            and "track" not in new_tags):
        existing_norm = existing["track"].split("/")[0].strip().lstrip("0") or "0"
        if existing_norm != parsed.get("track"):
            log.warning("Track mismatch: tag=%s, filename=%s for %s",
                        existing_norm, parsed["track"], path.name)
            review_items.append({
                "path": str(path),
                "reason": "track_mismatch",
                "tag_track": existing_norm,
                "filename_track": parsed["track"],
                "filename_track_raw": parsed_raw.get("track"),
                "tag_disc": _normalise_tag_number(existing.get("disc")),
                "filename_disc": parsed.get("disc"),
                "compact_disc_track_interpreted": compact_interpreted,
                "multidisc_context_reason": compact_reason,
            })

    # --- Genre ---------------------------------------------------------------
    # Always merge: folder genre + existing genre + MB genres (collect_set).
    # Existing genre is included so it is never lost, only enriched.
    folder_genre = ctx.genre
    existing_genres = [
        g.strip()
        for g in (existing.get("genre") or "").split("/")
        if g.strip()
    ]
    normalised_mb = [normalise_genre(g) for g in mb_genre_list]
    genre_str = merge_genres(folder_genre, existing_genres + normalised_mb)

    if genre_str:
        # Only write if the merged result differs from what is already stored
        if genre_str != existing.get("genre"):
            new_tags["genre"] = genre_str
    else:
        log.warning("No genre resolved for %s — flagging for review", path.name)
        review_items.append({"path": str(path), "reason": "no_genre"})

    # --- AlbumArtist / ISRC --------------------------------------------------
    if (ctx.is_compilation or ctx.is_soundtrack) and not existing.get("albumartist"):
        if mb_meta.get("albumartist"):
            new_tags["albumartist"] = mb_meta["albumartist"]

    if not existing.get("isrc") and mb_meta.get("isrc"):
        new_tags["isrc"] = mb_meta["isrc"]

    # --- BPM -----------------------------------------------------------------
    if not no_bpm and not existing.get("bpm") and ctx.needs_bpm:
        bpm = detect_bpm(path)
        if bpm is not None:
            new_tags["bpm"] = str(bpm)

    # --- Strip None values ---------------------------------------------------
    new_tags = {k: v for k, v in new_tags.items() if v is not None}

    if not new_tags:
        log.debug("  No changes needed for %s", path.name)
        return

    if dry_run:
        for field, val in new_tags.items():
            log.info("  [DRY-RUN] Would set %s = %s", field, val)
    else:
        log.info("  Writing %d tag(s) to %s", len(new_tags), path.name)
        written = write_tags(path, new_tags, dry_run=False)
        for field, val in written.items():
            log.info("  Set %s = %s", field, val)


# ---------------------------------------------------------------------------
# Album consistency pass
# ---------------------------------------------------------------------------

_MAJORITY_THRESHOLD = 0.6  # ≥60% of files must agree for majority-wins


def _normalise_whitespace(s: str) -> str:
    """Collapse whitespace for comparison."""
    return " ".join(s.split())


def _pick_majority(values: list[str]) -> str | None:
    """Return the majority value if one variant has ≥ MAJORITY_THRESHOLD share.

    Comparison is case-insensitive + whitespace-normalised.  The returned
    value uses the casing of the most common raw form.
    """
    if not values:
        return None
    # Group by normalised form
    buckets: dict[str, list[str]] = {}
    for v in values:
        key = _normalise_whitespace(v).lower()
        buckets.setdefault(key, []).append(v)
    if len(buckets) <= 1:
        return None  # already consistent

    total = len(values)
    best_key = max(buckets, key=lambda k: len(buckets[k]))
    if len(buckets[best_key]) / total < _MAJORITY_THRESHOLD:
        return None  # no clear majority

    # Pick the most common raw form within the winning bucket
    from collections import Counter
    raw_counts = Counter(buckets[best_key])
    return raw_counts.most_common(1)[0][0]


def _pick_preferred_value(values: list[str]) -> str | None:
    """Return the stable raw value for a consistent or majority tag set."""
    if not values:
        return None

    buckets: dict[str, list[str]] = {}
    for value in values:
        key = _normalise_whitespace(value).lower()
        buckets.setdefault(key, []).append(value)

    from collections import Counter

    if len(buckets) == 1:
        raw_counts = Counter(values)
        return raw_counts.most_common(1)[0][0]

    return _pick_majority(values)


def _backfill_compilation_albumartist(
    folder: Path,
    file_tags: list[tuple[Path, dict[str, str | None]]],
    *,
    dry_run: bool = True,
) -> None:
    """Fill missing albumartist tags for compilation folders."""
    ctx = get_folder_context(folder)
    target = _pick_preferred_value([
        albumartist
        for _, tags in file_tags
        if (albumartist := (tags.get("albumartist") or "").strip())
    ])

    if not target and not ctx.is_soundtrack:
        target = "Various Artists"

    if not target:
        return

    for path, tags in file_tags:
        current = (tags.get("albumartist") or "").strip()
        if current:
            continue
        if dry_run:
            log.info("  [DRY-RUN] Would set albumartist = %s in %s", target, path.name)
        else:
            write_tags(path, {"albumartist": target}, dry_run=False)
            log.info("  Set albumartist = %s in %s", target, path.name)


def _consistency_pass_folder(
    folder: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Normalise album-level tags within a single album folder.

    Only touches ``artist``, ``album``, and ``year``.  Loose files in
    artist root folders are skipped — they can legitimately differ.
    """
    kind = classify_folder(folder)
    if kind not in ("album", "disc"):
        return

    audio_files = sorted(
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    )
    if len(audio_files) < 2:
        return

    # Read current tags from all files
    file_tags: list[tuple[Path, dict[str, str | None]]] = []
    for f in audio_files:
        tags = read_tags(f)
        if tags is not None:
            file_tags.append((f, tags))

    if len(file_tags) < 2:
        return

    is_compilation = infer_compilation_folder(
        folder,
        file_tag_dicts=[tags for _, tags in file_tags],
    )

    # Detect multi-artist albums: 2+ distinct artists each with ≥ 2 tracks.
    # A single outlier (1 track) is treated as a typo and still normalised.
    # Multi-artist albums (below compilation threshold) preserve per-track
    # artist tags without setting albumartist.
    _artist_vals = [
        _normalise_whitespace(v).lower()
        for _, tags in file_tags
        if (v := (tags.get("artist") or "").strip())
    ]
    _artist_counts = Counter(_artist_vals)
    _real_artists = sum(1 for c in _artist_counts.values() if c >= 2)
    is_multi_artist = _real_artists >= 2

    if is_multi_artist and not is_compilation:
        log.info("  Multi-artist album (%d artists) — preserving per-track artist tags: %s",
                 _real_artists, folder.name)

    is_best_of = infer_best_of_folder(
        folder,
        file_tag_dicts=[tags for _, tags in file_tags],
    )
    if is_best_of:
        log.info("  Single-artist best-of detected — skipping year consistency: %s",
                 folder.name)

    for field in ("artist", "album", "year"):
        if (is_compilation or is_multi_artist) and field == "artist":
            continue
        if (is_compilation or is_best_of) and field == "album":
            continue
        if is_best_of and field == "year":
            continue

        values = [(tags.get(field) or "").strip() for _, tags in file_tags]
        non_empty = [v for v in values if v]
        if not non_empty:
            continue

        # Check if already consistent (case-insensitive)
        normalised = {_normalise_whitespace(v).lower() for v in non_empty}
        if len(normalised) <= 1:
            continue

        majority = _pick_majority(non_empty)
        if majority is None:
            log.warning("  No clear majority for %s in %s (%s) — flagging",
                        field, folder.name,
                        ", ".join(sorted(normalised)))
            review_items.append({
                "path": str(folder),
                "reason": "inconsistent_tags",
                "field": field,
                "variants": sorted(normalised),
            })
            continue

        # Fix outliers
        for path, tags in file_tags:
            current = (tags.get(field) or "").strip()
            if not current:
                continue
            if _normalise_whitespace(current).lower() == _normalise_whitespace(majority).lower():
                continue
            if dry_run:
                log.info("  [DRY-RUN] Would fix %s: %r → %r in %s",
                         field, current, majority, path.name)
            else:
                write_tags(path, {field: majority}, dry_run=False)
                log.info("  Fixed %s: %r → %r in %s",
                         field, current, majority, path.name)

    if is_compilation:
        _backfill_compilation_albumartist(
            folder,
            file_tags,
            dry_run=dry_run,
        )


def _consistency_pass(
    root: Path,
    *,
    dry_run: bool = True,
    review_items: list[dict],
) -> None:
    """Walk *root* and run album consistency on every album folder."""
    for folder in sorted(root.rglob("*")):
        if not folder.is_dir():
            continue
        _consistency_pass_folder(
            folder, dry_run=dry_run, review_items=review_items,
        )
    # Also check the root itself if it's an album
    _consistency_pass_folder(
        root, dry_run=dry_run, review_items=review_items,
    )


# ---------------------------------------------------------------------------
# Walk and process
# ---------------------------------------------------------------------------

def walk_folder(folder: Path, **kwargs) -> list[dict]:
    """Process all audio files under *folder*."""
    review_items: list[dict] = []
    _MULTIDISC_PARENT_CACHE.clear()
    dry_run = kwargs.get("dry_run", True)
    no_consistency = kwargs.pop("no_consistency", False)

    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and path.is_file():
            tag_file(path, review_items=review_items, **kwargs)

    # Phase 2: normalise album-level consistency after all files are tagged
    if not no_consistency:
        _consistency_pass(folder, dry_run=dry_run, review_items=review_items)

    return review_items


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    global log

    parser = argparse.ArgumentParser(description="Tag audio files with genre and metadata.")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually write tag changes to files")
    parser.add_argument("--folder", type=str, default=None,
                        help="Process a single top-level folder only")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing artist/title/album/year tags")
    parser.add_argument("--fix-suspicious", action="store_true",
                        help="Replace suspicious placeholder values only")
    parser.add_argument("--fix-track-mismatch", action="store_true",
                        help="When filename track/disc conflicts with tags, update track/disc from filename")
    parser.add_argument("--no-bpm", action="store_true",
                        help="Skip BPM detection (fast metadata-only pass)")
    parser.add_argument("--no-mb", action="store_true",
                        help="Skip MusicBrainz/AcoustID lookup entirely")
    parser.add_argument("--no-consistency", action="store_true",
                        help="Skip album-level tag consistency pass")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log_path = Path(args.log)
    log = setup_logger("01_tag", log_path)

    if args.folder:
        folders = [config.MUSIC_ROOT / args.folder]
    else:
        folders = sorted(
            p for p in config.MUSIC_ROOT.iterdir()
            if p.is_dir() and p.name.lower() not in config.SKIP_FOLDERS
        )

    all_review: list[dict] = []
    for folder in folders:
        log.info("=== Folder: %s ===", folder.name)
        items = walk_folder(
            folder,
            dry_run=dry_run,
            overwrite=args.overwrite,
            fix_suspicious=args.fix_suspicious,
            fix_track_mismatch=args.fix_track_mismatch,
            no_bpm=args.no_bpm,
            no_mb=args.no_mb,
            no_consistency=args.no_consistency,
        )
        all_review.extend(items)

    if all_review:
        review_path = Path("review.json")
        existing_review: list[dict] = []
        if review_path.exists():
            try:
                existing_review = json.loads(review_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                pass

        if not dry_run:
            seen = {(e["path"], e["reason"]) for e in existing_review}
            for item in all_review:
                key = (item["path"], item["reason"])
                if key not in seen:
                    existing_review.append(item)
                    seen.add(key)
            review_path.write_text(
                json.dumps(existing_review, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("Flagged %d files for review", len(all_review))

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info("Done (%s)", mode)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""01_tag.py — Fingerprint → MusicBrainz → folder genre → write tags.

Reads audio files, fingerprints via AcoustID, fetches metadata / genres
from MusicBrainz, determines folder-derived genre, detects BPM, and
writes all tags.  Existing tags are never overwritten unless ``--overwrite``
is passed.  Genre is always merged (folder + existing + MB), never replaced.

Usage:
    python 01_tag.py [--dry-run] [--folder NAME] [--overwrite] [--no-bpm] [--no-mb] [--log FILE]
    python 01_tag.py --no-dry-run --folder jazz
"""

import argparse
import json
import logging
from pathlib import Path

import config
from lib.genres import merge_genres, normalise_genre
from lib.logger import setup_logger
from lib.mb import fingerprint_lookup, mb_genres, mb_recording_metadata
from lib.parsers import parse_filename, parse_folder_name
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
        y, sr = _librosa.load(str(path), sr=None, duration=60)
        tempo, _ = _librosa.beat.beat_track(y=y, sr=sr)
        bpm = round(float(tempo[0]) if hasattr(tempo, '__len__') else float(tempo))
        return bpm if bpm > 0 else None
    except Exception as exc:
        log.warning("BPM detection failed for %s: %s", path.name, exc)
        return None


# ---------------------------------------------------------------------------
# Folder → genre resolution
# ---------------------------------------------------------------------------

def resolve_folder_genre(audio_path: Path) -> str | None:
    """Determine the folder-derived genre for *audio_path*.

    * Genre folders → folder name via FOLDER_TO_GENRE.
    * ``0random`` / ``0random_good`` → subfolder name via FOLDER_TO_GENRE.
    * Special folders with no inherent genre → None.
    """
    try:
        rel = audio_path.relative_to(config.MUSIC_ROOT)
    except ValueError:
        return None

    parts = rel.parts
    if not parts:
        return None

    top = parts[0].lower()

    if top in config.SKIP_FOLDERS:
        return None
    if top in config.GENRE_FOLDERS:
        return config.FOLDER_TO_GENRE.get(top)
    if top in {"0random", "0random_good"} and len(parts) >= 3:
        subfolder = parts[1].lower()
        return config.FOLDER_TO_GENRE.get(subfolder)

    return None


# ---------------------------------------------------------------------------
# Process a single file
# ---------------------------------------------------------------------------

def tag_file(
    path: Path,
    *,
    dry_run: bool = True,
    overwrite: bool = False,
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

    # --- Fingerprint + MusicBrainz ------------------------------------------
    # Only call API if we genuinely need data that folder/filename can't provide
    needs_mb = (
        not existing.get("artist") or
        not existing.get("title") or
        not resolve_folder_genre(path)  # no folder-derived genre available
    )

    recording_id = None
    mb_title = None
    mb_artist = None
    mb_meta: dict[str, str | None] = {"album": None, "year": None, "track": None}
    mb_genre_list: list[str] = []

    if needs_mb and not no_mb:
        fp = fingerprint_lookup(
            path,
            min_score=config.ACOUSTID_MIN_SCORE,
            rate_limit_seconds=config.MB_RATE_LIMIT_SECONDS,
        )
        recording_id = fp.get("recording_id")
        mb_title = fp.get("title")
        mb_artist = fp.get("artist")

        if recording_id:
            mb_meta = mb_recording_metadata(
                recording_id,
                rate_limit_seconds=config.MB_RATE_LIMIT_SECONDS,
            )
            mb_genre_list = mb_genres(
                recording_id,
                min_votes=config.MB_MIN_TAG_VOTES,
                max_genres=config.MB_MAX_GENRES,
                rate_limit_seconds=config.MB_RATE_LIMIT_SECONDS,
            )

    # --- Fill missing fields from MB ----------------------------------------
    # Rule: only write if the field is genuinely empty in existing tags.
    # Never fall back to existing value here — that would cause no-op writes.
    if overwrite or not existing.get("title"):
        if mb_title:
            new_tags["title"] = mb_title
    if overwrite or not existing.get("artist"):
        if mb_artist:
            new_tags["artist"] = mb_artist
    if overwrite or not existing.get("album"):
        if mb_meta.get("album"):
            new_tags["album"] = mb_meta["album"]
    if overwrite or not existing.get("year"):
        if mb_meta.get("year"):
            new_tags["year"] = mb_meta["year"]
    if overwrite or not existing.get("track"):
        if mb_meta.get("track"):
            new_tags["track"] = mb_meta["track"]

    # --- Fallback: parse filename for artist / title if still missing --------
    # Only fills fields empty in BOTH existing tags and new_tags so far.
    parsed = parse_filename(path)
    if not existing.get("title") and not new_tags.get("title"):
        if parsed.get("title"):
            new_tags["title"] = parsed["title"]
    if not existing.get("artist") and not new_tags.get("artist"):
        if parsed.get("artist"):
            new_tags["artist"] = parsed["artist"]
    if not existing.get("track") and not new_tags.get("track"):
        if parsed.get("track"):
            new_tags["track"] = parsed["track"]

    # --- Fallback: parse parent folder for album / year if still missing -----
    # Only fills fields empty in BOTH existing tags and new_tags so far.
    folder_info = parse_folder_name(path.parent.name)
    if not existing.get("album") and not new_tags.get("album"):
        if folder_info.get("album"):
            new_tags["album"] = folder_info["album"]
    if not existing.get("year") and not new_tags.get("year"):
        if folder_info.get("year"):
            new_tags["year"] = folder_info["year"]

    # --- Genre ---------------------------------------------------------------
    # Always merge: folder genre + existing genre + MB genres (collect_set).
    # Existing genre is included so it is never lost, only enriched.
    folder_genre = resolve_folder_genre(path)
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

    # --- BPM -----------------------------------------------------------------
    try:
        rel = path.relative_to(config.MUSIC_ROOT)
        top = rel.parts[0].lower() if rel.parts else ""
    except ValueError:
        top = ""
    if not no_bpm and not existing.get("bpm") and top not in config.NO_BPM_FOLDERS:
        bpm = detect_bpm(path)
        if bpm is not None:
            new_tags["bpm"] = str(bpm)

    # --- Strip None values ---------------------------------------------------
    new_tags = {k: v for k, v in new_tags.items() if v is not None}

    if not new_tags:
        log.info("  No changes needed for %s", path.name)
        return

    if dry_run:
        for field, val in new_tags.items():
            log.info("  [DRY-RUN] Would set %s = %s", field, val)
    else:
        written = write_tags(path, new_tags, dry_run=False)
        for field, val in written.items():
            log.info("  Set %s = %s", field, val)


# ---------------------------------------------------------------------------
# Walk and process
# ---------------------------------------------------------------------------

def walk_folder(folder: Path, **kwargs) -> list[dict]:
    """Process all audio files under *folder*."""
    review_items: list[dict] = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() in config.AUDIO_EXTENSIONS and path.is_file():
            tag_file(path, review_items=review_items, **kwargs)
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
    parser.add_argument("--no-bpm", action="store_true",
                        help="Skip BPM detection (fast metadata-only pass)")
    parser.add_argument("--no-mb", action="store_true",
                        help="Skip MusicBrainz/AcoustID lookup entirely")
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
            no_bpm=args.no_bpm,
            no_mb=args.no_mb,
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
            existing_review.extend(all_review)
            review_path.write_text(
                json.dumps(existing_review, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        log.info("Flagged %d files for review", len(all_review))

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info("Done (%s)", mode)


if __name__ == "__main__":
    main()

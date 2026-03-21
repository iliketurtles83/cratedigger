#!/usr/bin/env python3
"""01_tag.py — Fingerprint → MusicBrainz → folder genre → write tags.

Reads audio files, fingerprints via AcoustID, fetches metadata / genres
from MusicBrainz, determines folder-derived genre, detects BPM, and
writes all tags.  Existing artist / title tags are never overwritten
unless ``--overwrite`` is passed.

Usage:
    python 01_tag.py [--dry-run] [--folder NAME] [--overwrite] [--log FILE]
"""

import argparse
import json
import logging
from pathlib import Path

import config
from lib.genres import merge_genres, normalise_genre, parent_genre
from lib.logger import setup_logger
from lib.mb import fingerprint_lookup, mb_genres, mb_recording_metadata
from lib.parsers import parse_filename, parse_folder_name
from lib.tags import read_tags, write_tags

log: logging.Logger = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# BPM detection (optional — librosa may not be installed)
# ---------------------------------------------------------------------------
try:
    import librosa  # type: ignore[import-untyped]
    _HAS_LIBROSA = True
except ImportError:
    _HAS_LIBROSA = False


def detect_bpm(path: Path) -> int | None:
    """Return estimated BPM (rounded int) or None."""
    if not _HAS_LIBROSA:
        log.debug("librosa not installed — skipping BPM detection for %s", path.name)
        return None
    try:
        y, sr = librosa.load(str(path), sr=None, duration=60)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
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

    # Skip folders → never process
    if top in config.SKIP_FOLDERS:
        return None

    # Genre folders
    if top in config.GENRE_FOLDERS:
        return config.FOLDER_TO_GENRE.get(top)

    # 0random / 0random_good → genre from subfolder
    if top in {"0random", "0random_good"} and len(parts) >= 3:
        subfolder = parts[1].lower()
        return config.FOLDER_TO_GENRE.get(subfolder)

    # Other special folders → no folder genre
    return None


# ---------------------------------------------------------------------------
# Process a single file
# ---------------------------------------------------------------------------

def tag_file(
    path: Path,
    *,
    dry_run: bool = True,
    overwrite: bool = False,
    review_items: list[dict],
) -> None:
    """Tag a single audio file."""
    log.info("Processing %s", path)

    existing = read_tags(path)
    new_tags: dict[str, str | None] = {}

    # --- Fingerprint + MusicBrainz ------------------------------------------
    fp = fingerprint_lookup(
        path,
        min_score=config.ACOUSTID_MIN_SCORE,
        rate_limit_seconds=config.MB_RATE_LIMIT_SECONDS,
    )

    recording_id = fp.get("recording_id")
    mb_title = fp.get("title")
    mb_artist = fp.get("artist")

    mb_meta: dict[str, str | None] = {"album": None, "year": None, "track": None}
    mb_genre_list: list[str] = []

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

    # --- Fill missing fields from MB (never overwrite unless --overwrite) ----
    if overwrite or not existing.get("title"):
        new_tags["title"] = mb_title or existing.get("title")
    if overwrite or not existing.get("artist"):
        new_tags["artist"] = mb_artist or existing.get("artist")
    if overwrite or not existing.get("album"):
        new_tags["album"] = mb_meta.get("album") or existing.get("album")
    if overwrite or not existing.get("year"):
        new_tags["year"] = mb_meta.get("year") or existing.get("year")
    if overwrite or not existing.get("track"):
        new_tags["track"] = mb_meta.get("track") or existing.get("track")

    # --- Fallback: parse filename for artist / title if still missing --------
    if not new_tags.get("title") or not new_tags.get("artist"):
        parsed = parse_filename(path)
        if not new_tags.get("title"):
            new_tags["title"] = parsed.get("title")
        if not new_tags.get("artist"):
            new_tags["artist"] = parsed.get("artist")
        if not new_tags.get("track") and parsed.get("track"):
            new_tags["track"] = parsed.get("track")

    # --- Fallback: parse parent folder for album / year ----------------------
    folder_info = parse_folder_name(path.parent.name)
    if not new_tags.get("album"):
        new_tags["album"] = folder_info.get("album") or existing.get("album")
    if not new_tags.get("year"):
        new_tags["year"] = folder_info.get("year") or existing.get("year")

    # --- Genre ---------------------------------------------------------------
    folder_genre = resolve_folder_genre(path)
    normalised_mb = [normalise_genre(g) for g in mb_genre_list]
    genre_str = merge_genres(folder_genre, normalised_mb)

    if genre_str:
        new_tags["genre"] = genre_str
    elif existing.get("genre"):
        pass  # keep existing genre
    else:
        log.warning("No genre resolved for %s — flagging for review", path.name)
        review_items.append({
            "path": str(path),
            "reason": "no_genre",
        })

    # --- Grouping for mood folders -------------------------------------------
    try:
        rel = path.relative_to(config.MUSIC_ROOT)
        top = rel.parts[0].lower() if rel.parts else ""
    except ValueError:
        top = ""

    if top == "0meditation" and not existing.get("grouping"):
        new_tags["grouping"] = "meditation"
    elif top == "0shacks" and not existing.get("grouping"):
        new_tags["grouping"] = "shacks"

    # --- BPM -----------------------------------------------------------------
    if not existing.get("bpm"):
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
                        help="Actually write changes")
    parser.add_argument("--folder", type=str, default=None,
                        help="Process a single top-level folder only")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing artist/title tags from MB")
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

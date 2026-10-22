#!/usr/bin/env python3
"""enrich_covers.py — Enrich missing artwork across albums and singles.

Architecture & Contract:
1. Albums: Stored as cover.jpg in the album directory. Zero audio bytes modified,
   100% audio_hash invariant, satisfies the player contract.
2. Singles: Embedded in audio tags (APIC type 3 in ID3, FLAC Picture, MP4 covr).
   Skipped on Ogg/Opus/WMA by default because tag changes alter whole-file audio_hash.
3. Fallback Order:
   - Check local candidates (front.jpg, cover.jpeg, etc.) and normalize.
   - If missing, check audio tags for mb_albumid / mb_releasegroupid.
   - If tags lack MBIDs, search MusicBrainz by artist/album.
   - Fetch front cover from Cover Art Archive (CAA).
   - Validate and resize down to max_size (default 1200px) as standard RGB JPEG.

Usage:
    python scripts/enrich_covers.py [--dry-run] [--folder NAME] [--bucket BUCKET] [--singles]
    python scripts/enrich_covers.py --no-dry-run [--folder NAME] [--bucket BUCKET] [--singles]
"""

import argparse
import io
import json
import logging
from pathlib import Path
import sys

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.context import is_disc_subfolder, is_letter_bucket
from lib.logger import setup_logger
from lib.mb import (
    fetch_caa_cover,
    fetch_caa_release_group_cover,
    mb_recording_metadata,
    search_release_mbid,
)
from lib.tags import embed_artwork, has_embedded_artwork, read_tags
from scripts.normalize_covers import (
    find_image_files,
    is_album_directory,
    normalize_cover_in_folder,
    select_cover_candidate,
)

try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

log = logging.getLogger("enrich_covers")


def process_image(data: bytes, max_size: int = 1200) -> tuple[bytes, str]:
    """Validate, reformat, and proportionally resize image data to standard RGB JPEG.

    Returns (jpeg_bytes, mime_type).
    """
    if not _HAS_PIL:
        return data, "image/jpeg"

    try:
        img = Image.open(io.BytesIO(data))
        img.load()

        needs_reformat = img.format != "JPEG" or img.mode not in ("RGB", "L")
        needs_resize = max(img.width, img.height) > max_size

        if not needs_reformat and not needs_resize:
            return data, "image/jpeg"

        # Handle alpha channels cleanly on white background
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            mask = img.split()[-1] if "A" in img.mode else None
            background.paste(img, mask=mask)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        if needs_resize:
            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90)
        return out.getvalue(), "image/jpeg"
    except Exception as exc:
        log.warning("Image processing warning: %s; using original data", exc)
        return data, "image/jpeg"


def _review_item_key(item: dict) -> str:
    """Return stable identity key for review item deduplication."""
    return f"{item.get('path')}:{item.get('reason')}"


def _write_review_items(review_items: list[dict], *, dry_run: bool) -> None:
    """Append review items to review.json with deduplication."""
    if not review_items:
        return

    review_file = Path("review.json")
    existing: list[dict] = []
    if review_file.exists():
        try:
            existing = json.loads(review_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = []

    seen = {_review_item_key(e) for e in existing if isinstance(e, dict)}
    new_entries = [item for item in review_items if _review_item_key(item) not in seen]

    if not new_entries:
        return

    if dry_run:
        log.info("[DRY-RUN] Would append %d review item(s) to review.json", len(new_entries))
    else:
        existing.extend(new_entries)
        try:
            review_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            log.info("Appended %d review item(s) to review.json", len(new_entries))
        except OSError as exc:
            log.warning("Could not write to %s: %s", review_file, exc)


def enrich_album_cover(
    folder: Path,
    *,
    dry_run: bool = True,
    max_size: int = 1200,
    review_items: list[dict] | None = None,
) -> bool:
    """Enrich cover art for an album directory.

    Returns True if artwork was added or normalized, False otherwise.
    """
    # 1. Check if canonical cover already exists and is non-empty
    for canonical in ("cover.jpg", "folder.jpg"):
        cov = folder / canonical
        if cov.exists() and cov.is_file() and cov.stat().st_size > 0:
            return False

    # 2. Check local candidates (e.g. front.jpg, cover.jpeg)
    images = find_image_files(folder)
    candidate, reason = select_cover_candidate(images)
    if candidate:
        return normalize_cover_in_folder(folder, dry_run=dry_run, review_items=review_items)
    elif reason == "ambiguous_multiple_images":
        if review_items is not None:
            review_items.append({
                "path": str(folder),
                "reason": "ambiguous_cover_art",
                "images": [i.name for i in images],
            })
        return False

    # 3. No local candidate: read tags from first audio track
    audio_files = sorted([
        f for f in folder.iterdir()
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    ])
    if not audio_files:
        return False

    sample_track = audio_files[0]
    tags = read_tags(sample_track) or {}

    mb_albumid = tags.get("mb_albumid")
    mb_releasegroupid = tags.get("mb_releasegroupid")
    artist = (tags.get("albumartist") or tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()

    # Fallback: search release by artist and album if MBID is missing
    if not mb_albumid and not mb_releasegroupid and artist and album:
        rel_id, rg_id = search_release_mbid(artist, album)
        mb_albumid = rel_id
        mb_releasegroupid = rg_id

    if not mb_albumid and not mb_releasegroupid:
        log.warning("No MBID found for album: %s (%s - %s)", folder.name, artist, album)
        if review_items is not None:
            review_items.append({
                "path": str(folder),
                "reason": "no_mbid_found",
                "artist": artist,
                "album": album,
            })
        return False

    # 4. Fetch CAA cover bytes
    image_bytes = None
    if mb_albumid:
        image_bytes = fetch_caa_cover(mb_albumid, size=str(max_size))
    if not image_bytes and mb_releasegroupid:
        image_bytes = fetch_caa_release_group_cover(mb_releasegroupid, size=str(max_size))

    if not image_bytes:
        log.info("No CAA cover art available for: %s", folder.name)
        if review_items is not None:
            review_items.append({
                "path": str(folder),
                "reason": "caa_no_cover",
                "mb_albumid": mb_albumid,
                "mb_releasegroupid": mb_releasegroupid,
            })
        return False

    # 5. Process and resize with Pillow
    jpeg_bytes, _ = process_image(image_bytes, max_size=max_size)

    # 6. Write cover.jpg
    target = folder / "cover.jpg"
    if dry_run:
        log.info("[DRY-RUN] Would write: %s (%d bytes) in %s", target.name, len(jpeg_bytes), folder.name)
    else:
        try:
            target.write_bytes(jpeg_bytes)
            log.info("Wrote: %s (%d bytes) in %s", target.name, len(jpeg_bytes), folder.name)
        except OSError as exc:
            log.error("Failed to write %s in %s: %s", target.name, folder.name, exc)
            return False

    return True


def enrich_single_track(
    track_path: Path,
    *,
    dry_run: bool = True,
    max_size: int = 1200,
    force: bool = False,
    review_items: list[dict] | None = None,
) -> bool:
    """Enrich embedded cover art for a loose single.

    Returns True if artwork was embedded (or would be in dry_run).
    """
    if has_embedded_artwork(track_path):
        return False

    suffix = track_path.suffix.lower()
    if suffix in {".ogg", ".opus", ".wma"} and not force:
        log.debug("Skipping %s (Ogg/Opus/WMA alters audio_hash)", track_path.name)
        return False

    tags = read_tags(track_path) or {}
    mb_albumid = tags.get("mb_albumid")
    mb_releasegroupid = tags.get("mb_releasegroupid")
    mb_trackid = tags.get("mb_trackid")
    artist = (tags.get("artist") or tags.get("albumartist") or "").strip()
    title = (tags.get("title") or "").strip()
    album = (tags.get("album") or "").strip()

    # If missing album ID but recording ID is present, lookup recording metadata
    if not mb_albumid and not mb_releasegroupid and mb_trackid:
        rec_meta = mb_recording_metadata(mb_trackid)
        mb_albumid = rec_meta.get("mb_albumid")
        mb_releasegroupid = rec_meta.get("mb_releasegroupid")

    # If still missing and album title present, search MusicBrainz
    if not mb_albumid and not mb_releasegroupid and artist and album:
        rel_id, rg_id = search_release_mbid(artist, album)
        mb_albumid = rel_id
        mb_releasegroupid = rg_id

    if not mb_albumid and not mb_releasegroupid:
        log.warning("No MBID found for single: %s", track_path.name)
        if review_items is not None:
            review_items.append({
                "path": str(track_path),
                "reason": "single_no_mbid",
                "artist": artist,
                "title": title,
            })
        return False

    # Fetch CAA cover bytes
    image_bytes = None
    if mb_albumid:
        image_bytes = fetch_caa_cover(mb_albumid, size=str(max_size))
    if not image_bytes and mb_releasegroupid:
        image_bytes = fetch_caa_release_group_cover(mb_releasegroupid, size=str(max_size))

    if not image_bytes:
        log.info("No CAA cover art available for single: %s", track_path.name)
        if review_items is not None:
            review_items.append({
                "path": str(track_path),
                "reason": "single_caa_no_cover",
                "mb_albumid": mb_albumid,
                "mb_releasegroupid": mb_releasegroupid,
            })
        return False

    jpeg_bytes, mime = process_image(image_bytes, max_size=max_size)

    if dry_run:
        log.info("[DRY-RUN] Would embed artwork (%d bytes) into %s", len(jpeg_bytes), track_path.name)
        return True

    success = embed_artwork(track_path, jpeg_bytes, mime_type=mime, dry_run=False, force=force)
    if success:
        log.info("Embedded artwork into %s", track_path.name)
    return success


def scan_and_enrich(
    root: Path,
    *,
    dry_run: bool = True,
    singles: bool = False,
    albums_only: bool = True,
    max_size: int = 1200,
    force: bool = False,
) -> tuple[int, int, int, int, list[dict]]:
    """Scan root and enrich missing artwork.

    Returns (albums_checked, albums_enriched, singles_checked, singles_enriched, review_items).
    """
    albums_checked = 0
    albums_enriched = 0
    singles_checked = 0
    singles_enriched = 0
    review_items: list[dict] = []

    # Check root itself if it is an album directory
    if is_album_directory(root):
        albums_checked += 1
        if enrich_album_cover(root, dry_run=dry_run, max_size=max_size, review_items=review_items):
            albums_enriched += 1

    for path in sorted(root.rglob("*")):
        if path.is_dir():
            if path.name.lower() in config.SKIP_FOLDERS:
                continue

            if path.name == "0singles":
                if singles or not albums_only:
                    for track in sorted(path.iterdir()):
                        if track.is_file() and track.suffix.lower() in config.AUDIO_EXTENSIONS:
                            singles_checked += 1
                            if enrich_single_track(
                                track,
                                dry_run=dry_run,
                                max_size=max_size,
                                force=force,
                                review_items=review_items,
                            ):
                                singles_enriched += 1
                continue

            # Standard album directory
            if is_album_directory(path):
                albums_checked += 1
                if enrich_album_cover(
                    path,
                    dry_run=dry_run,
                    max_size=max_size,
                    review_items=review_items,
                ):
                    albums_enriched += 1

        elif path.is_file() and (singles or not albums_only):
            # Check loose singles directly under root or incoming
            if path.suffix.lower() in config.AUDIO_EXTENSIONS and path.parent == root:
                singles_checked += 1
                if enrich_single_track(
                    path,
                    dry_run=dry_run,
                    max_size=max_size,
                    force=force,
                    review_items=review_items,
                ):
                    singles_enriched += 1

    return albums_checked, albums_enriched, singles_checked, singles_enriched, review_items


def resolve_buckets(bucket: str | None, buckets: str | None, all_buckets: bool) -> list[str] | None:
    """Resolve requested buckets into an ordered list of letter bucket names."""
    if all_buckets or (buckets and buckets.strip().lower() in {"all", "#,a-z"}):
        return ["#"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    if bucket:
        return [bucket.strip().upper()]
    if buckets:
        result: list[str] = []
        for part in buckets.split(","):
            part = part.strip().upper()
            if not part:
                continue
            if "-" in part and len(part) == 3 and part[1] == "-":
                start_c, end_c = part[0], part[2]
                if start_c.isalpha() and end_c.isalpha():
                    for c in range(ord(start_c), ord(end_c) + 1):
                        result.append(chr(c))
                    continue
            result.append(part)
        return result
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich missing artwork across albums (cover.jpg) and singles (embedded)."
    )
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview changes without writing (default)")
    parser.add_argument("--no-dry-run", action="store_true", help="Actually download and write artwork")
    parser.add_argument("--folder", type=str, default=None, help="Process a single folder only (relative to library or absolute)")
    parser.add_argument("--bucket", type=str, default=None, help="Process a specific letter bucket (e.g. A, B, #)")
    parser.add_argument("--buckets", type=str, default=None, help="Process comma-separated buckets or ranges (e.g. '#,A-Z' or 'A,B,C')")
    parser.add_argument("--all-buckets", action="store_true", help="Process all letter buckets (# and A-Z), skipping root special folders")
    parser.add_argument("--singles", action="store_true", help="Process loose singles in 0singles folders")
    parser.add_argument("--albums-only", action="store_true", default=False, help="Only process albums, skip singles")
    parser.add_argument("--max-size", type=int, default=1200, help="Maximum image dimension in pixels (default: 1200)")
    parser.add_argument("--force-lossy-hash", action="store_true", help="Force embedding artwork on Ogg/Opus/WMA (alters audio_hash)")
    parser.add_argument("--library-root", type=str, default=str(config.MUSIC_ROOT), help="Library root path")
    parser.add_argument("--log", type=str, default=None, help="Optional log file path")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    if args.log:
        setup_logger("enrich_covers", Path(args.log))

    lib_root = Path(args.library_root)
    mode_str = "DRY-RUN" if dry_run else "LIVE"

    target_buckets = resolve_buckets(args.bucket, args.buckets, args.all_buckets)
    if target_buckets:
        total_alb_checked = 0
        total_alb_enriched = 0
        total_sng_checked = 0
        total_sng_enriched = 0
        all_review_items: list[dict] = []

        log.info("Processing %d bucket(s): %s (%s)...", len(target_buckets), ", ".join(target_buckets), mode_str)

        for idx, b in enumerate(target_buckets, 1):
            scan_root = lib_root / b
            if not scan_root.exists():
                log.warning("Bucket folder does not exist: %s — skipping", scan_root)
                continue

            log.info("\n--- [%d/%d] Scanning bucket: %s ---", idx, len(target_buckets), b)
            alb_checked, alb_enriched, sng_checked, sng_enriched, review_items = scan_and_enrich(
                scan_root,
                dry_run=dry_run,
                singles=args.singles,
                albums_only=args.albums_only,
                max_size=args.max_size,
                force=args.force_lossy_hash,
            )

            total_alb_checked += alb_checked
            total_alb_enriched += alb_enriched
            total_sng_checked += sng_checked
            total_sng_enriched += sng_enriched
            all_review_items.extend(review_items)
            _write_review_items(review_items, dry_run=dry_run)

        log.info("\n=== Overall Artwork Enrichment Summary (%s) ===", mode_str)
        log.info("  Buckets processed:          %d", len(target_buckets))
        log.info("  Album directories checked:  %d", total_alb_checked)
        log.info("  Album covers added/fixed:   %d", total_alb_enriched)
        if args.singles or not args.albums_only:
            log.info("  Singles checked:            %d", total_sng_checked)
            log.info("  Singles embedded:           %d", total_sng_enriched)
        log.info("  Unresolvable flagged items: %d", len(all_review_items))
        return

    if args.folder:
        folder_path = Path(args.folder)
        scan_root = folder_path if folder_path.is_absolute() else lib_root / folder_path
        if not scan_root.exists():
            cwd_target = Path.cwd() / folder_path
            if cwd_target.exists():
                scan_root = cwd_target
            else:
                log.error("Target folder does not exist: %s", scan_root)
                sys.exit(1)
    else:
        scan_root = lib_root

    log.info("Scanning for artwork enrichment in %s (%s)...", scan_root, mode_str)

    alb_checked, alb_enriched, sng_checked, sng_enriched, review_items = scan_and_enrich(
        scan_root,
        dry_run=dry_run,
        singles=args.singles,
        albums_only=args.albums_only,
        max_size=args.max_size,
        force=args.force_lossy_hash,
    )

    log.info("\n=== Artwork Enrichment Summary (%s) ===", mode_str)
    log.info("  Album directories checked:  %d", alb_checked)
    log.info("  Album covers added/fixed:   %d", alb_enriched)
    if args.singles or not args.albums_only:
        log.info("  Singles checked:            %d", sng_checked)
        log.info("  Singles embedded:           %d", sng_enriched)
    log.info("  Unresolvable flagged items: %d", len(review_items))

    _write_review_items(review_items, dry_run=dry_run)


if __name__ == "__main__":
    main()

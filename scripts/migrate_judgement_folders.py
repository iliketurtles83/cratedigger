#!/usr/bin/env python3
"""Migrate judgement folders (0faves, 0random, 0random_good) into canonical layout.

Follows player feedback:
1. Deduplicates against existing album tracks via audio_hash.
2. For duplicates: points Playlists/Faves.m3u8 to the album copy and removes the redundant file.
3. For unique tracks:
   - Preserves 5-star rating (POPM=255, FMPS_Rating=1.0) on faves / random_good tracks.
   - Preserves/sets genre from random subfolder if genre is empty.
   - Moves to $bucket/$artist/0singles/ (or album folder if genuine album tag exists).
   - Updates Playlists/Faves.m3u8 with the new relative path.
   - Syncs Beets library.db paths.

Usage:
    python scripts/migrate_judgement_folders.py [--dry-run] [--fix] [--folder 0faves]
"""

import argparse
import logging
import os
from pathlib import Path
import pickle
import re
import shutil
import sqlite3
import sys

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.audiohash import audio_hash
from lib.context import get_folder_context
from lib.parsers import (
    parse_filename,
    sanitise_component,
    sanitise_name,
    validate_albumartist,
    validate_year,
)
from lib.tags import read_tags, write_tags

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("migrate_judgement_folders")

_RANDOM_SUBFOLDER_GENRE = {
    "africa": "African",
    "alternative": "Rock",
    "comedy": "Chill",
    "dancehall": "Reggae",
    "dnb": "Drum & Bass",
    "dub": "Reggae",
    "electro": "Electronic",
    "hard": "Metal",
    "hiphop": "Hip-Hop",
    "mellow": "Chill",
    "oldies": "Pop",
    "postpunk": "Post-Punk",
    "rnb-soul": "R&B",
    "trance": "Electronic",
}


def _artist_letter_bucket(artist: str | None) -> str:
    if not artist:
        return getattr(config, "SYMBOL_BUCKET", "#")
    stripped = re.sub(r"^(The|A|An)\s+", "", artist.strip(), flags=re.IGNORECASE).strip()
    first = stripped[:1].upper() if stripped else "#"
    buckets = getattr(config, "LETTER_BUCKETS", set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    return first if first in buckets else getattr(config, "SYMBOL_BUCKET", "#")


def load_features_hash_map() -> dict[str, Path]:
    features_path = getattr(config, "FEATURES_STORE_PATH", Path("features.pkl"))
    hash_map: dict[str, Path] = {}
    if features_path.exists():
        try:
            with open(features_path, "rb") as f:
                df = pickle.load(f)
            if hasattr(df, "iterrows"):
                for _, row in df.iterrows():
                    h = row.get("audio_hash")
                    p_str = row.get("path")
                    if h and p_str and h not in hash_map:
                        p = Path(p_str)
                        if p.exists():
                            hash_map[h] = p
            log.info("Loaded %d precomputed hashes from %s", len(hash_map), features_path)
        except Exception as exc:
            log.warning("Could not load features.pkl: %s", exc)
    return hash_map


def update_playlist_entry(playlist_path: Path, old_rel: str, new_rel: str) -> bool:
    """Replace an entry in .m3u8 playlist."""
    if not playlist_path.exists():
        return False
    try:
        content = playlist_path.read_text(encoding="utf-8")
        if old_rel in content:
            updated = content.replace(old_rel, new_rel)
            playlist_path.write_text(updated, encoding="utf-8")
            return True
    except Exception as exc:
        log.warning("Failed to update playlist %s: %s", playlist_path, exc)
    return False


def migrate_folder(
    folder_name: str,
    library_root: Path,
    features_cache: dict[str, Path],
    *,
    dry_run: bool = True,
    fix: bool = False,
    beets_conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    folder_path = library_root / folder_name
    if not folder_path.exists():
        log.info("Folder %s does not exist, skipping", folder_path)
        return {"total": 0, "duplicates": 0, "moved": 0}

    stats = {"total": 0, "duplicates": 0, "moved": 0}
    is_faves = folder_name == "0faves"
    is_random_good = folder_name == "0random_good"
    playlist_path = library_root / "Playlists" / "Faves.m3u8"

    audio_files = [
        f for f in folder_path.rglob("*")
        if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
    ]
    stats["total"] = len(audio_files)
    log.info("Scanning %d tracks in %s...", len(audio_files), folder_path)

    for src_file in sorted(audio_files):
        try:
            h = audio_hash(str(src_file))
        except Exception as exc:
            log.warning("Hash computation failed for %s: %s", src_file, exc)
            h = None

        existing_album_file = None
        if h and h in features_cache:
            cand = features_cache[h]
            # Ensure candidate is in standard library (not inside judgement folders)
            if cand != src_file and not any(part in {"0faves", "0random", "0random_good"} for part in cand.parts):
                existing_album_file = cand

        # 1. Duplicate Handling
        if existing_album_file:
            stats["duplicates"] += 1
            log.info("Duplicate found for %s: album copy at %s", src_file.name, existing_album_file)

            if is_faves and playlist_path.exists():
                old_rel = f"../0faves/{src_file.name}"
                try:
                    new_rel = str(Path(os.path.relpath(existing_album_file, playlist_path.parent)))
                except ValueError:
                    new_rel = str(existing_album_file)

                if fix and not dry_run:
                    if update_playlist_entry(playlist_path, old_rel, new_rel):
                        log.info("  Updated Playlists/Faves.m3u8: %s -> %s", old_rel, new_rel)

            if fix and not dry_run:
                # Remove redundant copy
                src_file.unlink()
                if beets_conn:
                    c = beets_conn.cursor()
                    c.execute("DELETE FROM items WHERE path = ?", (str(src_file).encode("utf-8"),))
                log.info("  Removed duplicate file: %s", src_file)
            else:
                log.info("  [DRY-RUN] Would remove duplicate %s", src_file.name)
            continue

        # 2. Unique Track Migration
        tags = read_tags(src_file) or {}
        parsed = parse_filename(src_file)

        artist = (tags.get("albumartist") or tags.get("artist") or parsed.get("artist") or "Unknown").strip()
        title = (tags.get("title") or parsed.get("title") or src_file.stem).strip()
        album = tags.get("album")
        year = validate_year(tags.get("year"))

        bucket = _artist_letter_bucket(artist)
        dest_filename = sanitise_component(
            f"{sanitise_name(artist)} - {sanitise_name(title)}{src_file.suffix.lower()}"
        )

        # If genuine album release exists and not a placeholder folder name
        is_album_release = (
            album
            and album.strip().lower() not in {"0faves", "favorites", "faves", "0random", "0random_good"}
            and year
        )

        if is_album_release:
            album_folder = sanitise_component(
                f"{sanitise_name(artist)} - {sanitise_name(album)} ({year})"
            )
            dest_dir = (
                library_root / bucket / sanitise_component(sanitise_name(artist)) / album_folder
            )
        else:
            dest_dir = (
                library_root / bucket / sanitise_component(sanitise_name(artist)) / "0singles"
            )

        dest_file = dest_dir / dest_filename

        tag_updates: dict[str, str | None] = {}
        # Ensure ratings are set
        if (is_faves or is_random_good) and not tags.get("rating"):
            tag_updates["rating"] = "1.0"

        # Ensure genre is preserved from random folder if missing
        if folder_name.startswith("0random") and not tags.get("genre"):
            sub = src_file.parent.name.lower()
            if sub in config.FOLDER_TO_GENRE:
                tag_updates["genre"] = config.FOLDER_TO_GENRE[sub]
            elif sub in _RANDOM_SUBFOLDER_GENRE:
                tag_updates["genre"] = _RANDOM_SUBFOLDER_GENRE[sub]

        if not is_album_release and tags.get("album"):
            # Empty out fake album name for singles
            if tags["album"].strip().lower() in {"0faves", "favorites", "faves", "0random", "0random_good"}:
                tag_updates["album"] = ""

        stats["moved"] += 1
        prefix = "[DRY-RUN] Would move" if dry_run else "Moving"
        log.info("%s: %s -> %s", prefix, src_file.name, dest_file)

        if fix and not dry_run:
            if tag_updates:
                write_tags(src_file, tag_updates, dry_run=False)

            dest_dir.mkdir(parents=True, exist_ok=True)
            if dest_file.exists():
                log.warning("Destination exists, skipping move: %s", dest_file)
                continue

            shutil.move(str(src_file), str(dest_file))

            # Update playlist if in faves
            if is_faves and playlist_path.exists():
                old_rel = f"../0faves/{src_file.name}"
                try:
                    new_rel = str(Path(os.path.relpath(dest_file, playlist_path.parent)))
                except ValueError:
                    new_rel = str(dest_file)
                update_playlist_entry(playlist_path, old_rel, new_rel)

            # Update Beets DB item
            if beets_conn:
                c = beets_conn.cursor()
                c.execute(
                    "UPDATE items SET path = ? WHERE path = ?",
                    (str(dest_file).encode("utf-8"), str(src_file).encode("utf-8")),
                )

            # Update features cache
            if h:
                features_cache[h] = dest_file

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate migration (default)")
    parser.add_argument("--no-dry-run", action="store_false", dest="dry_run", help="Execute migration")
    parser.add_argument("--fix", action="store_true", help="Apply file moves and tag updates")
    parser.add_argument("--folder", type=str, default="all", help="Target folder (0faves, 0random, 0random_good, or all)")
    args = parser.parse_args()

    library_root = Path(getattr(config, "MUSIC_ROOT", Path("/media/jack/buffer/audio")))
    features_cache = load_features_hash_map()

    db_path = Path(getattr(config, "BEETS_DB_PATH", Path.home() / ".config" / "beets" / "library.db"))
    beets_conn = sqlite3.connect(db_path) if args.fix and not args.dry_run else None

    folders = ["0faves", "0random", "0random_good"] if args.folder == "all" else [args.folder]

    total_stats = {"total": 0, "duplicates": 0, "moved": 0}
    for folder in folders:
        s = migrate_folder(
            folder,
            library_root,
            features_cache,
            dry_run=args.dry_run and not args.fix,
            fix=args.fix,
            beets_conn=beets_conn,
        )
        for k in total_stats:
            total_stats[k] += s.get(k, 0)

    if beets_conn:
        beets_conn.commit()
        beets_conn.close()

    log.info("Migration Summary: %d total scanned, %d duplicates resolved, %d singles/albums routed. (dry_run=%s, fix=%s)",
             total_stats["total"], total_stats["duplicates"], total_stats["moved"], args.dry_run and not args.fix, args.fix)


if __name__ == "__main__":
    main()

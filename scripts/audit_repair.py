#!/usr/bin/env python3
"""Audit and repair metadata on buffer audio using the monodong backup and hygiene rules.

Detects and optionally fixes:
1. Junk genre tags (e.g., tokens matching 'genre', 'unknown', 'other', 'good', etc.)
2. Flattened / remapped genres where the backup preserves the richer/original tag
   (e.g., Ambient -> Chill, Techno -> Electronic)
3. Solo / consistency overwrites where backup preserves artist credits (e.g. feat. credits)
4. Erroneously overwritten album / year tags from earlier MusicBrainz single-track lookups.

Usage:
    python scripts/audit_repair.py [--dry-run] [--folder FOLDER] [--fix]
    python scripts/audit_repair.py --no-dry-run --folder chill --fix
"""

import argparse
import logging
from pathlib import Path
import re
import sys

# Ensure repository root is in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.genres import GENRE_BLACKLIST, normalise_genre
from lib.tags import read_tags, write_tags

log = logging.getLogger("audit_repair")

JUNK_GENRE_TOKENS = set(GENRE_BLACKLIST) | {
    "genre",
    "genres",
    "track",
    "audio",
    "music",
    "untitled",
}


RANDOM_SUBFOLDER_MAP = {
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


def resolve_fallback_genre(target_file: Path, target_root: Path) -> str | None:
    """Resolve fallback display genre from directory path or special policy."""
    try:
        rel = target_file.relative_to(target_root)
    except ValueError:
        return None

    if not rel.parts:
        return None

    top = rel.parts[0].lower()
    # 1. Direct top-level genre folder
    if top in config.FOLDER_TO_GENRE:
        return config.FOLDER_TO_GENRE[top]

    # 2. Subfolder under 0random / 0random_good
    if len(rel.parts) > 1 and top in {"0random", "0random_good"}:
        sub = rel.parts[1].lower()
        if sub in config.FOLDER_TO_GENRE:
            return config.FOLDER_TO_GENRE[sub]
        if sub in RANDOM_SUBFOLDER_MAP:
            return RANDOM_SUBFOLDER_MAP[sub]

    # 3. Special folder policy fallback
    if top in getattr(config, "SPECIAL_FOLDER_GENRE_POLICY", {}):
        for src in config.SPECIAL_FOLDER_GENRE_POLICY[top]:
            if src.startswith("fallback:"):
                return normalise_genre(src.split(":", 1)[1].strip())

    return None


def clean_genre_tag(genre_str: str | None) -> tuple[str | None, list[str]]:
    """Remove junk/blacklisted tokens from a slash-separated genre string.

    Returns (cleaned_genre_str, removed_tokens).
    """
    if not genre_str:
        return None, []

    tokens = [t.strip() for t in genre_str.split("/") if t.strip()]
    cleaned: list[str] = []
    removed: list[str] = []
    seen = set()

    for tok in tokens:
        norm = normalise_genre(tok)
        low = norm.lower()
        if low in JUNK_GENRE_TOKENS or not low:
            removed.append(tok)
            continue
        if low not in seen:
            seen.add(low)
            cleaned.append(norm)

    result_str = " / ".join(cleaned) if cleaned else None
    return result_str, removed


def find_matching_backup_file(target_file: Path, target_root: Path, backup_root: Path) -> Path | None:
    """Attempt to locate the corresponding backup file for target_file."""
    try:
        rel = target_file.relative_to(target_root)
    except ValueError:
        return None

    # 1. Exact relative path match
    exact = backup_root / rel_path if (rel_path := rel) else None
    if exact and exact.exists() and exact.is_file():
        return exact

    # 2. Check parent directory in backup for same filename
    parent_in_backup = backup_root / rel.parent
    if parent_in_backup.exists() and parent_in_backup.is_dir():
        candidate = parent_in_backup / target_file.name
        if candidate.exists():
            return candidate

    return None


def audit_file(
    target_file: Path,
    target_root: Path,
    backup_root: Path,
) -> dict[str, object]:
    """Audit a single audio file and compare with backup if available."""
    issue = {
        "path": str(target_file),
        "junk_genres": [],
        "genre_repair": None,
        "artist_repair": None,
        "album_repair": None,
        "has_issues": False,
    }

    current_tags = read_tags(target_file)
    if not current_tags:
        return issue

    # 1. Check for junk genre tokens
    raw_genre = current_tags.get("genre")
    cleaned_genre, removed_tokens = clean_genre_tag(raw_genre)
    if removed_tokens:
        issue["junk_genres"] = removed_tokens
        if not cleaned_genre:
            cleaned_genre = resolve_fallback_genre(target_file, target_root)
        issue["genre_repair"] = cleaned_genre
        issue["has_issues"] = True

    # 2. Compare against backup if available
    backup_file = find_matching_backup_file(target_file, target_root, backup_root)
    if backup_file:
        backup_tags = read_tags(backup_file)
        if backup_tags:
            b_artist = backup_tags.get("artist")
            c_artist = current_tags.get("artist")
            if b_artist and c_artist and b_artist != c_artist:
                # If backup had a longer artist string (e.g. feat. credits) that got truncated
                if len(b_artist) > len(c_artist) and c_artist.lower() in b_artist.lower():
                    issue["artist_repair"] = b_artist
                    issue["has_issues"] = True

            b_genre = backup_tags.get("genre")
            if b_genre and raw_genre and b_genre != raw_genre:
                # Check if backup had subgenres that got flattened (e.g. Ambient, Techno, Disco)
                for unflatten in ("ambient", "downtempo", "techno", "disco", "garage punk", "post-punk"):
                    if unflatten in b_genre.lower() and unflatten not in (cleaned_genre or raw_genre).lower():
                        # Prefer combining the backup genre with cleaned current genre
                        combined = f"{b_genre} / {cleaned_genre or raw_genre}"
                        c_genre, _ = clean_genre_tag(combined)
                        issue["genre_repair"] = c_genre
                        issue["has_issues"] = True
                        break

            b_album = backup_tags.get("album")
            c_album = current_tags.get("album")
            if b_album and c_album and b_album != c_album:
                # If album got changed to a known single-track misattribution like "The Best Of"
                c_low = c_album.lower()
                if "best of" in c_low or "greatest hits" in c_low or "anthology" in c_low:
                    if "best of" not in b_album.lower() and "greatest hits" not in b_album.lower():
                        issue["album_repair"] = b_album
                        issue["has_issues"] = True

    return issue


def run_audit(
    target_root: Path,
    backup_root: Path,
    folder: str | None = None,
    dry_run: bool = True,
    fix: bool = False,
) -> None:
    """Run audit and optional repair across target_root."""
    if folder:
        folder_names = [f.strip() for f in folder.split(",") if f.strip()]
        scan_dirs = [target_root / f for f in folder_names]
    else:
        scan_dirs = sorted([p for p in target_root.iterdir() if p.is_dir() and p.name not in config.SKIP_FOLDERS])

    total_scanned = 0
    total_issues = 0
    fixed_count = 0

    log.info("Starting audit on %s (backup: %s, dry_run=%s, fix=%s)", target_root, backup_root, dry_run, fix)

    for s_dir in scan_dirs:
        if not s_dir.exists():
            log.warning("Directory does not exist: %s", s_dir)
            continue

        log.info("=== Scanning: %s ===", s_dir.name)
        for path in sorted(s_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
                continue

            total_scanned += 1
            res = audit_file(path, target_root, backup_root)
            if not res["has_issues"]:
                continue

            total_issues += 1
            log.info("Issue in %s:", path.name)
            repair_tags: dict[str, str | None] = {}

            if res["junk_genres"]:
                log.info("  Junk genre tokens found: %s", res["junk_genres"])
            if res["genre_repair"]:
                log.info("  Genre candidate: -> %s", res["genre_repair"])
                repair_tags["genre"] = str(res["genre_repair"])
            if res["artist_repair"]:
                log.info("  Artist restore: -> %s", res["artist_repair"])
                repair_tags["artist"] = str(res["artist_repair"])
            if res["album_repair"]:
                log.info("  Album restore:  -> %s", res["album_repair"])
                repair_tags["album"] = str(res["album_repair"])

            if fix and repair_tags:
                if dry_run:
                    log.info("  [DRY-RUN] Would repair: %s", repair_tags)
                else:
                    write_tags(path, repair_tags, dry_run=False)
                    log.info("  [REPAIRED] Wrote %s", repair_tags)
                    fixed_count += 1

    mode = "DRY-RUN" if dry_run else "LIVE"
    log.info("Audit complete (%s): %d files scanned, %d issues found, %d repaired.", mode, total_scanned, total_issues, fixed_count)


def main():
    parser = argparse.ArgumentParser(description="Audit and repair tags using backup and hygiene rules.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview without writing")
    parser.add_argument("--no-dry-run", action="store_true", help="Apply fixes")
    parser.add_argument("--folder", type=str, default=None, help="Specific folder (e.g. chill, electronic)")
    parser.add_argument("--fix", action="store_true", help="Apply proposed fixes")
    parser.add_argument("--target-root", type=str, default=str(config.MUSIC_ROOT))
    parser.add_argument("--backup-root", type=str, default="/media/jack/monodong/audio2")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    dry_run = not args.no_dry_run
    run_audit(
        target_root=Path(args.target_root),
        backup_root=Path(args.backup_root),
        folder=args.folder,
        dry_run=dry_run,
        fix=args.fix,
    )


if __name__ == "__main__":
    main()

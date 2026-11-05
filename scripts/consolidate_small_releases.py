#!/usr/bin/env python3
"""Audit and consolidate incomplete small album and compilation folders.

Identifies folders with <= threshold tracks (default 3):
- Re-homes orphan compilation tracks in 0compilations/ to per-artist 0singles/.
- Re-homes incomplete artist album tracks to the artist's 0singles/.
- Sets album = "" (Option 1) to eliminate fragmented 1-track albums in music players.
- Preserves legitimate complete EPs/singles (where track_count >= track_total or tagged as EP).
- Resolves conflicts using audio_hash matching (deduplicates identical audio, creates
  deterministic [alternate-<hash8>] candidates for distinct audio).
- Keeps downstream playlists and features.pkl synchronized.

Usage:
    python scripts/consolidate_small_releases.py --dry-run
    python scripts/consolidate_small_releases.py --folder "Y" --dry-run
    python scripts/consolidate_small_releases.py --folder "0compilations" --dry-run
    python scripts/consolidate_small_releases.py --folder "0compilations" --fix --no-dry-run
    python scripts/consolidate_small_releases.py --all --fix --no-dry-run
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from lib.audiohash import audio_hash
from lib.context import (
    artist_letter_bucket,
    is_letter_bucket,
    _extract_primary_artist,
)
from lib.logger import setup_logger
from lib.parsers import (
    artist_comparison_key,
    build_filename,
    parse_filename,
    sanitise_component,
    sanitise_name,
)
from lib.path_updates import update_downstream_paths
from lib.tags import read_tags, write_tags

log = logging.getLogger("consolidate_small_releases")

_EP_SINGLE_PATTERNS = re.compile(
    r"\b(ep|e\.p\.|single|cds|maxi|maxi-single|7\"|12\"|vinyl)\b",
    re.IGNORECASE,
)


@dataclass
class ReleaseAnalysis:
    folder_path: str
    folder_type: str  # "compilation" | "artist_album"
    artist_name: str
    track_count: int
    is_complete_ep: bool
    reason: str
    tracks: list[dict] = field(default_factory=list)


def is_complete_release(
    folder: Path,
    audio_files: list[Path],
    file_tags: list[dict[str, str | None]],
) -> tuple[bool, str]:
    """Determine if a folder represents a complete EP or Single release."""
    cnt = len(audio_files)
    if cnt == 0:
        return False, "empty_folder"

    folder_name = folder.name
    # 1. Explicit track totals in tags
    totals: list[int] = []
    track_nums: list[int] = []

    for tags in file_tags:
        tr = tags.get("track") or ""
        m = re.match(r"^\s*(\d+)(?:\s*/\s*(\d+))?", tr)
        if m:
            track_nums.append(int(m.group(1)))
            if m.group(2):
                totals.append(int(m.group(2)))

    if totals:
        max_total = max(totals)
        if cnt >= max_total:
            return True, f"complete_tagged_total ({cnt}/{max_total})"
        else:
            return False, f"incomplete_tagged_total ({cnt}/{max_total})"

    # 2. Track numbering indicates higher track exists
    if track_nums and max(track_nums) > cnt:
        return False, f"incomplete_track_numbering (max track {max(track_nums)} > {cnt} files)"

    # 3. EP / Single keywords in folder name
    if _EP_SINGLE_PATTERNS.search(folder_name):
        # If tracks are sequential e.g. [1, 2] or cnt == 1
        return True, "ep_keyword_in_folder"

    # 4. Check album tag for EP keywords
    album_tags = {tags.get("album") for tags in file_tags if tags.get("album")}
    if any(_EP_SINGLE_PATTERNS.search(alb) for alb in album_tags if alb):
        return True, "ep_keyword_in_album_tag"

    # Otherwise, 1-3 tracks from a regular album is treated as incomplete
    return False, "no_completeness_evidence"


def _unique_alternate_path(path: Path, digest: str) -> Path:
    """Return a non-colliding alternate path with deterministically derived suffix."""
    suffix = digest.split(":", 1)[-1][:8]
    candidate = path.with_name(f"{path.stem} [alternate-{suffix}]{path.suffix}")
    number = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} [alternate-{suffix}-{number}]{path.suffix}")
        number += 1
    return candidate


def _remove_empty_directory(folder: Path, *, dry_run: bool) -> None:
    """Recursively remove empty directory, discarding leftover artwork if only scans/artwork remain."""
    if not folder.exists() or not folder.is_dir():
        return
    remaining = list(folder.iterdir())
    # If only image / text files remain (e.g. cover.jpg, folder.jpg, .nfo)
    ignorable_suffixes = {".jpg", ".jpeg", ".png", ".txt", ".nfo", ".cue", ".log", ".m3u", ".m3u8"}
    if all(f.is_file() and f.suffix.lower() in ignorable_suffixes for f in remaining):
        if dry_run:
            log.info("[DRY-RUN] Would remove empty folder and non-audio assets: %s", folder)
        else:
            try:
                for f in remaining:
                    f.unlink()
                folder.rmdir()
                log.info("Removed dissolved directory: %s", folder)
            except OSError as e:
                log.warning("Could not remove directory %s: %s", folder, e)


def consolidate_small_releases(
    *,
    dry_run: bool = True,
    folder: str | None = None,
    all_buckets: bool = False,
    threshold: int = 3,
    report_path: Path | None = None,
) -> dict:
    """Scan and consolidate small releases."""
    music_root = config.MUSIC_ROOT
    candidate_folders: list[tuple[Path, str, str]] = []  # (folder_path, folder_type, artist_name)

    if folder:
        target = music_root / folder
        if not target.exists():
            log.error("Target path does not exist: %s", target)
            return {"error": f"Path not found: {target}"}

        if target.name == "0compilations":
            for d in sorted(target.iterdir()):
                if d.is_dir():
                    candidate_folders.append((d, "compilation", "Various Artists"))
        elif is_letter_bucket(target.name):
            for artist_dir in sorted(target.iterdir()):
                if not artist_dir.is_dir():
                    continue
                for album_dir in sorted(artist_dir.iterdir()):
                    if album_dir.is_dir() and album_dir.name != config.ARTIST_SINGLES_FOLDER:
                        candidate_folders.append((album_dir, "artist_album", artist_dir.name))
        elif target.parent == music_root and is_letter_bucket(target.parent.name):
            # Target is an artist folder
            for album_dir in sorted(target.iterdir()):
                if album_dir.is_dir() and album_dir.name != config.ARTIST_SINGLES_FOLDER:
                    candidate_folders.append((album_dir, "artist_album", target.name))
        else:
            # Single album folder
            folder_type = "compilation" if "0compilations" in target.parts else "artist_album"
            artist_name = target.parent.name if folder_type == "artist_album" else "Various Artists"
            candidate_folders.append((target, folder_type, artist_name))
    else:
        # Scan 0compilations and/or all letter buckets
        comp_root = music_root / "0compilations"
        if comp_root.exists():
            for d in sorted(comp_root.iterdir()):
                if d.is_dir():
                    candidate_folders.append((d, "compilation", "Various Artists"))

        if all_buckets:
            for b in sorted(config.LETTER_BUCKETS | {config.SYMBOL_BUCKET}):
                b_path = music_root / b
                if not b_path.exists():
                    continue
                for artist_dir in sorted(b_path.iterdir()):
                    if not artist_dir.is_dir():
                        continue
                    for album_dir in sorted(artist_dir.iterdir()):
                        if album_dir.is_dir() and album_dir.name != config.ARTIST_SINGLES_FOLDER:
                            candidate_folders.append((album_dir, "artist_album", artist_dir.name))

    log.info("Starting small release audit across %d candidate folders...", len(candidate_folders))
    log.info("Mode: %s (threshold: <= %d tracks)", "DRY-RUN" if dry_run else "LIVE", threshold)

    complete_eps: list[ReleaseAnalysis] = []
    incompletes_to_consolidate: list[ReleaseAnalysis] = []
    skipped_large: int = 0

    moved_paths: dict[Path, Path] = {}
    stats = Counter()

    for folder_dir, folder_type, artist_context in candidate_folders:
        audio_files = [
            f for f in folder_dir.rglob("*")
            if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
        ]
        track_count = len(audio_files)

        if track_count == 0:
            stats["empty_folders"] += 1
            if not dry_run:
                _remove_empty_directory(folder_dir, dry_run=False)
            continue

        if track_count > threshold:
            skipped_large += 1
            continue

        # In 0compilations, any folder with <= threshold tracks is an incomplete compilation
        if folder_type == "compilation":
            is_comp, reason = False, "incomplete_compilation"
        else:
            file_tags = [read_tags(f) or {} for f in audio_files]
            is_comp, reason = is_complete_release(folder_dir, audio_files, file_tags)

        analysis = ReleaseAnalysis(
            folder_path=str(folder_dir.relative_to(music_root)),
            folder_type=folder_type,
            artist_name=artist_context,
            track_count=track_count,
            is_complete_ep=is_comp,
            reason=reason,
        )

        if is_comp:
            complete_eps.append(analysis)
            stats["complete_eps_preserved"] += 1
            continue

        # Incomplete release -> plan relocation
        for audio_path in sorted(audio_files):
            t = read_tags(audio_path) or {}
            track_artist = (t.get("artist") or "").strip()
            if not track_artist and " - " in audio_path.name:
                cand = audio_path.name.split(" - ")[0].strip()
                if not re.match(r"^\d+(?:-\d+)?$", cand):
                    track_artist = cand

            if folder_type == "compilation":
                primary_artist = _extract_primary_artist(track_artist) if track_artist else None
            else:
                primary_artist = artist_context

            title = (t.get("title") or "").strip()
            if not title:
                parsed = parse_filename(audio_path)
                title = parsed.get("title") or audio_path.stem

            if not primary_artist:
                dest_dir = music_root / config.ROOT_SINGLES_FOLDER
                dest_name = f"{sanitise_component(title)}{audio_path.suffix.lower()}"
            else:
                bucket = artist_letter_bucket(primary_artist)
                dest_dir = music_root / bucket / primary_artist / config.ARTIST_SINGLES_FOLDER
                dest_name = f"{sanitise_component(primary_artist)} - {sanitise_component(title)}{audio_path.suffix.lower()}"

            dest_path = dest_dir / dest_name

            analysis.tracks.append({
                "source": str(audio_path.relative_to(music_root)),
                "dest": str(dest_path.relative_to(music_root)),
                "artist": primary_artist,
                "title": title,
            })

        incompletes_to_consolidate.append(analysis)
        stats["incomplete_releases_consolidated"] += 1
        stats["tracks_consolidated"] += len(analysis.tracks)

    # Execution phase if not dry_run
    if not dry_run:
        for analysis in incompletes_to_consolidate:
            folder_dir = music_root / analysis.folder_path
            for trk in analysis.tracks:
                src_path = music_root / trk["source"]
                dest_path = music_root / trk["dest"]

                if not src_path.exists():
                    continue

                dest_path.parent.mkdir(parents=True, exist_ok=True)

                # Check conflict
                if dest_path.exists():
                    src_hash = audio_hash(src_path)
                    dest_hash = audio_hash(dest_path)
                    if src_hash and dest_hash and src_hash == dest_hash:
                        log.info("Duplicate audio found: removing %s (matches %s)", src_path.name, dest_path.name)
                        moved_paths[src_path] = dest_path
                        src_path.unlink()
                        stats["duplicates_removed"] += 1
                        continue
                    else:
                        dest_path = _unique_alternate_path(dest_path, src_hash or "alt")
                        log.warning("Alternate created for collision: %s -> %s", src_path.name, dest_path.name)
                        stats["alternates_created"] += 1

                # Update tags: Option 1 (album = "")
                artist_val = trk["artist"] or ""
                update_fields = {
                    "album": "",
                    "track": "",
                    "disc": "",
                    "compilation": "0",
                    "mb_albumid": "",
                    "mb_albumartistid": "",
                    "mb_releasegroupid": "",
                }
                if artist_val:
                    update_fields["albumartist"] = artist_val

                try:
                    write_tags(src_path, update_fields, dry_run=False)
                except Exception as e:
                    log.warning("Could not update tags on %s: %s", src_path, e)

                # Move file
                shutil.move(str(src_path), str(dest_path))
                moved_paths[src_path] = dest_path
                log.info("Moved: %s -> %s", src_path.name, dest_path)

            # Dissolve folder if empty
            _remove_empty_directory(folder_dir, dry_run=False)

        # Update downstream playlists and features.pkl
        if moved_paths:
            update_downstream_paths(moved_paths, music_root=music_root, logger=log)
            log.info("Updated downstream paths for %d moved tracks", len(moved_paths))

    print("\n" + "=" * 60)
    print(f"SMALL RELEASES CONSOLIDATION ({'DRY-RUN' if dry_run else 'LIVE'})")
    print("=" * 60)
    print(f"Candidate folders evaluated:       {len(candidate_folders):6d}")
    print(f"Releases preserved (Complete EP):  {len(complete_eps):6d}")
    print(f"Incomplete releases to dissolve:   {len(incompletes_to_consolidate):6d}")
    print(f"Tracks to relocate to 0singles:    {stats['tracks_consolidated']:6d}")
    if not dry_run:
        print(f"Duplicates removed:                {stats['duplicates_removed']:6d}")
        print(f"Alternates created:                {stats['alternates_created']:6d}")

    if complete_eps:
        print("\n--- Sample Preserved Complete EPs (Kept in Place) ---")
        for ep in complete_eps[:8]:
            print(f"  [EP] {ep.folder_path} ({ep.track_count} tracks) - {ep.reason}")

    if incompletes_to_consolidate:
        print("\n--- Sample Incomplete Releases to Consolidate into 0singles ---")
        for inc in incompletes_to_consolidate[:10]:
            print(f"  [-] {inc.folder_path} ({inc.track_count} tracks) - {inc.reason}")
            for trk in inc.tracks[:2]:
                print(f"       -> {trk['dest']}")

    summary = {
        "dry_run": dry_run,
        "threshold": threshold,
        "candidate_folders_evaluated": len(candidate_folders),
        "preserved_complete_eps": [asdict(e) for e in complete_eps],
        "incompletes_consolidated": [asdict(i) for i in incompletes_to_consolidate],
        "stats": dict(stats),
    }

    if report_path:
        report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Wrote report to: %s", report_path)
        print(f"\nDetailed report saved to: {report_path}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and consolidate incomplete small releases.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview consolidation without moving files (default)")
    parser.add_argument("--no-dry-run", action="store_false", dest="dry_run", help="Execute consolidation")
    parser.add_argument("--fix", action="store_true", help="Required with --no-dry-run to execute")
    parser.add_argument("--folder", type=str, default=None, help="Specific folder (e.g. '0compilations', 'Y', or artist folder)")
    parser.add_argument("--all", action="store_true", help="Process all letter buckets and 0compilations")
    parser.add_argument("--threshold", type=int, default=3, help="Max track count for small releases (default: 3)")
    parser.add_argument("--report", type=str, default="small_releases_report.json", help="Path to write JSON report")

    args = parser.parse_args()
    setup_logger("consolidate_small_releases")

    if not args.dry_run and not args.fix:
        parser.error("--no-dry-run requires --fix to modify filesystem and tags.")

    if not args.folder and not args.all:
        parser.error("Specify --folder <NAME> (e.g. '0compilations', 'Y') or --all to scan the full library.")

    report_file = Path(args.report) if args.report else None
    consolidate_small_releases(
        dry_run=args.dry_run,
        folder=args.folder,
        all_buckets=args.all,
        threshold=args.threshold,
        report_path=report_file,
    )


if __name__ == "__main__":
    main()

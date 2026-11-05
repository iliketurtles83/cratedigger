#!/usr/bin/env python3
"""scripts/generate_genre_playlists.py — Bring out hand-picked singles as top-level genre playlists.

Scans all loose singles (across all 0singles/ folders, Faves.m3u8, 0random/), resolves their
canonical top-level root genre, and generates clean M3U8 playlists in Playlists/:
    Playlists/Singles - Electronic.m3u8
    Playlists/Singles - Rock.m3u8
    ...
    Playlists/All_Singles.m3u8

Usage:
    python scripts/generate_genre_playlists.py [--dry-run]
    python scripts/generate_genre_playlists.py --fix --no-dry-run
"""

import argparse
import logging
from collections import defaultdict
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import config
from lib.genres import CHILD_TO_PARENT, ROOT_GENRES, normalise_genre
from lib.logger import setup_logger
from lib.parsers import parse_filename
from lib.tags import read_tags

log = logging.getLogger("generate_genre_playlists")

# Common aliases mapping subgenres/variants to top-level canonical root genres
_GENRE_ALIASES: dict[str, str] = {
    "dance hall": "Reggae",
    "dancehall": "Reggae",
    "disco": "Electronic",
    "alternative": "Rock",
    "alternative metal": "Metal",
    "groove metal": "Metal",
    "deathcore": "Metal",
    "top 40": "Pop",
    "art pop": "Pop",
    "happy hardcore": "Electronic",
    "club": "Electronic",
    "club-house": "Electronic",
    "psy-trance": "Electronic",
    "italo-disco": "Electronic",
    "ragga jungle": "Electronic",
    "big beat": "Electronic",
    "drum 'n bass": "Electronic",
    "modern-funk": "R&B",
    "traditional pop": "Pop",
    "post-hardcore": "Punk",
    "mathcore": "Metal",
    "garage punk": "Punk",
    "grime": "Hip-Hop",
    "deep house": "Electronic",
    "hard house": "Electronic",
    "breaks": "Electronic",
    "experimental rock": "Rock",
    "alternative dance": "Electronic",
    "hardcore techno": "Electronic",
    "hard trance": "Electronic",
    "eurobeat": "Electronic",
    "euro-house": "Electronic",
    "zamrock": "Rock",
    "east coast rap": "Hip-Hop",
    "hardcore rap": "Hip-Hop",
    "gangsta": "Hip-Hop",
    "contemporary jazz": "Jazz",
    "glam": "Rock",
    "psychedelic": "Rock",
    "exotica": "Chill",
    "meditative": "Chill",
    "dream": "Chill",
    "delta": "Blues",
    "country": "Country",
    "spoken": "Spoken",
    "african": "World",
    "latin": "World",
    "world": "World",
}


def resolve_root_genre(genre_tag: str | None) -> str:
    """Resolve a raw genre tag string to a canonical top-level root genre."""
    if not genre_tag:
        return "Other"

    # Split combinations like "Rock / Indie Rock" or "Electronic / Dance"
    cleaned = genre_tag.split("/")[0].strip()
    norm = normalise_genre(cleaned)
    k = norm.lower()

    if norm in ROOT_GENRES:
        # Normalize African and Latin to World for playlist grouping
        return "World" if norm in ("African", "Latin") else norm

    if k in CHILD_TO_PARENT:
        parent = CHILD_TO_PARENT[k]
        return "World" if parent in ("African", "Latin") else parent

    if k in _GENRE_ALIASES:
        return _GENRE_ALIASES[k]

    # Substring match against primary root genre keywords
    for r in ("Electronic", "Rock", "Hip-Hop", "Metal", "Pop", "Punk", "Chill", "Jazz", "Reggae", "Blues", "Folk"):
        if r.lower() in k:
            return r

    return "Other"


def collect_handpicked_tracks(music_root: Path) -> list[Path]:
    """Collect all unique handpicked tracks across 0singles, Faves playlist, and 0random."""
    candidates: set[Path] = set()

    # 1. All files in any 0singles/ folder
    for s_dir in music_root.rglob("0singles"):
        if s_dir.is_dir():
            for f in s_dir.iterdir():
                if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS:
                    candidates.add(f)

    # 2. All files in Faves.m3u8
    faves_playlist = music_root / "Playlists" / "Faves.m3u8"
    if faves_playlist.exists():
        try:
            content = faves_playlist.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    target = (faves_playlist.parent / line).resolve()
                    if target.is_file() and target.suffix.lower() in config.AUDIO_EXTENSIONS:
                        candidates.add(target)
        except Exception as exc:
            log.warning("Could not parse Faves.m3u8: %s", exc)

    # 3. All files in 0random/
    random_dir = music_root / "0random"
    if random_dir.exists():
        for f in random_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS:
                candidates.add(f)

    return sorted(candidates)


def generate_playlists(
    music_root: Path,
    playlists_dir: Path,
    *,
    dry_run: bool = True,
    fix: bool = False,
    min_tracks: int = 5,
) -> dict[str, int]:
    """Group handpicked tracks by root genre and generate M3U8 playlists."""
    tracks = collect_handpicked_tracks(music_root)
    log.info("Collected %d unique hand-picked / singles tracks.", len(tracks))

    genre_map: dict[str, list[Path]] = defaultdict(list)
    for f in tracks:
        try:
            tags = read_tags(f) or {}
        except Exception:
            tags = {}
        g = tags.get("genre")
        root = resolve_root_genre(g)
        genre_map[root].append(f)

    results: dict[str, int] = {}
    playlists_to_write: dict[str, list[Path]] = {}

    # 1. Per-genre playlists
    for root, fs in sorted(genre_map.items()):
        if len(fs) < min_tracks and root != "Other":
            # Fold tiny groups into Other
            genre_map["Other"].extend(fs)
            continue
        pl_name = f"Singles - {root}.m3u8"
        playlists_to_write[pl_name] = sorted(fs, key=lambda p: p.name.lower())
        results[pl_name] = len(fs)

    # If Other has tracks
    if genre_map.get("Other"):
        pl_name = "Singles - Other.m3u8"
        playlists_to_write[pl_name] = sorted(genre_map["Other"], key=lambda p: p.name.lower())
        results[pl_name] = len(genre_map["Other"])

    # 2. Master playlist of all handpicked singles
    playlists_to_write["All_Singles.m3u8"] = sorted(tracks, key=lambda p: p.name.lower())
    results["All_Singles.m3u8"] = len(tracks)

    mode_label = "LIVE" if (fix and not dry_run) else "DRY-RUN"
    log.info("\n=== Generated Playlists Summary (%s) ===", mode_label)

    if fix and not dry_run:
        playlists_dir.mkdir(parents=True, exist_ok=True)

    for pl_name, fs in sorted(playlists_to_write.items(), key=lambda x: -len(x[1])):
        pl_path = playlists_dir / pl_name
        log.info("  %-30s: %4d tracks", pl_name, len(fs))

        if fix and not dry_run:
            lines = ["#EXTM3U\n"]
            for f in fs:
                try:
                    rel = f"../{f.relative_to(music_root)}"
                except ValueError:
                    rel = str(f)
                tags = read_tags(f) or {}
                parsed = parse_filename(f)
                artist = (tags.get("artist") or parsed.get("artist") or "Unknown").strip()
                title = (tags.get("title") or parsed.get("title") or f.stem).strip()
                lines.append(f"#EXTINF:-1,{artist} - {title}\n{rel}\n")

            pl_path.write_text("".join(lines), encoding="utf-8")

    return results


def main() -> None:
    global log
    parser = argparse.ArgumentParser(
        description="Generate top-level genre playlists for all hand-picked singles and faves."
    )
    parser.add_argument("--dry-run", action="store_true", default=True, help="Simulate playlist generation (default)")
    parser.add_argument("--no-dry-run", action="store_true", help="Execute playlist writing")
    parser.add_argument("--fix", action="store_true", help="Write playlists to disk")
    parser.add_argument("--min-tracks", type=int, default=5, help="Minimum tracks for a standalone genre playlist")
    args = parser.parse_args()

    log = setup_logger("generate_genre_playlists", Path("review.log"))

    music_root = Path(getattr(config, "MUSIC_ROOT", Path("/media/jack/buffer/audio")))
    playlists_dir = music_root / "Playlists"

    dry_run = not args.no_dry_run
    generate_playlists(
        music_root,
        playlists_dir,
        dry_run=dry_run,
        fix=args.fix,
        min_tracks=args.min_tracks,
    )


if __name__ == "__main__":
    main()

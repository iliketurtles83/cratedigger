"""Album cover.jpg and single embedded APIC artwork management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import FLAC

from cratedigger.core import embed_artwork, has_embedded_artwork
from lib.mb import fetch_caa_cover, fetch_caa_release_group_cover
from scripts.enrich_covers import process_image

log = logging.getLogger("cratedigger.engine.artwork")

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
CANONICAL_COVER_NAMES = ("cover.jpg", "folder.jpg", "front.jpg", "cover.png", "folder.png")


def find_folder_cover(folder: Path) -> tuple[Path | None, list[Path]]:
    """Inspect *folder* for cover images.

    Returns ``(canonical_cover, all_candidates)``.
    """
    if not folder.is_dir():
        return None, []
    images = [
        p
        for p in sorted(folder.iterdir())
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]
    if not images:
        return None, []

    by_lower = {p.name.lower(): p for p in images}
    for cname in CANONICAL_COVER_NAMES:
        if cname in by_lower:
            return by_lower[cname], images
    if len(images) == 1:
        return images[0], images
    return None, images


def extract_embedded_cover_bytes(path: Path) -> bytes | None:
    """Return raw embedded front/primary cover image bytes from *path* if present."""
    try:
        audio = MutagenFile(path, easy=False)
    except Exception:
        return None
    if audio is None or not getattr(audio, "tags", None):
        return None

    suffix = path.suffix.lower()
    tags = audio.tags
    if suffix == ".mp3":
        apics = [f for k, f in tags.items() if k.startswith("APIC")]
        front = [f for f in apics if getattr(f, "type", None) == 3]
        chosen = front[0] if front else (apics[0] if apics else None)
        return getattr(chosen, "data", None) if chosen else None

    if suffix == ".flac" and isinstance(audio, FLAC) and audio.pictures:
        front = [p for p in audio.pictures if p.type == 3]
        chosen = front[0] if front else audio.pictures[0]
        return chosen.data

    if suffix in {".m4a", ".aac"} and "covr" in tags and tags["covr"]:
        return bytes(tags["covr"][0])

    return None


def resolve_album_cover_bytes(
    folder: Path,
    *,
    mb_albumid: str | None = None,
    mb_releasegroupid: str | None = None,
    max_dim: int = 1200,
    online: bool = False,
) -> dict[str, Any]:
    """Locate, extract, or fetch processed JPEG cover bytes for an album folder."""
    canonical, candidates = find_folder_cover(folder)
    if canonical is not None:
        raw = canonical.read_bytes()
        jpeg_bytes, _ = process_image(raw, max_size=max_dim)
        return {"status": "local", "source_path": canonical, "bytes": jpeg_bytes, "candidates": candidates}

    if len(candidates) > 1:
        return {"status": "ambiguous", "source_path": None, "bytes": None, "candidates": candidates}

    if online:
        raw: bytes | None = None
        if mb_albumid:
            raw = fetch_caa_cover(mb_albumid)
        if raw is None and mb_releasegroupid:
            raw = fetch_caa_release_group_cover(mb_releasegroupid)
        if raw:
            jpeg_bytes, _ = process_image(raw, max_size=max_dim)
            return {"status": "caa", "source_path": None, "bytes": jpeg_bytes, "candidates": []}

    return {"status": "missing", "source_path": None, "bytes": None, "candidates": []}


def ensure_single_embedded_cover(
    track_path: Path,
    *,
    folder_cover: Path | None = None,
    mb_albumid: str | None = None,
    max_dim: int = 1200,
    online: bool = False,
    dry_run: bool = False,
) -> bool:
    """Embed artwork into *track_path* when missing, using folder cover or CAA."""
    if has_embedded_artwork(track_path):
        return False

    raw: bytes | None = None
    if folder_cover and folder_cover.is_file():
        raw = folder_cover.read_bytes()
    else:
        canonical, _ = find_folder_cover(track_path.parent)
        if canonical and canonical.is_file():
            raw = canonical.read_bytes()
        elif online and mb_albumid:
            raw = fetch_caa_cover(mb_albumid)

    if not raw:
        return False
    jpeg_bytes, mime = process_image(raw, max_size=max_dim)
    if dry_run:
        return True
    return embed_artwork(track_path, jpeg_bytes, mime_type=mime, dry_run=False)

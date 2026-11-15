"""Audio transcoding (.wma/.mpc/.ogg/.opus -> .mp3) with full tag & rating preservation."""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import subprocess
import tempfile

from cratedigger.config_schema import Settings
from cratedigger.core import embed_artwork, read_tags, write_tags
from cratedigger.engine.artwork import extract_embedded_cover_bytes
from scripts.enrich_covers import process_image

log = logging.getLogger("cratedigger.engine.transcoder")

DEFAULT_TRANSCODE_EXTENSIONS = frozenset({".wma", ".mpc", ".ogg", ".opus"})


def needs_transcode(path: Path, settings: Settings | None = None) -> bool:
    """Return True if *path* has an extension scheduled for MP3 transcoding."""
    exts = (
        {e.lower() for e in settings.transcode_extensions}
        if settings is not None
        else DEFAULT_TRANSCODE_EXTENSIONS
    )
    return path.suffix.lower() in exts


def transcode_to_mp3(
    src: Path,
    dst: Path,
    *,
    bitrate: str = "320k",
    cover_bytes: bytes | None = None,
    max_cover_dim: int = 1200,
    dry_run: bool = False,
) -> Path:
    """Transcode *src* to MP3 at *dst*, copying all tags, ratings, MBIDs, and cover art."""
    if dry_run:
        log.info("[DRY-RUN] Would transcode %s -> %s (%s)", src, dst, bitrate)
        return dst

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg executable not found on PATH")

    src_tags = read_tags(src) or {}
    art = cover_bytes or extract_embedded_cover_bytes(src)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=str(dst.parent),
        prefix=f".{dst.stem}.",
        suffix=".mp3",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    quality_args = ["-q:a", "0"] if bitrate.upper() == "V0" else ["-b:a", bitrate]
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-vn",
        "-map_metadata",
        "-1",
        "-c:a",
        "libmp3lame",
        *quality_args,
        str(tmp_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()}")

        # Write canonical ID3v2.4 tags, ratings, playcount, and MBIDs through lib/tags.py.
        cleaned_tags = {k: v for k, v in src_tags.items() if v not in (None, "")}
        if cleaned_tags:
            write_tags(tmp_path, cleaned_tags, dry_run=False)

        if art:
            jpeg_bytes, mime = process_image(art, max_size=max_cover_dim)
            embed_artwork(tmp_path, jpeg_bytes, mime_type=mime, dry_run=False)

        tmp_path.replace(dst)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    log.info("Transcoded %s -> %s (%s)", src, dst, bitrate)
    return dst

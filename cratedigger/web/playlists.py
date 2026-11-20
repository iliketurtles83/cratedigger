"""Read-only, bounded inspection of library M3U8 playlists."""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlencode

import config
from cratedigger.web.services import resolve_safe_path

PAGE_SIZE = 50
MAX_PLAYLIST_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 200_000
MAX_LINE_CHARS = 8192
STATUSES = ("available", "missing", "blocked", "unsupported", "inaccessible")
FILTERS = ("all", *STATUSES, "duplicates")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _playlist_root(music_root: Path) -> Path:
    try:
        root = music_root.resolve()
        return resolve_safe_path(root / "Playlists", root)
    except ValueError as exc:
        raise ValueError("The Playlists directory is outside the configured library") from exc
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot resolve the Playlists directory: {exc}") from exc


def _directory_exists(root: Path) -> bool:
    try:
        info = root.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError(f"Cannot inspect the Playlists directory: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("The Playlists location is not a directory")
    return True


def _walk_error(error: OSError) -> None:
    raise error


def list_playlists(music_root: Path, *, query: str = "", page: int = 1) -> dict[str, Any]:
    """List regular playlist files without following symbolic links."""
    if len(query) > 200 or page < 1:
        raise ValueError("Invalid playlist search or page")
    root = _playlist_root(music_root)
    if not _directory_exists(root):
        return {"exists": False, "entries": [], "total": 0, "page": 1, "pages": 1, "skipped_links": 0}
    entries: list[dict[str, Any]] = []
    skipped_links = 0
    try:
        for folder, directories, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
            parent = Path(folder)
            skipped_links += sum((parent / name).is_symlink() for name in directories)
            directories[:] = [name for name in directories if not (parent / name).is_symlink()]
            for name in filenames:
                path = parent / name
                if path.suffix.casefold() != ".m3u8":
                    continue
                if path.is_symlink():
                    skipped_links += 1
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                relative = path.relative_to(root).as_posix()
                if query.casefold() not in relative.casefold():
                    continue
                entries.append({
                    "path": relative,
                    "size": info.st_size,
                    "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                })
    except OSError as exc:
        raise ValueError(f"Cannot list playlists: {exc}") from exc
    entries.sort(key=lambda item: (item["path"].casefold(), item["path"]))
    total = len(entries)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    return {
        "exists": True,
        "entries": entries[(page - 1) * PAGE_SIZE:page * PAGE_SIZE],
        "total": total,
        "page": page,
        "pages": pages,
        "skipped_links": skipped_links,
    }


def _playlist_path(music_root: Path, relative: str) -> Path:
    root = _playlist_root(music_root)
    if not _directory_exists(root):
        raise ValueError("The Playlists directory does not exist")
    candidate = Path(relative)
    if (
        not relative or "\x00" in relative or "\\" in relative or candidate.is_absolute()
        or ".." in candidate.parts or candidate.suffix.casefold() != ".m3u8"
    ):
        raise ValueError("Playlist path must be a relative .m3u8 file in Playlists")
    path = root
    try:
        for component in candidate.parts:
            path = path / component
            if path.is_symlink():
                raise ValueError("Symbolic-linked playlists and folders are not supported")
        path = resolve_safe_path(path, root)
        info = path.stat()
    except FileNotFoundError as exc:
        raise ValueError("Playlist file does not exist") from exc
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot inspect playlist: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Playlist is not a regular file")
    return path


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_playlist(path: Path) -> str:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            with os.fdopen(fd, "rb", closefd=False) as stream:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Playlist is not a regular file")
                data = stream.read(MAX_PLAYLIST_BYTES + 1)
                after = os.fstat(fd)
        finally:
            os.close(fd)
        current = path.stat()
    except OSError as exc:
        raise ValueError(f"Cannot read playlist: {exc}") from exc
    if len(data) > MAX_PLAYLIST_BYTES:
        raise ValueError(f"Playlist exceeds the {MAX_PLAYLIST_BYTES // (1024 * 1024)} MiB inspection limit")
    if (
        _file_identity(before) != _file_identity(after)
        or _file_identity(after) != _file_identity(current)
        or len(data) != after.st_size
    ):
        raise ValueError("Playlist changed while reading; try again")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Playlist is not valid UTF-8") from exc


def _entry(raw: str, playlist_path: Path, music_root: Path) -> dict[str, str]:
    if "\x00" in raw:
        return {"status": "inaccessible", "reason": "Path contains a null character", "key": raw}
    if _SCHEME.match(raw) or raw.startswith("\\\\") or raw.startswith("//"):
        return {"status": "blocked", "reason": "External or Windows-absolute path is not inspected", "key": raw}
    normalized = raw.replace("\\", "/")
    path = Path(normalized)
    candidate = path if path.is_absolute() else playlist_path.parent / path
    try:
        resolved = resolve_safe_path(candidate, music_root)
    except ValueError:
        return {"status": "blocked", "reason": "Path is outside the configured library", "key": raw}
    except (OSError, RuntimeError) as exc:
        return {"status": "inaccessible", "reason": f"Cannot resolve path: {exc}", "key": raw}
    key = str(resolved)
    try:
        info = resolved.stat()
    except (FileNotFoundError, NotADirectoryError):
        return {"status": "missing", "reason": "File not found", "key": key}
    except OSError as exc:
        return {"status": "inaccessible", "reason": f"Cannot inspect file: {exc}", "key": key}
    if not stat.S_ISREG(info.st_mode):
        return {"status": "unsupported", "reason": "Not a regular file", "key": key}
    if resolved.suffix.lower() not in config.AUDIO_EXTENSIONS:
        return {"status": "unsupported", "reason": "Unsupported audio format", "key": key}
    return {
        "status": "available",
        "reason": "",
        "key": key,
        "audio_url": "/media/audio?" + urlencode({"path": key}),
    }


def inspect_playlist(
    music_root: Path, relative: str, *, kind: str = "all", query: str = "", page: int = 1
) -> dict[str, Any]:
    """Inspect one playlist's ordered entries without changing it or its tracks."""
    if kind not in FILTERS or len(query) > 200 or page < 1:
        raise ValueError("Invalid playlist filters")
    path = _playlist_path(music_root, relative)
    text = _read_playlist(path)
    counts = {status: 0 for status in STATUSES}
    seen: set[str] = set()
    matching = []
    total = duplicates = orphaned_labels = 0
    label: str | None = None
    needle = query.casefold()
    for line_number, raw_line in enumerate(StringIO(text, newline=None), 1):
        if len(raw_line) > MAX_LINE_CHARS:
            raise ValueError(f"Playlist line {line_number} exceeds the {MAX_LINE_CHARS}-character inspection limit")
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.upper().startswith("#EXTINF:"):
                orphaned_labels += label is not None
                _, separator, value = line.partition(",")
                label = value.strip() if separator else ""
            continue
        total += 1
        if total > MAX_ENTRIES:
            raise ValueError(f"Playlist exceeds the {MAX_ENTRIES:,}-entry inspection limit")
        info = _entry(line, path, music_root)
        duplicate = info["key"] in seen
        seen.add(info["key"])
        counts[info["status"]] += 1
        duplicates += duplicate
        title = label or Path(line.replace("\\", "/")).stem or line
        label = None
        if kind != "all" and not (kind == "duplicates" and duplicate) and kind != info["status"]:
            continue
        if needle not in f"{title}\n{line}".casefold():
            continue
        matching.append({
            "number": total,
            "title": title,
            "path": line,
            "status": info["status"],
            "reason": info["reason"],
            "audio_url": info.get("audio_url", ""),
            "duplicate": duplicate,
        })
    orphaned_labels += label is not None
    matched = len(matching)
    pages = max(1, (matched + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    return {
        "path": relative,
        "total": total,
        "counts": counts,
        "duplicates": duplicates,
        "orphaned_labels": orphaned_labels,
        "matched": matched,
        "entries": matching[(page - 1) * PAGE_SIZE:page * PAGE_SIZE],
        "page": page,
        "pages": pages,
    }

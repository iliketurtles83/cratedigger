"""Reviewed, seed-based playlist plans and create-only M3U8 publication."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, TYPE_CHECKING
import uuid

import config
from cratedigger.analytics import source_file_state
from cratedigger.engine.playlists import format_relative_entry
from lib.parsers import sanitise_component

if TYPE_CHECKING:
    from cratedigger.web.similarity import SimilarityIndex

DEFAULT_TRACKS = 42
MAX_TRACKS = 200
MAX_PLAN_BYTES = 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def playlist_filename(name: str) -> str:
    filename = name if name.casefold().endswith(".m3u8") else name + ".m3u8"
    if (
        not name or name != name.strip() or len(filename) > 120 or len(os.fsencode(filename)) > 240
        or filename != sanitise_component(filename) or _CONTROL.search(filename)
    ):
        raise ValueError("Choose a Windows-safe playlist name of at most 120 characters, without folders")
    return filename


def _audio_state(raw: str, root: Path) -> tuple[Path, tuple[int, int, int, int]]:
    if not raw or _CONTROL.search(raw) or "\\" in raw:
        raise ValueError("Audio path is empty or contains unsupported playlist characters")
    path = Path(raw).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("Audio path is outside the configured library")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
        raise ValueError("Audio path is not a supported regular file")
    return path, source_file_state(path)


def _check_destination(root: Path, filename: str) -> None:
    directory = root / "Playlists"
    if directory.is_symlink():
        raise ValueError("Symbolic-linked Playlists directories cannot be written")
    if directory.exists():
        if not directory.is_dir():
            raise ValueError("The Playlists location is not a directory")
        if any(path.name.casefold() == filename.casefold() for path in directory.iterdir()):
            raise ValueError("A playlist with this name already exists (case-insensitive); choose a new name")


def preview_playlist(
    index: SimilarityIndex, music_root: Path, *, seed: str, name: str, count: int = DEFAULT_TRACKS
) -> dict[str, Any]:
    """Freeze the seed and its nearest neighbors, explicitly recording unavailable paths."""
    if type(count) is not int or not 1 <= count <= MAX_TRACKS:
        raise ValueError(f"Track count must be between 1 and {MAX_TRACKS}")
    if not index.uses_current_recipe:
        raise ValueError("Rebuild the analytics snapshot to use the current similarity recipe")
    selected = index.by_hash.get(seed)
    if selected is None or selected.vector_index is None:
        raise ValueError("Choose a seed with a usable similarity vector")
    root = music_root.resolve(strict=True)
    filename = playlist_filename(name)
    _check_destination(root, filename)
    candidates = [(selected, 1.0)]
    if count > 1:
        candidates.extend(index.neighbors(selected, limit=count - 1))
    tracks = []
    skipped = []
    seen = set()
    for track, score in candidates:
        try:
            path, state = _audio_state(track.path or "", root)
            if str(path) in seen:
                raise ValueError("Another selected hash uses this same audio path")
        except (OSError, RuntimeError, ValueError) as exc:
            if track.audio_hash == seed:
                raise ValueError(f"The seed audio is unavailable: {exc}") from exc
            skipped.append({"audio_hash": track.audio_hash, "title": track.display_title, "reason": str(exc)})
            continue
        seen.add(str(path))
        label = " - ".join(value for value in (track.artist, track.display_title) if value)
        tracks.append({
            "audio_hash": track.audio_hash, "path": str(path),
            "label": _CONTROL.sub(" ", label), "state": list(state), "score": score,
        })
    return {
        "version": 1, "music_root": str(root), "filename": filename, "seed": seed,
        "requested": count, "snapshot_generated_at": index.metadata["generated_at"],
        "tracks": tracks, "skipped": skipped,
    }


def write_plan(plan: dict[str, Any], reports_root: Path) -> Path:
    directory = reports_root / "playlists"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"seed-{uuid.uuid4().hex}.json"
    data = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(data.encode("utf-8")) > MAX_PLAN_BYTES:
        raise ValueError("Playlist preview exceeds the report size limit")
    with path.open("x", encoding="utf-8") as stream:
        stream.write(data)
    return path


def read_plan(path: Path, music_root: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = stream.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise ValueError("Playlist preview exceeds the report size limit")
    try:
        plan = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid playlist preview JSON: {exc}") from exc
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 1:
        raise ValueError("Unsupported playlist preview version")
    if plan.get("music_root") != str(music_root.resolve(strict=True)):
        raise ValueError("Playlist preview was prepared for a different library root")
    filename = plan.get("filename")
    if not isinstance(filename, str) or playlist_filename(filename) != filename:
        raise ValueError("Invalid playlist preview filename")
    count = plan.get("requested")
    tracks = plan.get("tracks")
    if (
        type(count) is not int or not 1 <= count <= MAX_TRACKS
        or not isinstance(tracks, list) or not 1 <= len(tracks) <= count
    ):
        raise ValueError("Invalid playlist preview track count")
    hashes = set()
    paths = set()
    for track in tracks:
        if not isinstance(track, dict) or any(
            not isinstance(track.get(key), str) or not track[key] for key in ("audio_hash", "path", "label")
        ):
            raise ValueError("Invalid playlist preview track")
        state = track.get("state")
        if (
            not isinstance(state, list) or len(state) != 4
            or any(type(value) is not int or value < 0 for value in state)
            or _CONTROL.search(track["label"])
            or track["audio_hash"] in hashes or track["path"] in paths
        ):
            raise ValueError("Invalid or duplicate playlist preview track evidence")
        hashes.add(track["audio_hash"])
        paths.add(track["path"])
    if plan.get("seed") != tracks[0]["audio_hash"]:
        raise ValueError("The first playlist track must be the reviewed seed")
    skipped = plan.get("skipped")
    if not isinstance(skipped, list) or len(tracks) + len(skipped) > count or any(
        not isinstance(item, dict) or any(
            not isinstance(item.get(key), str) for key in ("audio_hash", "title", "reason")
        ) for item in skipped
    ):
        raise ValueError("Invalid skipped-track evidence")
    if not isinstance(plan.get("snapshot_generated_at"), str):
        raise ValueError("Invalid playlist snapshot timestamp")
    for track in tracks:
        score = track.get("score")
        if type(score) not in (int, float) or not math.isfinite(score) or not -1 <= score <= 1:
            raise ValueError("Invalid playlist similarity score")
    return plan


def _verify_tracks(plan: dict[str, Any], root: Path) -> None:
    for track in plan["tracks"]:
        try:
            path, state = _audio_state(track["path"], root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"Reviewed audio is unavailable: {track['label']}: {exc}") from exc
        if str(path) != track["path"] or list(state) != track["state"]:
            raise ValueError(f"Audio changed since preview: {track['label']}; build a new preview")


def create_playlist(report: Path, music_root: Path, *, dry_run: bool = True) -> dict[str, Any]:
    """Validate reviewed file identities, then publish a new file without replacing anything."""
    root = music_root.resolve(strict=True)
    plan = read_plan(report, root)
    filename = plan["filename"]
    _check_destination(root, filename)
    _verify_tracks(plan, root)
    target = root / "Playlists" / filename
    lines = ["#EXTM3U"]
    for track in plan["tracks"]:
        lines.extend((f"#EXTINF:-1,{track['label']}", format_relative_entry(Path(track["path"]), target)))
    content = ("\n".join(lines) + "\n").encode("utf-8")
    if not dry_run:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            try:
                os.mkdir("Playlists", dir_fd=root_fd)
            except FileExistsError:
                pass
            directory_fd = os.open(
                "Playlists", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd,
            )
        finally:
            os.close(root_fd)
        temporary = f".playlist-{uuid.uuid4().hex}.tmp"
        try:
            if any(name.casefold() == filename.casefold() for name in os.listdir(directory_fd)):
                raise ValueError("A playlist with this name already exists; choose a new name")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=directory_fd)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                _verify_tracks(plan, root)
                os.link(
                    temporary, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            finally:
                os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    return {"dry_run": dry_run, "playlist": str(target), "tracks": len(plan["tracks"])}

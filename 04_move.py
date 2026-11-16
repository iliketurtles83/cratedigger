#!/usr/bin/env python3
"""04_move.py — Route intake and maintain the canonical Tier 1 layout.

Modes are mutually exclusive:

- ``--intake``: route files from INCOMING_FOLDER into staging folders
- ``--restructure``: compatibility alias for legacy genre-root migration
- ``--promote``: move staged albums from STAGED_ALBUMS_FOLDER to genre folders
- ``--to-artist-root``: migrate legacy genre roots to Tier 1
- ``--relocate-compilations``: move Various Artists releases to 0compilations
- ``--validate-config``: check top-level folder guardrails

Usage:
    python 04_move.py MODE
                      [--dry-run] [--folder NAME] [--log FILE]
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import config
from lib.context import (
    artist_letter_bucket,
    classify_folder,
    infer_compilation_folder,
    is_letter_bucket,
)
from lib.genres import normalise_genre, parent_genre
from lib.logger import setup_logger
from lib.parsers import artist_comparison_key, sanitise_component, sanitise_name
from lib.path_updates import record_folder_move, update_downstream_paths
from lib.preferences import remap_genre
from lib.tags import read_tags, write_tags

log: logging.Logger = None  # type: ignore[assignment]
MOVED_SINGLES: list[Path] = []

# Build reverse map: display genre → folder name
_GENRE_TO_FOLDER: dict[str, str] = {v.lower(): k for k, v in config.FOLDER_TO_GENRE.items()}


def resolve_target_folder(genre_tag: str) -> Path | None:
    """Return the target genre folder Path for the primary genre, or None."""
    candidates = [c.strip() for c in re.split(r"[/;,]", genre_tag) if c.strip()]
    for primary in candidates:
        normed = remap_genre(normalise_genre(primary)).lower()

        # Direct match
        folder_name = _GENRE_TO_FOLDER.get(normed)

        # Try parent genre
        if folder_name is None:
            par = parent_genre(primary)
            if par:
                folder_name = _GENRE_TO_FOLDER.get(par.lower())

        if folder_name is not None:
            return config.MUSIC_ROOT / folder_name

    return None


def _artist_letter_bucket(artist: str) -> str:
    """Return the Tier 1 letter bucket for an artist name.

    Thin wrapper over :func:`lib.context.artist_letter_bucket`, which is the
    single source of truth for path-derived bucketing.
    """
    return artist_letter_bucket(artist)


def _first_audio_file(folder: Path) -> Path | None:
    exts = config.AUDIO_EXTENSIONS | {".wma"}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in exts:
            return path
    return None


def _review_item_key(item: dict) -> str:
    """Return stable identity key for review item dedupe."""
    payload = {
        key: item.get(key)
        for key in sorted(item)
        if key not in {"path", "reason"}
    }
    return json.dumps(
        {
            "path": item.get("path"),
            "reason": item.get("reason"),
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _write_review_items(all_review: list[dict], *, dry_run: bool) -> None:
    if not all_review:
        return

    review_path = Path("review.json")
    existing: list[dict] = []
    if review_path.exists():
        try:
            existing = json.loads(review_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            pass

    if not dry_run:
        seen = {
            _review_item_key(e)
            for e in existing
            if isinstance(e, dict)
        }
        for item in all_review:
            key = _review_item_key(item)
            if key not in seen:
                existing.append(item)
                seen.add(key)
        review_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    log.info("Flagged %d items for review", len(all_review))


def _map_folder_name_from_genre(genre_tag: str) -> str | None:
    target_dir = resolve_target_folder(genre_tag)
    if target_dir is None:
        return None
    return target_dir.name


def _project_root() -> Path:
    return Path(__file__).resolve().parent


def _allowed_hook_roots() -> list[Path]:
    configured = getattr(config, "HOOK_ALLOWED_PATHS", None)
    roots: list[Path] = []
    if isinstance(configured, (list, tuple)) and configured:
        for raw in configured:
            roots.append(Path(raw).expanduser().resolve())
    else:
        default_root = Path(getattr(config, "HOOKS_ROOT", _project_root() / "hooks"))
        roots.append(default_root.expanduser().resolve())
    return roots


def _hook_policy() -> str:
    policy = str(getattr(config, "HOOK_FAILURE_POLICY", "continue")).strip().lower()
    if policy in {"continue", "review", "abort"}:
        return policy
    return "continue"


def _resolve_hook_command(command: object) -> list[str] | None:
    if not isinstance(command, list) or not command:
        return None
    parts = [str(p) for p in command]
    executable = Path(parts[0])
    if not executable.is_absolute():
        executable = (_project_root() / executable).resolve()
    else:
        executable = executable.resolve()

    for root in _allowed_hook_roots():
        try:
            executable.relative_to(root)
            break
        except ValueError:
            continue
    else:
        log.warning("Hook executable outside allowed roots: %s", executable)
        return None

    if not executable.exists() or not executable.is_file():
        log.warning("Hook executable missing: %s", executable)
        return None

    return [str(executable), *parts[1:]]


def _handle_hook_failure(
    *,
    event: str,
    hook_name: str,
    error_message: str,
    payload: dict[str, object],
    review_items: list[dict] | None,
) -> None:
    policy = _hook_policy()
    if policy == "abort":
        raise RuntimeError(error_message)

    log.warning(error_message)
    if policy == "review" and review_items is not None:
        review_items.append(
            {
                "path": str(payload.get("src", "")),
                "reason": "hook_failure",
                "hook": hook_name,
                "event": event,
                "error": error_message,
                "target": str(payload.get("dest", "")),
            }
        )


def _run_move_hooks(
    event: str,
    *,
    payload: dict[str, object],
    dry_run: bool,
    review_items: list[dict] | None,
) -> None:
    run_in_dry_run = bool(getattr(config, "HOOKS_RUN_IN_DRY_RUN", False))
    if dry_run and not run_in_dry_run:
        return

    hook_map = getattr(config, "MOVE_HOOKS", {})
    if not isinstance(hook_map, dict):
        return

    hook_specs = hook_map.get(event, [])
    if not isinstance(hook_specs, list):
        return

    try:
        default_timeout = float(getattr(config, "HOOK_TIMEOUT_SECONDS", 5.0))
    except (TypeError, ValueError):
        default_timeout = 5.0
    for idx, spec in enumerate(hook_specs, start=1):
        if not isinstance(spec, dict):
            continue
        if spec.get("enabled", True) is False:
            continue

        hook_name = str(spec.get("name") or f"{event}#{idx}")
        command = _resolve_hook_command(spec.get("command"))
        if command is None:
            _handle_hook_failure(
                event=event,
                hook_name=hook_name,
                error_message=f"Hook {hook_name} has invalid command configuration",
                payload=payload,
                review_items=review_items,
            )
            continue

        try:
            timeout = float(spec.get("timeout_seconds", default_timeout))
        except (TypeError, ValueError):
            timeout = default_timeout
        env = {
            "PATH": os.environ.get("PATH", ""),
            "CRATEDIGGER_HOOK_EVENT": event,
            "CRATEDIGGER_HOOK_NAME": hook_name,
        }
        try:
            proc = subprocess.run(
                command,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=timeout,
                cwd=str(config.MUSIC_ROOT),
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _handle_hook_failure(
                event=event,
                hook_name=hook_name,
                error_message=f"Hook {hook_name} failed: {exc}",
                payload=payload,
                review_items=review_items,
            )
            continue

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            details = stderr if stderr else f"exit_code={proc.returncode}"
            _handle_hook_failure(
                event=event,
                hook_name=hook_name,
                error_message=f"Hook {hook_name} failed: {details}",
                payload=payload,
                review_items=review_items,
            )
            continue

        log.info("Hook %s succeeded for %s", hook_name, event)


def _move_path(
    src: Path,
    dest: Path,
    *,
    dry_run: bool,
    review_items: list[dict] | None = None,
) -> None:
    payload = {
        "src": str(src),
        "dest": str(dest),
        "dry_run": dry_run,
    }
    _run_move_hooks(
        "pre_move",
        payload=payload,
        dry_run=dry_run,
        review_items=review_items,
    )

    if "0singles" in dest.parts and dest.suffix.lower() in config.AUDIO_EXTENSIONS:
        MOVED_SINGLES.append(dest)

    if dry_run:
        log.info("[DRY-RUN] Would move: %s → %s", src, dest)
        _run_move_hooks(
            "post_move",
            payload=payload,
            dry_run=True,
            review_items=review_items,
        )
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    log.info("Moved: %s → %s", src, dest)
    payload["dry_run"] = False
    _run_move_hooks(
        "post_move",
        payload=payload,
        dry_run=False,
        review_items=review_items,
    )


def _remove_if_empty(folder: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
            log.info("Removed empty directory: %s", folder)
    except OSError:
        pass


def _route_loose_file_from_intake(
    path: Path,
    *,
    dry_run: bool,
    review_items: list[dict],
) -> None:
    tags = read_tags(path)
    if tags is None:
        log.warning("Skipping unreadable file: %s", path)
        review_items.append({"path": str(path), "reason": "unreadable"})
        return

    genre_tag = tags.get("genre")
    if not genre_tag:
        log.warning("No genre tag for %s — flagging for review", path.name)
        review_items.append({"path": str(path), "reason": "no_genre"})
        return

    folder_name = _map_folder_name_from_genre(genre_tag)
    if folder_name is None:
        log.warning("No folder mapping for genre '%s' (%s) — flagging",
                    genre_tag, path.name)
        review_items.append({
            "path": str(path),
            "reason": "no_folder_mapping",
            "genre": genre_tag,
        })
        return

    dest = config.STAGED_TRACKS_FOLDER / folder_name / path.name
    if dest.exists():
        log.warning("Target exists, skipping: %s → %s", path, dest)
        review_items.append({
            "path": str(path),
            "reason": "move_conflict",
            "target": str(dest),
        })
        return

    _move_path(path, dest, dry_run=dry_run, review_items=review_items)


def _route_album_folder_from_intake(
    album_folder: Path,
    *,
    dry_run: bool,
    review_items: list[dict],
) -> None:
    sample = _first_audio_file(album_folder)
    if sample is None:
        log.warning("No audio files in intake album folder: %s", album_folder)
        review_items.append({
            "path": str(album_folder),
            "reason": "unknown_pattern",
        })
        return

    tags = read_tags(sample)
    if tags is None:
        log.warning("Skipping unreadable album folder: %s", album_folder)
        review_items.append({"path": str(album_folder), "reason": "unreadable"})
        return

    genre_tag = tags.get("genre")
    if not genre_tag:
        log.warning("No genre tag for album folder %s — flagging", album_folder)
        review_items.append({"path": str(album_folder), "reason": "no_genre"})
        return

    folder_name = _map_folder_name_from_genre(genre_tag)
    if folder_name is None:
        log.warning("No folder mapping for genre '%s' (%s) — flagging",
                    genre_tag, album_folder.name)
        review_items.append({
            "path": str(album_folder),
            "reason": "no_folder_mapping",
            "genre": genre_tag,
        })
        return

    dest = config.STAGED_ALBUMS_FOLDER / folder_name / album_folder.name
    if dest.exists():
        log.warning("Target exists, skipping: %s → %s", album_folder, dest)
        review_items.append({
            "path": str(album_folder),
            "reason": "move_conflict",
            "target": str(dest),
        })
        return

    _move_path(album_folder, dest, dry_run=dry_run, review_items=review_items)


def run_intake_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Route incoming loose files/albums into staging folders."""
    review_items: list[dict] = []

    if not config.INCOMING_FOLDER.exists():
        log.warning("INCOMING_FOLDER does not exist: %s", config.INCOMING_FOLDER)
        review_items.append({
            "path": str(config.INCOMING_FOLDER),
            "reason": "missing_folder",
        })
        return review_items

    if folder:
        candidates = [config.INCOMING_FOLDER / folder]
    else:
        candidates = sorted(config.INCOMING_FOLDER.iterdir())

    for item in candidates:
        if not item.exists():
            continue
        if item.is_dir():
            log.info("Intake album folder: %s", item)
            _route_album_folder_from_intake(item, dry_run=dry_run,
                                            review_items=review_items)
        elif item.is_file() and item.suffix.lower() in config.AUDIO_EXTENSIONS:
            log.info("Intake loose file: %s", item)
            _route_loose_file_from_intake(item, dry_run=dry_run,
                                          review_items=review_items)

    return review_items




# ── Legacy genre-root migration ───────────────────────────────────────────────

def run_restructure_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Migrate legacy genre roots into the canonical Tier 1 artist layout.

    The old album-count threshold conflicted with Tier 1 by flattening small
    artists into bucket roots. ``--restructure`` remains as a compatibility
    alias for ``--to-artist-root``.
    """
    if folder and is_letter_bucket(folder):
        log.warning(
            "%s is already a Tier 1 bucket; use 03_folders.py to normalize it",
            folder,
        )
        return [{
            "path": str(config.MUSIC_ROOT / folder),
            "reason": "tier1_use_03_folders",
        }]
    return run_to_artist_root_mode(dry_run=dry_run, folder=folder)


def run_promote_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Move staged albums from STAGED_ALBUMS_FOLDER into genre folders."""
    review_items: list[dict] = []

    if not config.STAGED_ALBUMS_FOLDER.exists():
        log.warning("STAGED_ALBUMS_FOLDER does not exist: %s",
                    config.STAGED_ALBUMS_FOLDER)
        review_items.append({
            "path": str(config.STAGED_ALBUMS_FOLDER),
            "reason": "missing_folder",
        })
        return review_items

    if folder:
        staged_genre_dirs = [config.STAGED_ALBUMS_FOLDER / folder]
    else:
        staged_genre_dirs = sorted(
            p for p in config.STAGED_ALBUMS_FOLDER.iterdir() if p.is_dir()
        )

    for staged_genre_dir in staged_genre_dirs:
        if not staged_genre_dir.exists() or not staged_genre_dir.is_dir():
            continue

        folder_name = staged_genre_dir.name
        if folder_name not in config.FOLDER_TO_GENRE:
            log.warning("Unknown staged genre folder: %s — flagging",
                        staged_genre_dir)
            review_items.append({
                "path": str(staged_genre_dir),
                "reason": "no_folder_mapping",
                "genre": folder_name,
            })
            continue

        target_genre_dir = config.MUSIC_ROOT / folder_name

        for album_folder in sorted(staged_genre_dir.iterdir()):
            if not album_folder.is_dir():
                continue

            dest = target_genre_dir / album_folder.name
            if dest.exists():
                log.warning("Target exists, skipping: %s → %s", album_folder, dest)
                review_items.append({
                    "path": str(album_folder),
                    "reason": "move_conflict",
                    "target": str(dest),
                })
                continue

            _move_path(album_folder, dest, dry_run=dry_run, review_items=review_items)

        _remove_if_empty(staged_genre_dir, dry_run=dry_run)

    return review_items


# ── Artist-root restructure (genre-root → letter-bucket/artist/album) ────────────────

def _migrate_album_to_artist_root(
    folder: Path,
    *,
    dry_run: bool,
    review_items: list[dict],
) -> None:
    """Move one album folder from any genre-root path to artist-root layout.

    Reads artist from tags (albumartist preferred over artist), falls back to
    parsing the folder name before " - ".
    """
    folder_artist: str | None = None
    artist: str | None = None
    if " - " in folder.name:
        cand = folder.name.split(" - ")[0].strip()
        if cand and cand.lower() not in {"various artists", "various", "va"}:
            folder_artist = cand

    sample = _first_audio_file(folder)
    if sample is not None:
        tags = read_tags(sample)
        if tags:
            albumartist = (tags.get("albumartist") or "").strip()
            if albumartist and albumartist.lower() in {"various artists", "various", "va"}:
                dest_comp = config.MUSIC_ROOT / "0compilations" / folder.name
                if dest_comp.exists():
                    log.warning("Target exists in 0compilations, skipping: %s → %s", folder, dest_comp)
                    review_items.append({"path": str(folder), "reason": "move_conflict", "target": str(dest_comp)})
                else:
                    _move_path(folder, dest_comp, dry_run=dry_run, review_items=review_items)
                return
            elif albumartist:
                artist = albumartist
            elif folder_artist:
                artist = folder_artist
            else:
                artist = (tags.get("artist") or "").strip() or None

    if not artist and folder_artist:
        artist = folder_artist

    if not artist:
        log.warning("Cannot determine artist for %s — flagging", folder.name)
        review_items.append({"path": str(folder), "reason": "no_artist_tag"})
        return

    artist_dir, _ = _canonical_artist_folder_name(artist)
    artist_dir = artist_dir or sanitise_component(sanitise_name(artist))
    bucket = _artist_letter_bucket(artist_dir)
    dest = config.MUSIC_ROOT / bucket / artist_dir / sanitise_component(folder.name)

    if dest.exists():
        log.warning("Target exists, skipping: %s → %s", folder, dest)
        review_items.append({
            "path": str(folder),
            "reason": "move_conflict",
            "target": str(dest),
        })
        return

    _move_path(folder, dest, dry_run=dry_run, review_items=review_items)


def _walk_genre_to_artist_root(genre_dir: Path, *, dry_run: bool) -> list[dict]:
    """Walk one genre folder and migrate its contents to artist-root layout."""
    review_items: list[dict] = []
    for child in sorted(genre_dir.iterdir()):
        if child.is_file():
            if child.suffix.lower() in config.AUDIO_EXTENSIONS:
                tags = read_tags(child)
                artist = (tags.get("artist") or "").strip() if tags else None
                if not artist and " - " in child.name:
                    cand = child.name.split(" - ")[0].strip()
                    if not re.match(r"^\d+(?:-\d+)?$", cand):
                        artist = cand or None
                if artist:
                    artist_dir, _ = _canonical_artist_folder_name(artist)
                    artist_dir = artist_dir or sanitise_component(sanitise_name(artist))
                    bucket = _artist_letter_bucket(artist_dir)
                    dest_single = config.MUSIC_ROOT / bucket / artist_dir / "0singles" / sanitise_component(child.name)
                else:
                    dest_single = config.MUSIC_ROOT / "0singles" / sanitise_component(child.name)

                if dest_single.exists():
                    log.warning("Target single exists, skipping: %s → %s", child, dest_single)
                    review_items.append({
                        "path": str(child),
                        "reason": "move_conflict",
                        "target": str(dest_single),
                    })
                else:
                    _move_path(child, dest_single, dry_run=dry_run, review_items=review_items)
            continue

        if not child.is_dir():
            continue

        # Check for compilation releases first (routes to 0compilations/)
        is_comp = False
        prefix = child.name.split(" - ")[0].strip().lower() if " - " in child.name else ""
        if prefix in {"various artists", "various", "va", "v.a."}:
            is_comp = True
        else:
            subdirs = [s for s in child.iterdir() if s.is_dir()]
            if subdirs:
                # Multi-directory release: only route to 0compilations if >= 75% of subdirs are compilations AND not a solo artist folder
                comp_count = sum(1 for s in subdirs if infer_compilation_folder(s))
                if comp_count > 0 and comp_count >= len(subdirs) * 0.75:
                    comp_keywords = ("compilation", "collection", "various", "sampler", "top 10", "top 100", "classics")
                    sample = _first_audio_file(child)
                    sample_tags = read_tags(sample) if sample else {}
                    sample_aa = (sample_tags.get("albumartist") or "").strip().lower() if sample_tags else ""
                    if sample_aa in {"various artists", "various", "va"} or any(kw in child.name.lower() for kw in comp_keywords):
                        is_comp = True
            else:
                if infer_compilation_folder(child):
                    is_comp = True

        if is_comp:
            dest = config.MUSIC_ROOT / "0compilations" / child.name
            if dest.exists():
                log.warning("Target exists in 0compilations, skipping: %s → %s", child, dest)
                review_items.append({
                    "path": str(child),
                    "reason": "move_conflict",
                    "target": str(dest),
                })
            else:
                _move_path(child, dest, dry_run=dry_run, review_items=review_items)
            continue

        # Check for genre-level singles (genre/0singles/)
        if child.name.lower() == "0singles":
            for s in sorted(child.iterdir()):
                if not s.is_file() or s.suffix.lower() not in config.AUDIO_EXTENSIONS:
                    continue
                tags = read_tags(s)
                artist = (tags.get("artist") or "").strip() if tags else None
                if not artist and " - " in s.name:
                    cand = s.name.split(" - ")[0].strip()
                    if not re.match(r"^\d+(?:-\d+)?$", cand):
                        artist = cand or None
                if artist:
                    artist_dir, _ = _canonical_artist_folder_name(artist)
                    artist_dir = artist_dir or sanitise_component(sanitise_name(artist))
                    bucket = _artist_letter_bucket(artist_dir)
                    dest_single = config.MUSIC_ROOT / bucket / artist_dir / "0singles" / sanitise_component(s.name)
                else:
                    dest_single = config.MUSIC_ROOT / "0singles" / sanitise_component(s.name)

                if dest_single.exists():
                    log.warning("Target single exists, skipping: %s → %s", s, dest_single)
                    review_items.append({
                        "path": str(s),
                        "reason": "move_conflict",
                        "target": str(dest_single),
                    })
                else:
                    _move_path(s, dest_single, dry_run=dry_run, review_items=review_items)
            _remove_if_empty(child, dry_run=dry_run)
            continue

        # Check for legacy genre-level compilations (genre/0compilations/ or genre/0various/)
        if child.name.lower() in {"0compilations", "0various"}:
            for sub in sorted(child.iterdir()):
                if not sub.is_dir():
                    continue
                dest_comp = config.MUSIC_ROOT / "0compilations" / sub.name
                if dest_comp.exists():
                    log.warning("Target exists in 0compilations, skipping: %s → %s", sub, dest_comp)
                    review_items.append({
                        "path": str(sub),
                        "reason": "move_conflict",
                        "target": str(dest_comp),
                    })
                else:
                    _move_path(sub, dest_comp, dry_run=dry_run, review_items=review_items)
            _remove_if_empty(child, dry_run=dry_run)
            continue

        kind = classify_folder(child)

        # If unknown, check whether it contains audio files and can be treated as an artist
        if kind == "unknown":
            audio_files = [f for f in child.rglob("*") if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS]
            if not audio_files:
                if not dry_run and not any(child.iterdir()):
                    _remove_if_empty(child, dry_run=False)
                continue
            kind = "artist"

        if kind == "album":
            # genre/Artist - Album (Year)/ → bucket/Artist/Artist - Album (Year)/
            _migrate_album_to_artist_root(
                child, dry_run=dry_run, review_items=review_items)

        elif kind in ("artist", "artist_flat", "artist_mixed"):
            # genre/Artist/ → bucket/Artist/  (move the whole artist folder)
            artist_name = sanitise_component(sanitise_name(child.name))
            bucket = _artist_letter_bucket(artist_name)
            dest = config.MUSIC_ROOT / bucket / artist_name

            if dest.exists():
                # Artist already has a folder under the letter bucket (from
                # another genre).  Merge by moving each child individually.
                log.info("Merging artist folder: %s → %s", child, dest)
                for sub in sorted(child.iterdir()):
                    sub_dest = dest / sub.name
                    if sub_dest.exists():
                        log.warning("Merge conflict, skipping: %s", sub)
                        review_items.append({
                            "path": str(sub),
                            "reason": "move_conflict",
                            "target": str(sub_dest),
                        })
                    else:
                        _move_path(sub, sub_dest, dry_run=dry_run, review_items=review_items)
                _remove_if_empty(child, dry_run=dry_run)
            else:
                _move_path(child, dest, dry_run=dry_run, review_items=review_items)

        elif kind == "disc":
            # Disc subfolder at genre root is unusual; flag for review
            review_items.append({
                "path": str(child),
                "reason": "unexpected_disc_at_genre_root",
            })
        # local_special left in place

    _remove_if_empty(genre_dir, dry_run=dry_run)
    return review_items


def run_to_artist_root_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Restructure genre-root folders to artist-root letter-bucket layout.

    Walks each top-level genre folder (Tier 2 paths) and moves its contents
    to the canonical artist-root shape under MUSIC_ROOT/letter/Artist/.
    Letter buckets, top-level specials, and SKIP_FOLDERS are not touched.
    """
    _letter_skip = {b.lower() for b in getattr(config, "LETTER_BUCKETS", set())}
    _symbol = getattr(config, "SYMBOL_BUCKET", "#")
    _special = {s.lower() for s in getattr(config, "TOP_LEVEL_SPECIAL", config.SPECIAL_FOLDERS)}
    skip = _special | _letter_skip | {_symbol} | {s.lower() for s in config.SKIP_FOLDERS}

    if folder:
        genre_dirs = [config.MUSIC_ROOT / folder]
    else:
        genre_dirs = sorted(
            p for p in config.MUSIC_ROOT.iterdir()
            if p.is_dir() and p.name.lower() not in skip
        )

    all_review: list[dict] = []
    for genre_dir in genre_dirs:
        log.info("To-artist-root: %s", genre_dir.name)
        items = _walk_genre_to_artist_root(genre_dir, dry_run=dry_run)
        all_review.extend(items)

    return all_review


_COMPILATION_ALBUMARTISTS = {
    "various artists",
    "various",
    "va",
    "v.a.",
}


def _explicit_compilation_name(folder: Path) -> str | None:
    """Return a canonical folder name for an explicitly tagged compilation."""
    albumartists: set[str] = set()
    albums: set[str] = set()
    years: set[str] = set()

    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(path)
        if not tags:
            continue
        albumartist = (tags.get("albumartist") or "").strip().casefold()
        if albumartist:
            albumartists.add(albumartist)
        album = (tags.get("album") or "").strip()
        if album:
            albums.add(album)
        year = (tags.get("year") or "").strip()[:4]
        if re.fullmatch(r"\d{4}", year):
            years.add(year)

    if not albumartists or not albumartists.issubset(_COMPILATION_ALBUMARTISTS):
        return None

    if len(albums) == 1:
        name = sanitise_name(next(iter(albums)))
        if len(years) == 1:
            name = f"{name} ({next(iter(years))})"
        return sanitise_component(name)

    prefix = folder.name.split(" - ", 1)
    raw = prefix[1] if len(prefix) == 2 and prefix[0].casefold() in _COMPILATION_ALBUMARTISTS else folder.name
    return sanitise_component(raw)


def _primary_track_artist(artist: str) -> str:
    """Return the lead artist from a credited track-artist string."""
    return re.split(
        r"\s+(?:feat\.?|ft\.?|featuring)\s+",
        artist,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()


def _canonical_artist_folder_name(artist: str) -> tuple[str | None, str | None]:
    """Return an existing Tier 1 spelling, or a sanitized new folder name."""
    bucket = config.MUSIC_ROOT / _artist_letter_bucket(artist)
    matches = [
        path.name
        for path in bucket.iterdir()
        if path.is_dir()
        and sanitise_component(path.name) == path.name
        and artist_comparison_key(path.name) == artist_comparison_key(artist)
    ] if bucket.is_dir() else []
    if len(matches) > 1:
        return None, "ambiguous_artist_folder"
    return (matches[0] if matches else sanitise_component(sanitise_name(artist))), None


def _relocate_various_artist_singles(
    artist_dir: Path,
    *,
    dry_run: bool,
    moved_paths: dict[Path, Path],
    review_items: list[dict],
) -> None:
    """Convert loose Various Artists tracks into per-artist singles."""
    singles_dir = artist_dir / config.ARTIST_SINGLES_FOLDER
    if not singles_dir.is_dir():
        return

    for source in sorted(singles_dir.iterdir()):
        if not source.is_file() or source.suffix.lower() not in config.AUDIO_EXTENSIONS:
            continue
        tags = read_tags(source)
        credited_artist = (tags.get("artist") or "").strip() if tags else ""
        primary_artist = _primary_track_artist(credited_artist)
        if not primary_artist:
            review_items.append({
                "path": str(source),
                "reason": "no_artist_tag",
            })
            continue

        artist_folder, error = _canonical_artist_folder_name(primary_artist)
        if error or not artist_folder:
            review_items.append({
                "path": str(source),
                "reason": error or "no_artist_folder",
            })
            continue

        destination = (
            config.MUSIC_ROOT
            / _artist_letter_bucket(artist_folder)
            / artist_folder
            / config.ARTIST_SINGLES_FOLDER
            / source.name
        )
        if destination == source:
            continue
        if destination.exists():
            log.warning(
                "Artist single target exists, skipping: %s → %s",
                source,
                destination,
            )
            review_items.append({
                "path": str(source),
                "reason": "move_conflict",
                "target": str(destination),
            })
            continue

        if not dry_run:
            write_tags(
                source,
                {
                    "albumartist": artist_folder,
                    "album": "",
                    "compilation": "0",
                    "track": "",
                    "disc": "",
                    "mb_albumid": "",
                    "mb_albumartistid": "",
                    "mb_releasegroupid": "",
                },
                dry_run=False,
            )
            moved_paths[source] = destination
        _move_path(
            source,
            destination,
            dry_run=dry_run,
            review_items=review_items,
        )

    _remove_if_empty(singles_dir, dry_run=dry_run)


def run_relocate_compilations_mode(*, dry_run: bool, folder: str | None) -> list[dict]:
    """Move compilations and rehome loose Various Artists tracks."""
    review_items: list[dict] = []
    if folder:
        buckets = [config.MUSIC_ROOT / folder]
    else:
        buckets = [
            config.MUSIC_ROOT / bucket
            for bucket in sorted(config.LETTER_BUCKETS | {config.SYMBOL_BUCKET})
        ]

    compilation_root = config.MUSIC_ROOT / "0compilations"
    moved_paths: dict[Path, Path] = {}
    for bucket in buckets:
        if not bucket.is_dir() or not is_letter_bucket(bucket.name):
            continue
        for artist_dir in sorted(path for path in bucket.iterdir() if path.is_dir()):
            if artist_dir.name.casefold() not in _COMPILATION_ALBUMARTISTS:
                continue
            _relocate_various_artist_singles(
                artist_dir,
                dry_run=dry_run,
                moved_paths=moved_paths,
                review_items=review_items,
            )
            for album_dir in sorted(path for path in artist_dir.iterdir() if path.is_dir()):
                if album_dir.name == config.ARTIST_SINGLES_FOLDER:
                    continue
                destination_name = _explicit_compilation_name(album_dir)
                if destination_name is None:
                    continue
                destination = compilation_root / destination_name
                if destination.exists():
                    log.warning(
                        "Compilation target exists, skipping: %s → %s",
                        album_dir,
                        destination,
                    )
                    review_items.append({
                        "path": str(album_dir),
                        "reason": "move_conflict",
                        "target": str(destination),
                    })
                    continue
                if not dry_run:
                    record_folder_move(
                        album_dir,
                        destination,
                        moved_paths,
                        audio_extensions=config.AUDIO_EXTENSIONS,
                    )
                _move_path(
                    album_dir,
                    destination,
                    dry_run=dry_run,
                    review_items=review_items,
                )
            _remove_if_empty(artist_dir, dry_run=dry_run)

    if not dry_run:
        update_downstream_paths(
            moved_paths,
            music_root=config.MUSIC_ROOT,
            logger=log,
        )
    return review_items


def run_validate_config_mode() -> list[dict]:
    """Report folder-guardrail mismatches and unclassified top-level folders."""
    review_items: list[dict] = []
    top_level_special = set(config.TOP_LEVEL_SPECIAL)

    for setting, values in (
        ("SPECIAL_FOLDERS", set(config.SPECIAL_FOLDERS)),
        ("SKIP_FOLDERS", set(config.SKIP_FOLDERS)),
    ):
        for name in sorted(values - top_level_special):
            review_items.append({
                "path": str(config.MUSIC_ROOT / name),
                "reason": "config_guardrail_mismatch",
                "setting": setting,
                "detail": "not_in_TOP_LEVEL_SPECIAL",
            })

    known = (
        top_level_special
        | set(config.LETTER_BUCKETS)
        | {config.SYMBOL_BUCKET}
        | set(config.GENRE_FOLDERS)
        | {
            config.INCOMING_FOLDER.name,
            config.STAGED_TRACKS_FOLDER.name,
            config.STAGED_ALBUMS_FOLDER.name,
        }
    )
    if config.MUSIC_ROOT.is_dir():
        for path in sorted(entry for entry in config.MUSIC_ROOT.iterdir() if entry.is_dir()):
            if path.name not in known:
                review_items.append({
                    "path": str(path),
                    "reason": "unclassified_top_level_folder",
                })

    if review_items:
        for item in review_items:
            log.warning("Config validation: %s", item)
    else:
        log.info("Config validation passed")
    return review_items


def _record_intake_playlist(moved_singles: list[Path], *, dry_run: bool) -> Path | None:
    """Record moved singles into a dated intake playlist."""
    if not moved_singles:
        return None
    from datetime import date
    today_str = date.today().isoformat()
    playlist_dir = config.MUSIC_ROOT / "Playlists"
    playlist_path = playlist_dir / f"Intake_{today_str}.m3u8"
    if dry_run:
        log.info("[DRY-RUN] Would record %d moved single(s) into playlist: %s", len(moved_singles), playlist_path)
        return playlist_path

    playlist_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = set()
    existing_content = ""
    if playlist_path.exists():
        existing_content = playlist_path.read_text(encoding="utf-8")
        for line in existing_content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                existing_paths.add(line)

    new_lines = []
    if not existing_content:
        new_lines.append("#EXTM3U\n")

    added = 0
    for file_path in moved_singles:
        try:
            rel = f"../{file_path.relative_to(config.MUSIC_ROOT)}"
        except ValueError:
            rel = str(file_path)
        if rel in existing_paths:
            continue
        tags = read_tags(file_path) or {}
        parsed = parse_filename(file_path)
        artist = (tags.get("artist") or parsed.get("artist") or "Unknown").strip()
        title = (tags.get("title") or parsed.get("title") or file_path.stem).strip()
        new_lines.append(f"#EXTINF:-1,{artist} - {title}\n{rel}\n")
        existing_paths.add(rel)
        added += 1

    if added > 0:
        with playlist_path.open("a" if existing_content else "w", encoding="utf-8") as handle:
            handle.write("".join(new_lines))
        log.info("Recorded %d new single(s) into playlist: %s", added, playlist_path)
    return playlist_path


def main() -> None:
    global log

    parser = argparse.ArgumentParser(
        description="04_move modes: intake, restructure, promote, Tier 1 repair")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--intake", action="store_true",
                      help="Route INCOMING_FOLDER files to staging folders")
    mode.add_argument("--restructure", action="store_true",
                      help="Migrate legacy genre roots to Tier 1 (compatibility mode)")
    mode.add_argument("--promote", action="store_true",
                      help="Promote staged albums to main genre folders")
    mode.add_argument("--to-artist-root", action="store_true",
                      help="Restructure genre-root folders to artist-root layout (dry-run safe)")
    mode.add_argument("--relocate-compilations", action="store_true",
                      help="Move explicitly tagged compilations from artist trees to 0compilations")
    mode.add_argument("--validate-config", action="store_true",
                      help="Report guardrail mismatches and unclassified top-level folders")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Show changes without writing (default: True)")
    parser.add_argument("--no-dry-run", action="store_true",
                        help="Actually move files")
    parser.add_argument("--folder", type=str, default=None,
                        help="Process a single top-level folder only")
    parser.add_argument("--log", type=str, default="review.log",
                        help="Log file path (default: review.log)")
    args = parser.parse_args()

    dry_run = not args.no_dry_run
    log = setup_logger("04_move", Path(args.log))

    if args.intake:
        all_review = run_intake_mode(dry_run=dry_run, folder=args.folder)
    elif args.restructure:
        all_review = run_restructure_mode(dry_run=dry_run, folder=args.folder)
    elif args.to_artist_root:
        all_review = run_to_artist_root_mode(dry_run=dry_run, folder=args.folder)
    elif args.relocate_compilations:
        all_review = run_relocate_compilations_mode(
            dry_run=dry_run,
            folder=args.folder,
        )
    elif args.validate_config:
        all_review = run_validate_config_mode()
    else:
        all_review = run_promote_mode(dry_run=dry_run, folder=args.folder)

    _write_review_items(all_review, dry_run=dry_run)

    if (args.intake or args.to_artist_root) and MOVED_SINGLES:
        _record_intake_playlist(MOVED_SINGLES, dry_run=dry_run)

    log.info("Done (%s)", "DRY-RUN" if dry_run else "LIVE")


if __name__ == "__main__":
    main()

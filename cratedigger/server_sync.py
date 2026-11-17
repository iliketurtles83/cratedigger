"""Guarded preview and application of the existing rsync mirror operation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
from typing import Any

import config
from cratedigger.analytics import source_file_state
from cratedigger.job_logs import read_log_tail
from cratedigger.ops.registry import REPOSITORY_ROOT

_REMOTE = re.compile(
    r"(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9_.-]*)@)?"
    r"(?P<host>[A-Za-z0-9_][A-Za-z0-9_.-]*|\[[0-9A-Fa-f:]+\]):"
    r"(?P<path>/[A-Za-z0-9_./+,-]*)"
)
_CHANGE = "@@sync-change "
_RESULT = "@@sync-plan "


def _walk_error(error: OSError) -> None:
    raise error


def _has_audio(source: Path) -> bool:
    extensions = {suffix.lower() for suffix in config.AUDIO_EXTENSIONS} | {".wma"}
    for folder, directories, filenames in os.walk(source, followlinks=False, onerror=_walk_error):
        parent = Path(folder)
        directories[:] = [
            name for name in directories
            if not name.startswith(".") and name != "0videos" and not (parent / name).is_symlink()
        ]
        for name in filenames:
            path = parent / name
            if not name.startswith(".") and path.suffix.lower() in extensions and not path.is_symlink() and path.is_file():
                return True
    return False


def _overlaps(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def sync_scope(
    source: str | Path,
    destination: str,
    *,
    complete_source: bool,
    protected_library_root: Path | None = None,
) -> tuple[Path, str, Path | None]:
    """Validate a confirmed complete source and an explicit, non-overlapping destination."""
    if not complete_source:
        raise ValueError("Confirm that the source is a complete library mirror")
    if not source or re.search(r"[\x00-\x1f\x7f]", str(source)):
        raise ValueError("Source must be a valid complete mirror directory")
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir() or source_path == Path(source_path.anchor):
        raise ValueError("Source must be an existing library directory, not the filesystem root")
    home = Path.home().resolve()
    repo = REPOSITORY_ROOT.resolve()
    libraries = [Path(config.MUSIC_ROOT).resolve()]
    if protected_library_root is not None:
        libraries.append(protected_library_root.resolve())
    if home.is_relative_to(source_path) or _overlaps(source_path, repo):
        raise ValueError("Source must be the library itself or a complete mirror, not a broader home or repository directory")
    if any(root != source_path and _overlaps(root, source_path) for root in libraries):
        raise ValueError("Source must be the configured library root or a separate complete mirror, not a parent or subfolder")
    if not _has_audio(source_path):
        raise ValueError("Source contains no eligible audio files; refusing an empty or incomplete mirror")
    if not destination or re.search(r"[\x00-\x1f\x7f]", destination):
        raise ValueError("Destination must be an existing local directory or an explicit remote path")
    match = _REMOTE.fullmatch(destination)
    if match:
        remote_path = PurePosixPath(match["path"])
        if remote_path == PurePosixPath("/") or ".." in remote_path.parts:
            raise ValueError("Remote destination must be a non-root absolute path without traversal")
        hostname = match["host"].strip("[]").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                address = ipaddress.IPv4Address(socket.inet_aton(hostname))
            except OSError:
                address = None
        mapped = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost") or (
            any(ip.is_loopback or ip.is_unspecified for ip in (address, mapped) if ip is not None)
        ):
            raise ValueError("Use an absolute local path instead of a loopback remote destination")
        host = f'{match["user"]}@{match["host"]}' if match["user"] else match["host"]
        return source_path, f"{host}:{remote_path}", None
    local = Path(destination).expanduser()
    if not local.is_absolute():
        raise ValueError("Destination must be an absolute local path or user@host:/absolute/path")
    local = local.resolve()
    if not local.is_dir() or local == Path(local.anchor):
        raise ValueError("Local destination must be an existing directory, not the filesystem root")
    if _overlaps(source_path, local):
        raise ValueError("Source and destination must not overlap")
    protected = [repo, *libraries]
    if home.is_relative_to(local) or any(_overlaps(local, root) for root in protected):
        raise ValueError("Local destination must not overlap the home root, repository, or configured music library")
    return source_path, str(local), local


def _sync_command(source: Path, destination: str, *, preview: bool) -> tuple[str, ...]:
    command = ["bash", str(REPOSITORY_ROOT / "scripts" / "server_sync.sh"), "--source", str(source), "--dest", destination]
    if preview:
        command.extend(("--dry-run", "--itemize-plan"))
    return tuple(command)


def _run_sync(source: Path, destination: str, *, preview: bool) -> list[str]:
    env = os.environ.copy()
    env.update(LC_ALL="C", TZ="UTC", SSH_ASKPASS_REQUIRE="never")
    env.setdefault("RSYNC_RSH", "ssh -o BatchMode=yes")
    changes: list[str] = []
    with subprocess.Popen(
        _sync_command(source, destination, preview=preview),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    ) as process:
        if process.stdout is None:
            raise RuntimeError("Could not capture sync output")
        for line in process.stdout:
            print(line, end="", flush=True)
            if preview and line.startswith(_CHANGE):
                changes.append(line[len(_CHANGE):].rstrip("\n"))
        exit_code = process.wait()
    if exit_code:
        raise RuntimeError(f"Sync {'preview' if preview else 'apply'} failed with exit code {exit_code}")
    return changes


def sync_plan_digest(changes: list[str]) -> str:
    digest = hashlib.sha256()
    for change in sorted(changes):
        encoded = change.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def reviewed_sync_plan(log_path: Path, logs_dir: Path, source: Path, destination: str) -> str:
    """Read the recorded plan from a successful preview log."""
    for line in reversed(read_log_tail(log_path, logs_dir)):
        if not line.startswith(_RESULT):
            continue
        try:
            result = json.loads(line[len(_RESULT):])
        except json.JSONDecodeError as exc:
            raise ValueError("Sync preview log has an invalid recorded plan") from exc
        if (
            not isinstance(result, dict)
            or result.get("source") != str(source)
            or result.get("destination") != destination
            or not isinstance(result.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", result["sha256"])
        ):
            raise ValueError("Sync preview log does not match the selected source and destination")
        return result["sha256"]
    raise ValueError("Sync preview has no recorded plan; run a fresh preview")


def run_guarded_sync(
    source: Path,
    destination: str,
    *,
    complete_source: bool,
    dry_run: bool = True,
    expected_plan: str | None = None,
    expected_source_state: tuple[int, int, int, int] | None = None,
    expected_destination_state: tuple[int, int, int, int] | None = None,
    check_destination_state: bool = False,
    protected_library_root: Path | None = None,
) -> dict[str, Any]:
    source, destination, local_destination = sync_scope(
        source, destination, complete_source=complete_source, protected_library_root=protected_library_root
    )
    source_state = source_file_state(source)
    destination_state = source_file_state(local_destination) if local_destination else None
    if expected_source_state is not None and source_state != expected_source_state:
        raise RuntimeError("Sync source changed after the job was queued; no sync was performed")
    if check_destination_state and destination_state != expected_destination_state:
        raise RuntimeError("Sync destination changed after the job was queued; no sync was performed")
    if not dry_run and (not isinstance(expected_plan, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_plan)):
        raise ValueError("A successful reviewed sync preview is required before applying")

    changes = _run_sync(source, destination, preview=True)
    plan = sync_plan_digest(changes)
    if source_file_state(source) != source_state or (
        local_destination is not None and source_file_state(local_destination) != destination_state
    ):
        raise RuntimeError("Sync source or destination changed during preview; no sync was performed")
    summary = {
        "source": str(source),
        "destination": destination,
        "sha256": plan,
        "changes": len(changes),
        "deletions": sum(change.startswith("*deleting") for change in changes),
    }
    if dry_run:
        print(
            f"Preview: {summary['changes']} changes, including {summary['deletions']} deletions. "
            "Review the itemized paths before applying.",
            flush=True,
        )
        print(_RESULT + json.dumps(summary, ensure_ascii=False, separators=(",", ":")), flush=True)
        return summary
    if plan != expected_plan:
        raise RuntimeError("The sync plan changed since preview; no files were transferred or deleted")
    print("Reviewed sync plan matches; starting mirror. Independent changes can still race with rsync.", flush=True)
    _run_sync(source, destination, preview=False)
    return summary

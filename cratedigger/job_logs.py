"""Bounded access to operation log tails."""

from __future__ import annotations

from pathlib import Path


def read_log_tail(path: Path, allowed_dir: Path, *, limit: int = 65536) -> list[str]:
    path = path.resolve()
    if not path.is_relative_to(allowed_dir.resolve()):
        raise ValueError("Job log is outside the configured log directory")
    if limit <= 0:
        raise ValueError("Log tail limit must be positive")
    with path.open("rb") as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - limit)
        stream.seek(start)
        if start:
            stream.readline()
        return stream.read().decode("utf-8").splitlines()

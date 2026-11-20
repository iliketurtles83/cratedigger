"""Content fingerprints for reviewed operation inputs."""

import hashlib
from pathlib import Path


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

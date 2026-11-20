"""Snapshot-only track search and player-recipe similarity."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any

import numpy as np

from cratedigger.analytics import SIMILARITY_DIMS, SNAPSHOT_VERSION, source_file_state
from cratedigger.web.insights import read_snapshot_metadata
from lib.descriptors import DESCRIPTOR_RECIPE
from lib.recommender import RECIPE_VERSION

MAX_VECTOR_BYTES = 256 * 1024 * 1024
MAX_TRACKS = 300_000
SEARCH_LIMIT = 30


@dataclass(frozen=True, slots=True)
class SnapshotTrack:
    audio_hash: str
    path: str | None
    artist: str | None
    album: str | None
    title: str | None
    genre: str | None
    vector_index: int | None
    search_text: str

    @property
    def display_title(self) -> str:
        return self.title or (Path(self.path).stem if self.path else "(unknown title)")


@dataclass(frozen=True, slots=True)
class SimilarityIndex:
    metadata: dict[str, Any]
    tracks: tuple[SnapshotTrack, ...]
    by_hash: dict[str, SnapshotTrack]
    vector_tracks: tuple[SnapshotTrack, ...]
    vectors: np.ndarray

    @property
    def uses_current_recipe(self) -> bool:
        return (
            self.metadata.get("similarity_recipe") == RECIPE_VERSION
            and self.metadata.get("descriptor_recipe") == DESCRIPTOR_RECIPE
        )

    def search(self, query: str) -> tuple[list[SnapshotTrack], int]:
        terms = query.casefold().split()
        if not terms:
            return [], 0
        matches = [track for track in self.tracks if all(term in track.search_text for term in terms)]
        return matches[:SEARCH_LIMIT], len(matches)

    def neighbors(self, seed: SnapshotTrack, *, limit: int = 10) -> list[tuple[SnapshotTrack, float]]:
        if seed.vector_index is None:
            return []
        if not 1 <= limit <= 200:
            raise ValueError("Invalid similarity result limit")
        scores = self.vectors @ self.vectors[seed.vector_index]
        scores[seed.vector_index] = -np.inf
        count = min(limit, len(self.vector_tracks) - 1)
        order = np.argsort(-scores, kind="stable")[:count]
        return [(self.vector_tracks[int(index)], float(np.clip(scores[index], -1, 1))) for index in order]


def _load_index(path: Path) -> SimilarityIndex:
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            metadata = read_snapshot_metadata(conn)
            if metadata["snapshot_version"] != SNAPSHOT_VERSION:
                return SimilarityIndex(metadata, (), {}, (), np.empty((0, SIMILARITY_DIMS), dtype=np.float32))
            track_count = metadata["unique_tracks"]
            vector_count = metadata["similarity_tracks"]
            if track_count > MAX_TRACKS or vector_count * SIMILARITY_DIMS * 4 > MAX_VECTOR_BYTES:
                raise ValueError("Analytics snapshot exceeds the similarity explorer's in-memory limit")
            vectors = np.empty((vector_count, SIMILARITY_DIMS), dtype=np.float32)
            tracks: list[SnapshotTrack] = []
            vector_tracks: list[SnapshotTrack] = []
            by_hash: dict[str, SnapshotTrack] = {}
            rows = conn.execute(
                "SELECT t.audio_hash, t.path, t.artist, t.album, t.title, t.genre, v.vector "
                "FROM tracks AS t LEFT JOIN similarity_vectors AS v ON v.audio_hash=t.audio_hash "
                "ORDER BY t.artist COLLATE NOCASE, t.album COLLATE NOCASE, t.title COLLATE NOCASE, t.audio_hash"
            )
            for row in rows:
                audio_hash = row["audio_hash"]
                if not isinstance(audio_hash, str) or not audio_hash.strip() or audio_hash in by_hash:
                    raise ValueError("Analytics snapshot has an invalid or duplicate audio hash")
                index = None
                blob = row["vector"]
                if blob is not None:
                    index = len(vector_tracks)
                    if not isinstance(blob, bytes) or len(blob) != SIMILARITY_DIMS * 4 or index >= vector_count:
                        raise ValueError("Analytics snapshot contains an invalid similarity vector")
                    vectors[index] = np.frombuffer(blob, dtype="<f4")
                fields = [row[name] for name in ("path", "artist", "album", "title", "genre")]
                if any(value is not None and not isinstance(value, str) for value in fields):
                    raise ValueError("Analytics snapshot contains invalid track metadata")
                path_value, artist, album, title, genre = fields
                search_text = " ".join(value for value in fields if value).casefold()
                track = SnapshotTrack(audio_hash, path_value, artist, album, title, genre, index, search_text)
                tracks.append(track)
                by_hash[audio_hash] = track
                if index is not None:
                    vector_tracks.append(track)
            if len(tracks) != track_count or len(vector_tracks) != vector_count:
                raise ValueError("Analytics snapshot similarity counts do not match its contents")
            if vector_count:
                norms = np.linalg.norm(vectors, axis=1)
                if not np.isfinite(vectors).all() or not np.all(np.isclose(norms, 1.0, rtol=1e-5, atol=1e-5)):
                    raise ValueError("Analytics snapshot contains non-finite or unnormalized similarity vectors")
            vectors.setflags(write=False)
            return SimilarityIndex(metadata, tuple(tracks), by_hash, tuple(vector_tracks), vectors)
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"Cannot read analytics snapshot: {exc}") from exc


class SimilarityCache:
    """Keep one bounded in-memory index and invalidate it on snapshot replacement."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._path: Path | None = None
        self._identity: tuple[int, int, int, int] | None = None
        self._index: SimilarityIndex | None = None

    def get(self, snapshot_path: Path) -> SimilarityIndex | None:
        with self._lock:
            for _ in range(2):
                try:
                    path = snapshot_path.resolve()
                    identity = source_file_state(path)
                except FileNotFoundError:
                    self._path = self._identity = self._index = None
                    return None
                except OSError as exc:
                    raise ValueError(f"Cannot inspect analytics snapshot: {exc}") from exc
                if not path.is_file():
                    raise ValueError(f"Analytics snapshot is not a file: {path}")
                if self._path == path and self._identity == identity:
                    return self._index
                index = _load_index(path)
                try:
                    unchanged = source_file_state(path) == identity
                except FileNotFoundError:
                    unchanged = False
                except OSError as exc:
                    raise ValueError(f"Cannot inspect analytics snapshot: {exc}") from exc
                if unchanged:
                    self._path, self._identity, self._index = path, identity, index
                    return index
            raise ValueError("Analytics snapshot changed while loading; try again")

"""Audio-only file hash: the same value before and after a tag edit, move or rename.

Tags are skipped by finding the audio region of the file (MP3, FLAC, MP4/M4A,
WAV). Other formats (Ogg, Opus, WMA) hash the whole file, so a retag changes
their hash. The registry still keeps their track_id when the path is unchanged.

Only the head and tail of the audio region are read, plus its length, so a scan
over a network share stays fast. Identical audio (the same rip filed twice)
gives the same hash; the registry never treats the hash as unique.
"""
from __future__ import annotations

import hashlib
import os
import struct
from typing import BinaryIO

HASH_VERSION = "a1"
SAMPLE_BYTES = 256 * 1024


def _skip_id3v2(f: BinaryIO, pos: int) -> int:
    """Return the offset after any ID3v2 tags starting at pos."""
    while True:
        f.seek(pos)
        header = f.read(10)
        if len(header) < 10 or header[:3] != b"ID3":
            return pos
        size = 0
        for b in header[6:10]:
            size = (size << 7) | (b & 0x7F)
        footer = 10 if header[5] & 0x10 else 0
        pos += 10 + size + footer


def _strip_trailers(f: BinaryIO, start: int, end: int) -> int:
    """Return the end of the audio after removing ID3v1, APEv2 and Lyrics3v2 trailers."""
    while end - start >= 32:
        if end - start >= 128:
            f.seek(end - 128)
            if f.read(3) == b"TAG":
                end -= 128
                continue
        f.seek(end - 32)
        footer = f.read(32)
        if footer[:8] == b"APETAGEX":
            tag_size, flags = struct.unpack("<I4xI", footer[12:24])
            end -= tag_size + (32 if flags & 0x80000000 else 0)
            continue
        if end - start >= 15:
            f.seek(end - 15)
            tail = f.read(15)
            if tail[6:] == b"LYRICS200" and tail[:6].isdigit():
                end -= int(tail[:6]) + 15
                continue
        break
    return max(end, start)


def _flac_audio_start(f: BinaryIO, pos: int) -> int:
    f.seek(pos)
    if f.read(4) != b"fLaC":
        return pos
    pos += 4
    while True:
        f.seek(pos)
        header = f.read(4)
        if len(header) < 4:
            return pos
        pos += 4 + int.from_bytes(header[1:4], "big")
        if header[0] & 0x80:
            return pos


def _mp4_mdat(f: BinaryIO, size: int) -> tuple[int, int] | None:
    pos = 0
    while pos + 8 <= size:
        f.seek(pos)
        atom_size, atom_type = struct.unpack(">I4s", f.read(8))
        header = 8
        if atom_size == 1:
            atom_size = struct.unpack(">Q", f.read(8))[0]
            header = 16
        elif atom_size == 0:
            atom_size = size - pos
        if atom_size < header:
            return None
        if atom_type == b"mdat":
            return pos + header, min(pos + atom_size, size)
        pos += atom_size
    return None


def _wav_data(f: BinaryIO, size: int) -> tuple[int, int] | None:
    f.seek(0)
    if f.read(4) != b"RIFF":
        return None
    pos = 12
    while pos + 8 <= size:
        f.seek(pos)
        chunk_id, chunk_size = struct.unpack("<4sI", f.read(8))
        if chunk_id == b"data":
            return pos + 8, min(pos + 8 + chunk_size, size)
        pos += 8 + chunk_size + (chunk_size & 1)
    return None


def audio_region(f: BinaryIO, size: int, ext: str) -> tuple[int, int]:
    """Return the [start, end) byte range holding the audio, excluding tags."""
    ext = ext.lower()
    if ext in (".mp3", ".mp2"):
        start = _skip_id3v2(f, 0)
        return start, _strip_trailers(f, start, size)
    if ext == ".flac":
        start = _flac_audio_start(f, _skip_id3v2(f, 0))
        return start, _strip_trailers(f, start, size)
    if ext in (".m4a", ".mp4", ".aac", ".m4b", ".alac"):
        return _mp4_mdat(f, size) or (0, size)
    if ext == ".wav":
        return _wav_data(f, size) or (0, size)
    return 0, size


def audio_hash(path: str) -> str:
    """Hash the audio region of a file, as "<version>:<hex>". Raises OSError."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        start, end = audio_region(f, size, os.path.splitext(path)[1])
        length = max(end - start, 0)
        h = hashlib.sha256(str(length).encode())
        f.seek(start)
        if length <= 2 * SAMPLE_BYTES:
            h.update(f.read(length))
        else:
            h.update(f.read(SAMPLE_BYTES))
            f.seek(end - SAMPLE_BYTES)
            h.update(f.read(SAMPLE_BYTES))
    return f"{HASH_VERSION}:{h.hexdigest()[:32]}"



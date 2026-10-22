"""tests/test_tags_artwork.py — Tests for embedded artwork detection, embedding, and hash safety."""

import io
from pathlib import Path
import struct
import tempfile
import unittest

from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover

from lib.audiohash import audio_hash
from lib.tags import embed_artwork, has_embedded_artwork


class TagsArtworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 1x1 transparent red PNG
        self.dummy_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0"
            b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        # Minimal JPEG header bytes
        self.dummy_jpg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00" + b"\x00" * 64 + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_dummy_mp3(self, name: str = "test.mp3") -> Path:
        """Create a minimal valid MPEG-1 Layer 3 audio stream."""
        p = self.root / name
        # MPEG-1 Layer 3 128kbps, 44.1kHz, stereo: frame size 417 bytes
        frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
        p.write_bytes(frame * 10)
        return p

    def _create_dummy_flac(self, name: str = "test.flac") -> Path:
        """Create a minimal valid FLAC stream."""
        p = self.root / name
        # fLaC marker + STREAMINFO block with sample rate 44100 + audio bytes
        # STREAMINFO block header: last=1, type=0 (STREAMINFO), length=34
        # Minimum block size (2B), max block size (2B), min frame (3B), max frame (3B)
        # sample rate (20b), channels (3b), bps (5b), total samples (36b)
        # 44100 Hz = 0x0AC44; 2 channels, 16 bits = 0x0AC44 1 0F -> 0x0a, 0xc4, 0x42, 0xf0...
        streaminfo = bytearray(34)
        struct.pack_into(">H", streaminfo, 0, 4096)  # min block size
        struct.pack_into(">H", streaminfo, 2, 4096)  # max block size
        struct.pack_into(">I", streaminfo, 10, (44100 << 12) | (1 << 9) | (15 << 4))
        block_hdr = bytes([0x80]) + struct.pack(">I", len(streaminfo))[1:]
        audio_stream = b"\xff\xf8" * 500
        p.write_bytes(b"fLaC" + block_hdr + streaminfo + audio_stream)
        return p

    def _create_dummy_m4a(self, name: str = "test.m4a") -> Path:
        """Create a minimal valid MP4/M4A structure."""
        p = self.root / name
        # ftyp atom (32 bytes)
        ftyp_data = b"M4A " + b"\x00\x00\x02\x00" + b"isommp42M4A "
        ftyp = struct.pack(">I", len(ftyp_data) + 8) + b"ftyp" + ftyp_data
        # moov atom with minimal udta/meta/ilst structure
        moov_inner = b"\x00\x00\x00\x08moov"
        # mdat atom with audio payload
        audio_data = b"\xaa\xbb\xcc\xdd" * 100
        mdat = struct.pack(">I", len(audio_data) + 8) + b"mdat" + audio_data
        p.write_bytes(ftyp + moov_inner + mdat)
        return p

    def test_has_embedded_artwork_mp3(self) -> None:
        mp3_path = self._create_dummy_mp3()
        self.assertFalse(has_embedded_artwork(mp3_path))

        # Add APIC frame
        audio = MP3(str(mp3_path))
        audio.add_tags()
        audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=self.dummy_jpg))
        audio.save(v2_version=4)

        self.assertTrue(has_embedded_artwork(mp3_path))

    def test_has_embedded_artwork_flac(self) -> None:
        flac_path = self._create_dummy_flac()
        self.assertFalse(has_embedded_artwork(flac_path))

        audio = FLAC(str(flac_path))
        pic = Picture()
        pic.data = self.dummy_jpg
        pic.type = 3
        pic.mime = "image/jpeg"
        audio.add_picture(pic)
        audio.save()

        self.assertTrue(has_embedded_artwork(flac_path))

    def test_embed_artwork_mp3_hash_safety(self) -> None:
        mp3_path = self._create_dummy_mp3("singles.mp3")
        hash_before = audio_hash(str(mp3_path))

        # Embed artwork live
        embedded = embed_artwork(mp3_path, self.dummy_jpg, mime_type="image/jpeg", dry_run=False)
        self.assertTrue(embedded)
        self.assertTrue(has_embedded_artwork(mp3_path))

        hash_after = audio_hash(str(mp3_path))
        self.assertEqual(hash_before, hash_after, "MP3 audio_hash must not change when embedding art")

    def test_embed_artwork_flac_hash_safety(self) -> None:
        flac_path = self._create_dummy_flac("singles.flac")
        hash_before = audio_hash(str(flac_path))

        embedded = embed_artwork(flac_path, self.dummy_jpg, mime_type="image/jpeg", dry_run=False)
        self.assertTrue(embedded)
        self.assertTrue(has_embedded_artwork(flac_path))

        hash_after = audio_hash(str(flac_path))
        self.assertEqual(hash_before, hash_after, "FLAC audio_hash must not change when embedding art")

    def test_embed_artwork_dry_run(self) -> None:
        mp3_path = self._create_dummy_mp3("dry_run.mp3")
        self.assertFalse(has_embedded_artwork(mp3_path))

        # Dry run returns True (would embed) but does not write to disk
        result = embed_artwork(mp3_path, self.dummy_jpg, mime_type="image/jpeg", dry_run=True)
        self.assertTrue(result)
        self.assertFalse(has_embedded_artwork(mp3_path))

    def test_embed_artwork_ogg_refusal(self) -> None:
        ogg_path = self.root / "track.ogg"
        ogg_path.write_bytes(b"OggS" + b"\x00" * 100)

        # Without force: refused
        result = embed_artwork(ogg_path, self.dummy_jpg, dry_run=False, force=False)
        self.assertFalse(result)

    def test_embed_empty_bytes(self) -> None:
        mp3_path = self._create_dummy_mp3("empty.mp3")
        self.assertFalse(embed_artwork(mp3_path, b"", dry_run=False))


if __name__ == "__main__":
    unittest.main()

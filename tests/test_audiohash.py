"""Unit tests for lib/audiohash.py."""

import struct
import tempfile
import unittest
import wave
from pathlib import Path

from lib.audiohash import audio_hash, HASH_VERSION, SAMPLE_BYTES
from lib.tags import write_tags


def _create_wav(path: Path, num_samples: int = 1000) -> None:
    """Create a minimal valid PCM WAV file."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(22050)
        wf.writeframes(b"\x00\x00" * num_samples)


class TestAudioHash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_hash_format(self):
        wav_file = self.root / "test.wav"
        _create_wav(wav_file, num_samples=500)
        h = audio_hash(str(wav_file))
        self.assertTrue(h.startswith(f"{HASH_VERSION}:"))
        parts = h.split(":")
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[1]), 32)

    def test_short_file_determinism(self):
        f1 = self.root / "track1.wav"
        f2 = self.root / "track2.wav"
        _create_wav(f1, num_samples=100)
        _create_wav(f2, num_samples=100)
        self.assertEqual(audio_hash(str(f1)), audio_hash(str(f2)))

    def test_different_audio_different_hash(self):
        f1 = self.root / "audio1.wav"
        f2 = self.root / "audio2.wav"
        _create_wav(f1, num_samples=100)
        _create_wav(f2, num_samples=200)
        self.assertNotEqual(audio_hash(str(f1)), audio_hash(str(f2)))

    def test_id3_tag_invariance_mp3(self):
        """Simulate an MP3 with prepended ID3v2 and appended ID3v1."""
        mp3_audio = b"\xff\xfb\x90\x64" * 1000  # synthetic audio frames
        base_path = self.root / "clean.mp3"
        base_path.write_bytes(mp3_audio)
        clean_hash = audio_hash(str(base_path))

        # Add ID3v2 header with matching payload length
        id3_body = b"TIT2\x00\x00\x00\x05\x00\x00\x00Hello"
        tag_size = len(id3_body)
        id3v2_tag = b"ID3\x03\x00\x00\x00\x00\x00" + bytes([tag_size]) + id3_body
        id3v1_tag = b"TAG" + b"X" * 125
        tagged_path = self.root / "tagged.mp3"
        tagged_path.write_bytes(id3v2_tag + mp3_audio + id3v1_tag)
        tagged_hash = audio_hash(str(tagged_path))

        self.assertEqual(clean_hash, tagged_hash)

    def test_flac_metadata_invariance(self):
        """Verify that FLAC metadata blocks before audio frames are skipped."""
        audio_stream = b"\xff\xf8" * 500
        # FLAC header 'fLaC' followed by a metadata block header with last-block flag (0x80)
        meta_block_payload = b"\x00" * 34  # STREAMINFO
        meta_block_hdr = bytes([0x80]) + struct.pack(">I", len(meta_block_payload))[1:]
        flac_content = b"fLaC" + meta_block_hdr + meta_block_payload + audio_stream

        f1 = self.root / "f1.flac"
        f1.write_bytes(flac_content)
        h1 = audio_hash(str(f1))

        # Add an additional intermediate metadata block (e.g. VORBIS_COMMENT, not last block)
        vorbis_payload = b"test comment block"
        vorbis_hdr = bytes([0x04]) + struct.pack(">I", len(vorbis_payload))[1:]
        flac_modified = b"fLaC" + vorbis_hdr + vorbis_payload + meta_block_hdr + meta_block_payload + audio_stream
        f2 = self.root / "f2.flac"
        f2.write_bytes(flac_modified)
        h2 = audio_hash(str(f2))

        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()

"""Integration tests for complete-library sync through a USB mirror."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "server_sync.sh"


@unittest.skipUnless(shutil.which("rsync") and shutil.which("bash"), "rsync and bash are required")
class TestServerSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.usb = self.root / "usb"
        self.nas = self.root / "nas"
        for directory in (self.source, self.usb, self.nas):
            directory.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def snapshot(self, root):
        return {
            str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
            for path in root.rglob("*")
        }

    def sync(self, source, destination, dry_run):
        command = ["bash", str(SCRIPT), "--source", str(source), "--dest", str(destination)]
        if dry_run:
            command.append("--dry-run")
        return subprocess.run(command, check=True, capture_output=True, text=True)

    def test_two_hop_mirror_previews_deletions_and_preserves_exclusions(self):
        self.write(self.source, "A/Artist/track.mp3", b"new audio")
        self.write(self.source, "Playlists/Faves.m3u8", b"#EXTM3U\n../A/Artist/track.mp3\n")
        self.write(self.source, ".DS_Store", b"junk")
        self.write(self.source, "A/Artist/track.vdjstems", b"junk")
        self.write(self.source, "A/Artist/track~", b"junk")
        self.write(self.source, "0videos/source.mp4", b"excluded")

        for destination in (self.usb, self.nas):
            self.write(destination, "obsolete.mp3", b"stale")
            self.write(destination, "Old/.hidden", b"perishable")
        self.write(self.usb, "0videos/usb.mp4", b"usb video")
        self.write(self.nas, "0videos/nas.mp4", b"nas video")
        self.write(self.nas, "A/Artist/track.mp3", b"old and different audio")

        usb_before = self.snapshot(self.usb)
        self.sync(self.source, self.usb, dry_run=True)
        self.assertEqual(self.snapshot(self.usb), usb_before)
        self.sync(self.source, self.usb, dry_run=False)

        self.assertEqual((self.usb / "A/Artist/track.mp3").read_bytes(), b"new audio")
        self.assertTrue((self.usb / "Playlists/Faves.m3u8").exists())
        self.assertFalse((self.usb / "obsolete.mp3").exists())
        self.assertFalse((self.usb / "Old").exists())
        self.assertFalse((self.usb / ".DS_Store").exists())
        self.assertFalse((self.usb / "A/Artist/track.vdjstems").exists())
        self.assertFalse((self.usb / "A/Artist/track~").exists())
        self.assertTrue((self.usb / "0videos/usb.mp4").exists())
        self.assertFalse((self.usb / "0videos/source.mp4").exists())

        nas_before = self.snapshot(self.nas)
        self.sync(self.usb, self.nas, dry_run=True)
        self.assertEqual(self.snapshot(self.nas), nas_before)
        self.sync(self.usb, self.nas, dry_run=False)

        self.assertEqual((self.nas / "A/Artist/track.mp3").read_bytes(), b"new audio")
        self.assertEqual(
            (self.nas / "Playlists/Faves.m3u8").read_bytes(),
            b"#EXTM3U\n../A/Artist/track.mp3\n",
        )
        self.assertFalse((self.nas / "obsolete.mp3").exists())
        self.assertFalse((self.nas / "Old").exists())
        self.assertFalse((self.nas / ".DS_Store").exists())
        self.assertFalse((self.nas / "A/Artist/track.vdjstems").exists())
        self.assertFalse((self.nas / "A/Artist/track~").exists())
        self.assertTrue((self.nas / "0videos/nas.mp4").exists())
        self.assertFalse((self.nas / "0videos/usb.mp4").exists())


if __name__ == "__main__":
    unittest.main()

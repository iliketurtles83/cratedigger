from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config
from lib.context import classify_folder, get_folder_context


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)

        patchers = [
            patch.object(config, "MUSIC_ROOT", self.root),
            patch.object(config, "FOLDER_TO_GENRE", {"rock": "Rock", "soundtrack": "Soundtrack"}),
            patch.object(config, "GENRE_FOLDERS", {"rock", "soundtrack"}),
            patch.object(config, "SUBGENRE_BUCKETS", {"0alt rock": "Alt-Rock"}),
            patch.object(config, "SPECIAL_FOLDERS", {"0new", "0random", "0compilations", "0various"}),
            patch.object(config, "SKIP_FOLDERS", {"0videos"}),
            patch.object(config, "NO_BPM_FOLDERS", {"spoken", "0mixes"}),
            patch.object(config, "ALBUMARTIST_FOLDERS", {"0compilations", "0various", "soundtrack"}),
            patch.object(config, "AUDIO_EXTENSIONS", {".mp3", ".flac"}),
        ]

        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.addCleanup(self.tempdir.cleanup)

    def _touch(self, relative_path: str) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_classify_disc_folder(self) -> None:
        folder = self.root / "rock" / "Artist" / "Disc 2"
        folder.mkdir(parents=True)

        self.assertEqual(classify_folder(folder), "disc")

    def test_classify_artist_folder(self) -> None:
        folder = self.root / "rock" / "Tycho"
        (folder / "Tycho - Dive (2011)").mkdir(parents=True)

        self.assertEqual(classify_folder(folder), "artist")

    def test_classify_artist_mixed_folder(self) -> None:
        folder = self.root / "rock" / "Tycho"
        (folder / "Tycho - Dive (2011)").mkdir(parents=True)
        self._touch("rock/Tycho/01 - Tycho - A Walk.mp3")

        self.assertEqual(classify_folder(folder), "artist_mixed")

    def test_classify_artist_flat_folder(self) -> None:
        folder = self.root / "rock" / "Tycho"
        folder.mkdir(parents=True)
        self._touch("rock/Tycho/01 - Tycho - A Walk.mp3")

        self.assertEqual(classify_folder(folder), "artist_flat")

    def test_classify_subgenre_and_local_special_folders(self) -> None:
        subgenre = self.root / "rock" / "0alt rock"
        local_special = self.root / "rock" / "0compilations"
        subgenre.mkdir(parents=True)
        local_special.mkdir(parents=True)

        self.assertEqual(classify_folder(subgenre), "subgenre")
        self.assertEqual(classify_folder(local_special), "local_special")

    def test_get_folder_context_for_subgenre_album_file(self) -> None:
        path = self._touch("rock/0alt rock/Tycho - Dive (2011)/01 - Tycho - A Walk.mp3")

        context = get_folder_context(path)

        self.assertEqual(context.top, "rock")
        self.assertEqual(context.genre, "Rock")
        self.assertEqual(context.subgenre, "Alt-Rock")
        self.assertEqual(context.folder_kind, "album")
        self.assertTrue(context.is_genre_folder)
        self.assertFalse(context.is_special_folder)
        self.assertFalse(context.is_compilation)
        self.assertFalse(context.is_soundtrack)
        self.assertTrue(context.needs_bpm)
        self.assertFalse(context.skip)
        self.assertEqual(context.depth, 3)

    def test_get_folder_context_marks_local_compilation(self) -> None:
        path = self._touch("rock/0compilations/Best of 90s/01 - Artist - Track.mp3")

        context = get_folder_context(path)

        self.assertEqual(context.top, "rock")
        self.assertTrue(context.is_compilation)
        self.assertFalse(context.is_soundtrack)

    def test_get_folder_context_marks_local_various_bucket_compilation(self) -> None:
        path = self._touch("rock/0various/Collection/01 - Artist - Track.mp3")

        context = get_folder_context(path)

        self.assertEqual(context.top, "rock")
        self.assertTrue(context.is_compilation)

    def test_get_folder_context_does_not_mark_nested_bucket_name_as_compilation(self) -> None:
        path = self._touch("rock/Artist/0compilations/Best of 90s/01 - Artist - Track.mp3")

        context = get_folder_context(path)

        self.assertEqual(context.top, "rock")
        self.assertFalse(context.is_compilation)

    def test_get_folder_context_does_not_mark_subgenre_nested_compilation_bucket(self) -> None:
        path = self._touch("rock/0alt rock/0compilations/Best of 90s/01 - Artist - Track.mp3")

        context = get_folder_context(path)

        self.assertEqual(context.top, "rock")
        self.assertEqual(context.subgenre, "Alt-Rock")
        self.assertFalse(context.is_compilation)


if __name__ == "__main__":
    unittest.main()

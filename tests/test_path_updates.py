import logging
import os
import pickle
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from lib.path_updates import update_downstream_paths


class PathUpdatesTests(unittest.TestCase):
    def test_consolidates_rows_when_source_maps_to_existing_path(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            music_root = root / "music"
            old_path = music_root / "A" / "Artist" / "Album" / "old.mp3"
            new_path = music_root / "A" / "Artist" / "Album" / "new.mp3"
            home = root / "home"
            database = home / ".config" / "beets" / "library.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE items (path BLOB)")
                connection.executemany(
                    "INSERT INTO items (path) VALUES (?)",
                    [(str(old_path).encode(),), (str(new_path).encode(),)],
                )

            dataframe = pd.DataFrame(
                [
                    {"path": str(old_path), "value": "source"},
                    {"path": str(new_path), "value": "destination"},
                ]
            )
            with (root / "features.pkl").open("wb") as handle:
                pickle.dump(dataframe, handle)

            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch("lib.path_updates.Path.home", return_value=home):
                    update_downstream_paths(
                        {old_path: new_path},
                        music_root=music_root,
                        logger=logging.getLogger("test"),
                    )
            finally:
                os.chdir(previous_cwd)

            with (root / "features.pkl").open("rb") as handle:
                updated = pickle.load(handle)
            self.assertEqual(updated["path"].tolist(), [str(new_path)])
            self.assertEqual(updated["value"].tolist(), ["destination"])

            with sqlite3.connect(database) as connection:
                paths = connection.execute("SELECT path FROM items").fetchall()
            self.assertEqual(paths, [(str(new_path).encode(),)])


if __name__ == "__main__":
    unittest.main()

import logging
import os
import pickle
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

if __name__ == "__main__":
    unittest.main()

"""Keep path-indexed downstream data synchronized after library moves."""

import logging
import pickle
import sqlite3
from pathlib import Path


def record_folder_move(
    old_folder: Path,
    new_folder: Path,
    path_map: dict[Path, Path],
    *,
    audio_extensions: set[str],
) -> None:
    """Record audio-file path transformations before moving a folder."""
    if not old_folder.exists():
        return
    current_to_original = {current: original for original, current in path_map.items()}
    for path in old_folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in audio_extensions:
            continue
        relative = path.relative_to(old_folder)
        original = current_to_original.get(path, path)
        path_map[original] = new_folder / relative


def update_downstream_paths(
    path_map: dict[Path, Path],
    *,
    music_root: Path,
    logger: logging.Logger,
) -> None:
    """Update playlists, features.pkl, and the beets DB after path changes."""
    if not path_map:
        return

    playlists_dir = music_root / "Playlists"
    if playlists_dir.exists():
        updated_playlists = 0
        for playlist in playlists_dir.glob("*.m3u8"):
            try:
                content = playlist.read_text(encoding="utf-8")
                updated = content
                for old_path, new_path in path_map.items():
                    old_relative = str(old_path.relative_to(music_root))
                    new_relative = str(new_path.relative_to(music_root))
                    updated = updated.replace(f"../{old_relative}", f"../{new_relative}")
                    updated = updated.replace(old_relative, new_relative)
                if updated != content:
                    playlist.write_text(updated, encoding="utf-8")
                    updated_playlists += 1
            except (OSError, UnicodeError, ValueError) as exc:
                logger.warning("Could not update playlist %s: %s", playlist.name, exc)
        if updated_playlists:
            logger.info("Updated %d playlist(s) with changed paths", updated_playlists)

    features_path = Path("features.pkl")
    if features_path.exists():
        try:
            with features_path.open("rb") as handle:
                dataframe = pickle.load(handle)
            updates = {str(old): str(new) for old, new in path_map.items()}
            mask = dataframe["path"].isin(updates)
            if mask.any():
                dataframe.loc[mask, "path"] = dataframe.loc[mask, "path"].map(updates)
                with features_path.open("wb") as handle:
                    pickle.dump(dataframe, handle)
                logger.info("Updated %d track path(s) in features.pkl", mask.sum())
        except (OSError, KeyError, TypeError, ValueError, pickle.PickleError) as exc:
            logger.warning("Could not update features.pkl: %s", exc)

    database = Path.home() / ".config" / "beets" / "library.db"
    if not database.exists():
        return
    try:
        with sqlite3.connect(database) as connection:
            updated_items = 0
            for old_path, new_path in path_map.items():
                old_relative = str(old_path.relative_to(music_root)).encode("utf-8")
                new_relative = str(new_path.relative_to(music_root)).encode("utf-8")
                old_absolute = str(old_path).encode("utf-8")
                new_absolute = str(new_path).encode("utf-8")
                cursor = connection.execute(
                    "UPDATE items SET path = ? WHERE path = ?",
                    (new_relative, old_relative),
                )
                updated_items += cursor.rowcount
                cursor = connection.execute(
                    "UPDATE items SET path = ? WHERE path = ?",
                    (new_absolute, old_absolute),
                )
                updated_items += cursor.rowcount
        if updated_items:
            logger.info("Updated %d item path(s) in beets library.db", updated_items)
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning("Could not update beets DB: %s", exc)

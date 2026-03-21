from pathlib import Path

# Copy this file to config.py and fill in your own values

MUSIC_ROOT = Path("/path/to/your/music")

GENRE_FOLDERS = {
    "genre1", "genre2", "genre3",
    # add your genre folder names here
}

SPECIAL_FOLDERS = {
    "0inbox", "0favourites",
    # add your special folder names here
}

SKIP_FOLDERS = {"0skip"}

FOLDER_TO_GENRE = {
    "genre1": "Genre One",
    "genre2": "Genre Two",
    # subfolder genres for random/inbox folders
    "subgenre1": "Subgenre One",
}

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

MB_RATE_LIMIT_SECONDS = 1.1
ACOUSTID_MIN_SCORE    = 0.8
MB_MIN_TAG_VOTES      = 2
MB_MAX_GENRES         = 5
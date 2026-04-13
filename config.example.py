from pathlib import Path

# Copy this file to config.py and fill in your own values

MUSIC_ROOT = Path("/path/to/your/music")

# ── Intake / staging ──────────────────────────────────────────────────────────
INCOMING_FOLDER      = MUSIC_ROOT / "incoming"        # drop zone
STAGED_TRACKS_FOLDER = MUSIC_ROOT / "new_songs"       # loose files by genre, awaiting decision
STAGED_ALBUMS_FOLDER = MUSIC_ROOT / "new_albums"      # albums by genre, review before promoting

# Special folders — tag only, never move or rename files
SPECIAL_FOLDERS = {
    "0faves", "0faves_alltime", "0random", "0random_good",
    "0new", "0shacks", "0compilations", "0various", "0mixes",
}

SKIP_FOLDERS = {"0videos"}

# Folders where BPM detection makes no sense (speech, ambient, mixes)
NO_BPM_FOLDERS = {"spoken", "0mixes"}

# Folders where albumartist tag is relevant
ALBUMARTIST_FOLDERS = {"0compilations", "0various", "soundtrack"}

# Single source of truth: folder name → display genre.
# GENRE_FOLDERS is derived from this — never edit GENRE_FOLDERS directly.
FOLDER_TO_GENRE = {
    "genre1": "Genre One",
    "genre2": "Genre Two",
}

# Derived automatically from FOLDER_TO_GENRE — never edit directly
GENRE_FOLDERS = set(FOLDER_TO_GENRE.keys())

# Subgenre bucket folders inside genre folders — folder name : display genre
SUBGENRE_BUCKETS = {
    "0subgenre-one": "Subgenre One",
}

# Placeholder values that indicate bad/missing data — trigger MB lookup
SUSPICIOUS_TAG_VALUES = {
    "unknown artist", "unknown", "track", "untitled",
    "artist", "album artist", "no artist", "various artists",
}

# Artists with this many or more albums get their own artist subfolder.
# Below this they live flat in the genre root.
ARTIST_FOLDER_THRESHOLD = 3

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

MB_RATE_LIMIT_SECONDS = 1.1
ACOUSTID_MIN_SCORE    = 0.8
MB_MIN_TAG_VOTES      = 2
MB_MAX_GENRES         = 5
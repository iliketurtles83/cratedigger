from pathlib import Path

# Copy this file to config.py and fill in your own values

MUSIC_ROOT = Path("/path/to/your/music")

# ── Intake / staging ──────────────────────────────────────────────────────────
INCOMING_FOLDER      = MUSIC_ROOT / "incoming"        # drop zone
STAGED_TRACKS_FOLDER = MUSIC_ROOT / "new_songs"       # loose files by genre, awaiting decision
STAGED_ALBUMS_FOLDER = MUSIC_ROOT / "new_albums"      # albums by genre, review before promoting

# ── Artist-root letter bucketing ──────────────────────────────────────────────
LETTER_BUCKETING  = True
ARTICLE_STRIP     = ["The", "A", "An"]    # stripped for bucketing only, not from folder name
SYMBOL_BUCKET     = "#"                   # bucket for artists starting with numbers/symbols

ARTIST_SINGLES_FOLDER = "0singles"        # created under artist folder when needed
ROOT_SINGLES_FOLDER   = "0singles"        # root-level holding pen for homeless singles

# Top-level special folders — never renamed, never moved, no genre injected from path
TOP_LEVEL_SPECIAL = {
    "0compilations",
    "0various",
    "0mixes",
    "0singles",
    "0random",
    "#",
}

LETTER_BUCKETS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# SKIP_FOLDERS includes letter buckets, the symbol bucket, and top-level special folders.
# The pipeline descends into letter buckets rather than treating them as genre contexts.
# TOP_LEVEL_SPECIAL folders are never renamed, never moved, never have genre injected from path.
SKIP_FOLDERS = TOP_LEVEL_SPECIAL | LETTER_BUCKETS | {SYMBOL_BUCKET}

# Special folders — tag only, never move or rename files (kept for backward compatibility)
SPECIAL_FOLDERS = TOP_LEVEL_SPECIAL

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

# ── Phase 4: user preferences ────────────────────────────────────────────────
# Optional genre remapping applied before folder routing and final genre writes.
# Example: {"IDM": "Electronic"}
GENRE_REMAP_RULES: dict[str, str] = {}

# Scoped preference overrides.
# Precedence: artist > genre > global.
#
# Supported settings:
# - artist_folder_threshold: positive integer
# - preference_labels: list[str]
PREFERENCE_OVERRIDES: dict[str, dict] = {
    "global": {
        # "artist_folder_threshold": 3,
        # "preference_labels": ["study"],
    },
    "genre": {
        # "electronic": {"artist_folder_threshold": 4},
        # "jazz": {"preference_labels": ["study"]},
    },
    "artist": {
        # "aphex twin": {"artist_folder_threshold": 6, "preference_labels": ["love"]},
    },
}

# Sidecar store for preference labels (does not affect folder contract).
PREFERENCE_LABELS_PATH = Path("preference_labels.json")

# ── Phase 4: move hooks ──────────────────────────────────────────────────────
# Hooks are optional callbacks for move operations. Commands must be a list,
# e.g. ["hooks/on_move.py", "--mode", "audit"].
MOVE_HOOKS: dict[str, list[dict]] = {
    "pre_move": [],
    "post_move": [],
}

# Security boundary for hook executables.
HOOKS_ROOT = Path(__file__).resolve().parent / "hooks"
HOOK_ALLOWED_PATHS = [HOOKS_ROOT]

# Hook execution policy:
# - continue: log warning and continue
# - review: log + append review.json item with reason "hook_failure"
# - abort: raise error and stop processing
HOOK_FAILURE_POLICY = "continue"
HOOK_TIMEOUT_SECONDS = 5.0
HOOKS_RUN_IN_DRY_RUN = False

# Albums with more distinct artists than this are treated as compilations.
# Albums with 2+ artists at or below this are multi-artist albums.
COMPILATION_ARTIST_THRESHOLD = 3

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

# ── Phase 5: audio analysis ──────────────────────────────────────────────────
# Pickled pandas DataFrame written by 06_analyze.py.
FEATURES_STORE_PATH = Path("features.pkl")

MB_RATE_LIMIT_SECONDS = 1.1
ACOUSTID_MIN_SCORE    = 0.8
MB_MIN_TAG_VOTES      = 2
MB_MAX_GENRES         = 5
MB_MAX_RETRIES        = 3
MB_BACKOFF_BASE       = 2.0
MB_ADAPTIVE_MAX_DELAY = 8.0
MB_MAX_QUEUE_SECONDS  = 20.0
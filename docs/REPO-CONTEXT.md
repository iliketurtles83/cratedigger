# Copilot Instructions — cratedigger

## Project Purpose

A personal music collection management pipeline that:
1. Tags audio files with genre and metadata (artist, album, year, BPM, track)
2. Normalises filenames and folder names
3. Routes incoming music to the right place automatically
4. Builds toward a personal music recommender model ("poor man's Spotify")

All scripts are safe, idempotent, and support `--dry-run` before touching files.

---

## Repository Structure

```
cratedigger/
  config.py                  ← user settings: music root, folder lists, preferences
  config.example.py          ← example config with placeholder values, committed for reference
  .env                       ← API keys (never committed, in .gitignore)
  .env.example               ← example .env with variable names but no keys, committed for reference
  .gitignore                 ← covers: .env, config.py, *.log, *.json, __pycache__, .venv
  lib/
    context.py               ← FolderContext dataclass, get_folder_context(), classify_folder(), infer_compilation_folder()
    genres.py                ← genre normalisation, merge logic, blacklist, parent lookup
    parsers.py               ← filename and folder name parsing, sanitisation
    tags.py                  ← mutagen read/write wrappers for all formats
    mb.py                    ← MusicBrainz / AcoustID lookup with rate limiting
    logger.py                ← shared logging setup
  01_tag.py                  ← tag missing fields using MB and folder context, flag unresolvable cases
  02_rename.py               ← rename song files using tags as source of truth
  03_folders.py              ← rename folder names, classify folder types
  04_move.py                 ← intake routing, restructure, promote
  05_review.py               ← interactive resolution of files flagged in review log
  06_analyze.py              ← audio feature extraction for recommender (future)
  intake.py                  ← orchestrates 01→02→04 for files in INCOMING_FOLDER
  genre-tree.txt             ← AllMusic genre taxonomy (reference, never modified)
```

---

## Configuration — config.py

```python
from pathlib import Path

MUSIC_ROOT = Path("/path/to/audio")

# ── Intake / staging ──────────────────────────────────────────────────────────
INCOMING_FOLDER      = MUSIC_ROOT / "incoming_foler"   # drop zone
STAGED_TRACKS_FOLDER = MUSIC_ROOT / "new_songs"       # loose files by genre, awaiting decision
STAGED_ALBUMS_FOLDER = MUSIC_ROOT / "new_albums"# albums by genre, review before promoting

# ── Genre folders ─────────────────────────────────────────────────────────────
# Single source of truth. GENRE_FOLDERS derived from this.
FOLDER_TO_GENRE = {
    "50s": "50s", "60s": "60s", "70s": "70s", "80s": "80s",
    "african": "African", "blues": "Blues", "brazil": "Brazil",
    "classical": "Classical", "electronic": "Electronic". "folk": "Folk", "funk": "Funk",
    "hip-hop": "Hip-Hop", "indie": "Indie", "industrial": "Industrial",
    "japan": "Japan", "jazz": "Jazz", "latin": "Latin", "metal": "Metal",
    "meditation": "Meditation",
    "pop": "Pop", "post-punk": "Post-Punk", "post-rock": "Post-Rock",
    "punk": "Punk", "reggae": "Reggae", "rnb": "R&B", "rock": "Rock",
    "soundtrack": "Soundtrack", "spoken": "Spoken Word",
}
GENRE_FOLDERS = set(FOLDER_TO_GENRE.keys())  # derived — never edit

SUBGENRE_BUCKETS = {
    "alt-rock": "Alternative Rock", "blues-rock": "Blues Rock",
    "noise-rock": "Noise Rock", "post-hardcore": "Post-Hardcore",
}

SPECIAL_FOLDERS = {
    "favorites", "mix_from_john", "random",
    "new_songs", "new_albums", "compilations", "mixes"
}

SKIP_FOLDERS = {"videos"}

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

# Folders where albumartist tag is relevant
ALBUMARTIST_FOLDERS = {"compilations", "various", "soundtrack"}

# Folders where BPM makes no sense
NO_BPM_FOLDERS = {"spoken", "mixes"}

# Placeholder values that indicate bad/missing data — trigger MB lookup
SUSPICIOUS_TAG_VALUES = {
    "unknown artist", "unknown", "track", "untitled",
    "artist", "album artist", "no artist", "various artists",
}

ARTIST_FOLDER_THRESHOLD = 3

COMPILATION_ARTIST_THRESHOLD = 3

MB_RATE_LIMIT_SECONDS = 1.1
ACOUSTID_MIN_SCORE    = 0.8
MB_MIN_TAG_VOTES      = 2
MB_MAX_GENRES         = 5
```

---

## lib/context.py — Folder Context (Single Source of Truth)

All path-based decisions across the entire pipeline flow from this module.
No script should re-implement folder classification or genre resolution.

In order to tag and rename files accurately, context needs to be established. 
Before tagging a file, the system needs to know what type of folder it is in and what else is there.
Known cases of folders are:
- subgenre folder (e.g. rock/0-alt-rock/)
- artist folder with albums (e.g. rock/Radiohead/OK Computer/)
- artist folder with albums and single songs (e.g. rock/Radiohead/OK Computer/, rock/Radiohead/Radiohead - The National Anthem.mp3)
- artist folder with only single songs (e.g. rock/Radiohead/Radiohead - Paranoid Android.mp3)
- special folder with single songs (e.g. rock/favorites/Radiohead - Karma Police.mp3)

Types of albums:
- regular album (e.g. rock/Radiohead/OK Computer/)
- compilation (e.g. rock/0compilations/Best of 90s/) — COMPILATION_ARTIST_THRESHOLD+ distinct artists
- soundtrack (e.g. soundtrack/0various/Inception/) 
- multi-artist album — 2+ real artists (each ≥ 2 tracks) but below COMPILATION_ARTIST_THRESHOLD. Per-track artist tags preserved, no albumartist set.


```python
from dataclasses import dataclass
from pathlib import Path

@dataclass
class FolderContext:
    top: str                  # raw top-level folder name e.g. "rock"
    genre: str | None         # display genre e.g. "Rock", or None
    subgenre: str | None      # e.g. "Alt-Rock" if in subgenre bucket, else None
    folder_kind: str          # see Folder Classifier section
    is_genre_folder: bool     # top is in GENRE_FOLDERS
    is_special_folder: bool   # top is in SPECIAL_FOLDERS
    is_compilation: bool      # albumartist tag needed
    is_soundtrack: bool       # albumartist tag needed
    needs_bpm: bool           # not in NO_BPM_FOLDERS
    skip: bool                # top is in SKIP_FOLDERS
    depth: int                # levels below music root

def get_folder_context(path: Path) -> FolderContext:
    """
    Build complete folder context for any audio file path.
    Called once per file in tag_file(), used for all tag decisions.
    Imports config — never hardcodes folder names.
    """
```

### Usage in all scripts
```python
from lib.context import get_folder_context, FolderContext

ctx = get_folder_context(path)

# 01_tag.py
folder_genre = ctx.genre
needs_bpm = ctx.needs_bpm and not existing.get("bpm")
needs_albumartist = (ctx.is_compilation or ctx.is_soundtrack)

# 03_folders.py
if ctx.folder_kind == "artist_flat": ...

# 04_move.py
if ctx.is_special_folder: # never move
```

### classify_folder() lives in lib/context.py
Previously in 03_folders.py — moved here so all scripts share it.
03_folders.py and 04_move.py import from lib.context, not from each other.

### infer_compilation_folder() lives in lib/context.py
Used by 01_tag.py (consistency pass) and 03_folders.py (folder naming).
Returns True when a folder should be treated as a compilation.
Detection order:
1. `ctx.is_compilation` or `ctx.is_soundtrack` → immediate True
2. albumartist tag in compilation set ("various artists", "va", etc.) → True
3. Single albumartist that doesn't match any track artist → True
4. Mixed album/year tags → False for `artist_flat` folders (genuine mixed content), but tolerated for `album` folders (likely sloppy metadata)
5. COMPILATION_ARTIST_THRESHOLD+ distinct artists with no dominant share (≤50%) → True

```python
def infer_compilation_folder(
    folder: Path,
    *,
    file_tag_dicts: list[dict[str, str | None]] | None = None,
) -> bool: ...
```
Accepts pre-read `file_tag_dicts` to avoid re-reading tags when caller already has them.

```python
def classify_folder(folder: Path) -> str:
    """
    Returns: album | artist | artist_mixed | artist_flat |
             subgenre | local_special | disc | unknown
    """
    has_audio = any audio files directly inside folder
    has_album_subdirs = any subdirs classifying as album or artist
    starts_with_0 = folder.name.startswith("0")
    has_separator = " - " in folder.name

    if is_disc_subfolder(name): return "disc"
    if starts_with_0 and name in SUBGENRE_BUCKETS: return "subgenre"
    if starts_with_0: return "local_special"
    if has_separator: return "album"
    if has_album_subdirs and not has_audio: return "artist"
    if has_album_subdirs and has_audio: return "artist_mixed"
    if not has_album_subdirs and has_audio: return "artist_flat"
    return "unknown"

def is_disc_subfolder(name: str) -> bool:
    return bool(re.match(r"^(cd|disc|disk)\s*\d+$", name, re.IGNORECASE))
```

---

## Pipeline Architecture

**Tags own content. Context owns structure.**

- Tags answer: *what is this file?* — artist, title, track, disc, album, year
- Context answers: *how should this file be treated?* — naming pattern, disc prefix mode, genre scope, folder classification

These are independent concerns. Each script needs both, but for different reasons.
Correctness flows forward (bad tags → bad renames → bad folders), but each step's
logic is independently grounded — it has a primary input from the previous stage plus
its own structural awareness via context.

```
01_tag.py       fingerprint → MusicBrainz → folder context → write tags
      ↓
02_rename.py    normalise audio filenames using tags as source of truth
      ↓
03_folders.py   classify folders, normalise names, flag artist_flat
      ↓
04_move.py      intake routing, restructure, promote
      ↓
05_review.py    interactive resolution of flagged files and folders
      ↓
06_analyze.py   audio feature extraction for recommender (future)

intake.py       orchestrates 01→02→04 for files arriving in INCOMING_FOLDER
```

### Per-script inputs
```
01_tag.py  →  existing tags + AcoustID + context (genre, BPM gate, albumartist eligibility)
02_rename  →  tags (source of truth) + local sibling scan (disc prefix decision)
03_folders →  tags inside folder (content) + classify_folder() (disk structure)
04_move    →  tags (genre routing) + context (folder kind, special folder gate)
```

02_rename.py does NOT call `get_folder_context()` — it only needs `is_disc_subfolder()`
plus its own `_build_disc_context()` sibling scan for disc prefix decisions.
03_folders.py calls `classify_folder()` directly at folder level, not per file.

### CLI flags all scripts must support
- `--dry-run`       show changes, write nothing (safe default)
- `--no-dry-run`    actually write changes
- `--folder NAME`   process one top-level folder only
- `--log FILE`      log file path (default: review.log)

### 01_tag.py flags
- `--overwrite`         overwrite all existing tags from MB (dangerous, nuclear)
- `--fix-suspicious`    replace only placeholder/bad values (safer alternative)
- `--no-bpm`            skip BPM detection
- `--no-mb`             skip MusicBrainz/AcoustID entirely

### 04_move.py modes (mutually exclusive)
- `--intake`        route files from INCOMING_FOLDER to staging folders
- `--restructure`   apply N=3 threshold to existing genre folder structure
- `--promote`       move staged albums from STAGED_ALBUMS_FOLDER to genre folders

---

## 01_tag.py — Preconditions and Postconditions

### Preconditions
- Audio file exists and is readable
- File format supported: mp3 / flac / ogg / m4a / aac / opus
- Folder context available via get_folder_context()
- 01_tag.py must run BEFORE 03_folders.py on any folder

### Postconditions
Every processed file should have — if resolvable:
- `genre` — folder + existing + MB merged
- `artist` — from MB or filename parse
- `title` — from MB or filename parse
- `track` — from MB or filename parse
- `album` — from MB, folder name, or filename parse
- `year` — from MB or folder name
- `bpm` — from librosa (except NO_BPM_FOLDERS)
- `albumartist` — only in compilation/soundtrack context, if MB provides it
- `isrc` — if MB provides it

Existing non-empty fields never overwritten unless `--overwrite` or
`--fix-suspicious` passed. Unresolvable fields flagged in review.json.

---

## 01_tag.py — Critical Logic Rules

### API gate — needs_mb
```python
ctx = get_folder_context(path)

def is_suspicious(value: str | None) -> bool:
    return (value or "").strip().lower() in config.SUSPICIOUS_TAG_VALUES

needs_mb_for_identity = (
    overwrite or
    (fix_suspicious and is_suspicious(existing.get("artist"))) or
    (fix_suspicious and is_suspicious(existing.get("title"))) or
    not existing.get("artist") or
    not existing.get("title")
)

needs_mb_for_year = (
    not effective_existing.get("year") and
    not parse_folder_name(path.parent.name).get("year")
)

needs_mb_for_genre = not has_meaningful_genres(existing.get("genre"))

needs_mb = (
    needs_mb_for_identity or
    needs_mb_for_year or
    needs_mb_for_genre
)
```

Key rules:
- `--overwrite` forces needs_mb True — without this, overwrite does nothing on tagged files
- Suspicious values only trigger MB if `--fix-suspicious` is also passed; otherwise only missing fields trigger identity lookup
- Year triggers MB only if missing from both `effective_existing` AND folder name
- Genre triggers MB if existing genre tag has no meaningful (non-blacklisted) value

### Fill missing fields — always check existing AND new_tags
```python
# CORRECT
if not existing.get("album") and not new_tags.get("album"):
    if mb_meta.get("album"):
        new_tags["album"] = mb_meta["album"]

# WRONG — overwrites good existing data
if not new_tags.get("album"):
    new_tags["album"] = mb_meta.get("album") or existing.get("album")
```

### --fix-suspicious flag
Replaces fields whose current value is in SUSPICIOUS_TAG_VALUES.
Safer than --overwrite which replaces everything unconditionally.

Implemented via `effective_existing` — a modified copy of `existing` with suspicious
values cleared to None. All fill logic uses `effective_existing` instead of `existing`.
```python
effective_existing = dict(existing)
if fix_suspicious:
    for field in ("artist", "title", "album", "year", "track"):
        if is_suspicious(existing.get(field)):
            effective_existing[field] = None

# Downstream fill logic then guards against effective_existing, not existing:
if not effective_existing.get("title") and not new_tags.get("title"):
    ...
```

### Genre merge — always include existing
```python
existing_genres = [
    g.strip()
    for g in (existing.get("genre") or "").split("/")
    if g.strip()
]
normalised_mb = [normalise_genre(g) for g in mb_genre_list]
genre_str = merge_genres(ctx.genre, existing_genres + normalised_mb)

if genre_str and genre_str != existing.get("genre"):
    new_tags["genre"] = genre_str
```

### AlbumArtist — compilation/soundtrack context only
```python
if (ctx.is_compilation or ctx.is_soundtrack) and not existing.get("albumartist"):
    if mb_meta.get("albumartist"):
        new_tags["albumartist"] = mb_meta["albumartist"]
```
Never write albumartist for regular artist albums.

### ISRC — if MB provides it
```python
if not existing.get("isrc") and mb_meta.get("isrc"):
    new_tags["isrc"] = mb_meta["isrc"]
```

### BPM — lazy import, skip for non-musical folders
```python
_librosa = None

def detect_bpm(path: Path) -> int | None:
    global _librosa
    if _librosa is None:
        try:
            import librosa as lib
            _librosa = lib
        except ImportError:
            _librosa = False
    if not _librosa:
        return None
    ...

# Gate in tag_file():
if not no_bpm and not existing.get("bpm") and ctx.needs_bpm:
    bpm = detect_bpm(path)
```

### No grouping tag
No GROUPING_FOLDERS, no grouping tag logic anywhere in pipeline.

---

## Tag Fields Reference

| Field       | ID3 (MP3) | Vorbis (FLAC/OGG) | M4A   | Notes |
|-------------|-----------|-------------------|-------|-------|
| Title       | TIT2      | title             | ©nam  | |
| Artist      | TPE1      | artist            | ©ART  | |
| AlbumArtist | TPE2      | albumartist       | aART  | compilation/soundtrack only |
| Album       | TALB      | album             | ©alb  | |
| Year        | TDRC      | date              | ©day  | |
| Genre       | TCON      | genre             | ©gen  | slash-separated |
| BPM         | TBPM      | bpm               | —     | integer, M4A unsupported |
| Track       | TRCK      | tracknumber       | trkn  | preserve x/total format |
| Disc        | TPOS      | discnumber        | disk  | multi-disc albums |
| ISRC        | TSRC      | isrc              | —     | deduplication anchor |

No grouping tag. All fields must be in tags.py field maps.

---

## Genre System

### Tag format
```
Electronic / Downtempo
Jazz / Soul
Rock / Alt-Rock / Shoegaze
```
Main genre always first. Never flatten to single genre.

### Merge logic (lib/genres.py merge_genres())
1. ctx.genre (folder-derived) → first
2. Existing genre tags → second (never lost)
3. MB genres → appended, blacklisted discarded
4. Deduplicate case-insensitively, join with ` / `

### Genre source by folder type
| Folder | Source |
|---|---|
| Genre folder | FOLDER_TO_GENRE[top] |
| Genre / subgenre bucket | FOLDER_TO_GENRE[top] / SUBGENRE_BUCKETS[level2] |
| 0random / 0random_good | FOLDER_TO_GENRE[subfolder] |
| 0faves, 0shacks, 0mixes | MB fingerprint only |
| 0compilations, 0various | Per-track MB only |

### FOLDER_TO_GENRE values are display names
```
"spoken":  "Spoken Word"    "rnb": "R&B"    "hip-hop": "Hip-Hop"
```

### Genre blacklist
```python
GENRE_BLACKLIST = {
    "other", "unknown", "miscellaneous", "seen live", "favorites",
    "favourite", "good",
}
```

---

## 03_folders.py — Responsibilities

**Does:**
- Use classify_folder() / infer_compilation_folder() / is_disc_subfolder() from lib/context.py
- Fix `, The` / `, A` suffix
- Add missing year — read from file tags first, folder name second
- Build folder names from tags as primary source (01_tag.py must run first)
- Move loose files from artist_mixed → `0singles/` subfolder
- Flag artist_flat, unknown, conflict cases for review

**Does NOT:**
- Own classify_folder() — import from lib/context.py
- Move files between genre folders
- Apply N=3 rule
- Rename genre roots, SPECIAL_FOLDERS, subgenre buckets, disc subfolders

### Folder name source priority
```
1. File tags inside the folder  ← most reliable — 01_tag.py must run first
2. Existing folder name         ← fallback if tags incomplete
3. Flag for review              ← if neither gives enough info
```

### artist_flat handling
When classify_folder() returns "artist_flat":
- Read artist, album, year from file tags inside
- Build canonical: Artist - Album (Year)/
- If files have mixed album tags → flag for review, do not rename
- If tags incomplete → flag for review, do not guess
- Example: AGO/ with tagged files → AGO - Cronenberg on Warhol (2006)/

### Compilation folder naming
When `infer_compilation_folder()` returns True, use Album-only format:
```
Album Title (Year)/          # no artist prefix — compilations and soundtracks
```
Regular albums: `Artist - Album (Year)/`
Never prepend artist name to a compilation or soundtrack folder.

### Pipeline dependency
03_folders.py depends on 01_tag.py having already run on the same folder.
Never run on untagged files.

### Embedded year pattern
```python
_EMBEDDED_YEAR = re.compile(r"[\(\[](\d{4})[\)\]]")  # handles (1960) and [1960]
```

### Disc subfolder detection (from lib/context.py)
```python
is_disc_subfolder(name)  # import from lib.context
```

---

## 04_move.py — Three Modes

### --intake mode
```python
is_album_track = path.parent != config.INCOMING_FOLDER
is_loose       = path.parent == config.INCOMING_FOLDER
```
Albums → STAGED_ALBUMS_FOLDER / genre_subfolder / Artist - Album (Year)/
Loose  → STAGED_TRACKS_FOLDER / genre_subfolder / filename

Never move directly to genre folders. Always stage first.

### --promote mode
STAGED_ALBUMS_FOLDER / genre / Album/ → genre / Album/
Remove empty genre subfolders from staging after move.

### --restructure mode
Apply N=3 threshold. See Artist Folder Threshold Rule.

### Artist Folder Threshold Rule
```
artist_flat, 0 albums, all files same album tag → flatten to genre root, delete artist folder
artist_flat, 0 albums, mixed album tags         → flag for review
artist_flat, 1-2 albums (below threshold)       → create album in genre root, delete if empty
artist_flat, 3+ albums (at/above threshold)     → create album inside artist folder
artist_mixed                                    → loose files → 0singles/, albums normal
```
Never delete folder without verifying empty first.

---

## 06_analyze.py — Audio Feature Extraction (Future)

Separate from tagging. Analysis outputs belong in feature store, not audio tags.
Allows re-running with better models without touching tags.

| Feature | Source | Storage |
|---|---|---|
| BPM | librosa | audio tag (already in 01_tag.py) |
| Key | librosa | audio tag (conventional to embed) |
| Energy / loudness | librosa | features.json or sqlite |
| Spectral centroid | librosa | features.json or sqlite |
| Zero crossing rate | librosa | features.json or sqlite |
| Danceability | librosa | features.json or sqlite |

Run after 01_tag.py — needs ISRC in tags for cross-referencing.
Embed in tags: BPM (done), Key. Store externally: everything else.

---

## Intake Flow

```bash
python3 01_tag.py --no-dry-run --folder incoming
python3 02_rename.py --no-dry-run --folder incoming
python3 04_move.py --intake --no-dry-run
# review 0new_albums/, then:
python3 04_move.py --promote --no-dry-run
# review 0new/ loose tracks manually
```

Or: `python3 intake.py --no-dry-run`

---

## Filename Conventions

### Input (parser handles all)
```
1-01 - Artist - Song Title.mp3
Artist - 2-03 - Song Title.mp3
1-04-Song Title.mp3
1-01 Song Title.mp3
01 - Artist - Song Title.mp3
Artist - 01 - Song Title.mp3
Artist - Song Title.mp3
08.Song Title.mp3
04-Song Title.mp3
01 Song Title.mp3
```

### Output target
```
01 - Artist Name - Song Title.mp3           # single-disc or no disc info
1-01 - Artist Name - Song Title.mp3         # multi-disc (disc-track prefix)
```

### Folder target
```
Artist Name - Album Title (Year)/
Artist Name/Artist Name - Album Title (Year)/
```

---

## lib/parsers.py

### Patterns
```python
# Disc-track patterns (must come before track-only patterns)
r"^(?P<disc>\d{1,2})-(?P<track>\d{2,3})\s*-\s*(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
r"^(?P<artist>.+?)\s*-\s*(?P<disc>\d{1,2})-(?P<track>\d{2,3})\s*-\s*(?P<title>.+)$"
r"^(?P<disc>\d{1,2})-(?P<track>\d{2,3})-(?P<title>.+)$"
r"^(?P<disc>\d{1,2})-(?P<track>\d{2,3})\s+(?P<title>.+)$"
# Track-only patterns
r"^(?P<track>\d{1,3})\s*-\s*(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
r"^(?P<artist>.+?)\s*-\s*(?P<track>\d{1,3})\s*-\s*(?P<title>.+)$"
r"^(?P<track>\d{1,3})-(?P<title>.+)$"                              # 04-Song Title (no spaces)
r"^(?P<track>\d{1,3})\.\s*(?P<artist>.+?)\s*-\s*(?P<title>.+)$"  # 13. Artist - Title
r"^(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
r"^(?P<track>\d{1,3})\.(?P<title>.+)$"
r"^(?P<track>\d{1,3})\s+(?P<title>.+)$"
```

### Ripper annotations
Strip: `[-]`, `[*]`, `[#]`, `[DM]`, `[DDR]`, `[NoFS]`, `[raw]`,
`[ChattChitto RG]`, `[www.anything.com]`, `[plixid.com]`, empty `[]`

Keep: `[Live]`, `[Instrumental]`, `[Remix]`, `[Bonus Tracks]`, `[EP]`,
`[Disc 1]`, `[Disc 2]`, `[deluxe edition]`, `[clean]`, `[2007]`

Never auto-capitalise. Preserve case exactly as in tags.

---

## MusicBrainz / AcoustID

- Always fingerprint — never tag-based lookup
- Score threshold: ACOUSTID_MIN_SCORE (0.8)
- Rate limit: MB_RATE_LIMIT_SECONDS (1.1s)
- Fill missing or suspicious fields only — never overwrite without flag
- set_useragent() reads from .env — never hardcoded
- Degrade gracefully if pyacoustid not installed

---

## Compilation Placement

Three valid locations — leave where they are, tag correctly:
- `0compilations/` — genre-spanning
- `0various/` — various artists
- Inside genre folder — single-genre compilations

---

## Testing Protocol

1. Copy folder → `foldername_test/`
2. Add `"foldername_test": "Genre"` to FOLDER_TO_GENRE temporarily
3. `--dry-run`, review output
4. `--no-dry-run`, verify in Strawberry
5. Remove test folder and config entry

Test folders not in config always trigger MB.

---

## Recommender Model (Future)

| Signal | Source |
|---|---|
| Genre (multi-value) | Tags — never flatten |
| BPM | librosa tag |
| Key | 06_analyze.py |
| Year | Tags |
| Artist | Tags — collaborative filtering |
| AlbumArtist | Tags — compilation signal |
| ISRC | Tags — deduplication anchor |
| Energy, spectral | 06_analyze.py → feature store |
| 0faves / 0faves_alltime | Positive preference labels |
| 0random_good vs 0random | Implicit promotion signal |

---

## Secrets and Security

- API keys in .env only — python-dotenv
- .env and config.py never committed
- config.example.py committed — fully depersonalised
- No personal paths or folder names in committed files

---

## Coding Conventions

- pathlib.Path always — never os.path
- --dry-run default, all writes gated
- Idempotent — safe to re-run
- Never move/rename from SPECIAL_FOLDERS
- Never process SKIP_FOLDERS
- review.log + review.json for all failures
- Log: YYYY-MM-DD HH:MM:SS LEVEL message
- Import: stdlib → third-party → local (config, then lib/)
- .venv always
- GENRE_FOLDERS = set(FOLDER_TO_GENRE.keys()) — derived, never edited
- librosa lazy import only
- get_folder_context() called once per file, result passed through — never re-computed
- modularity - lib/ for shared logic, /lib modules do what the name says, main scripts orchestrate flow

---

## What Copilot Should Never Do

- Use os.path — always pathlib.Path
- Hardcode paths, folder names, genre lists outside config.py
- Hardcode API keys — always .env
- Overwrite existing tags without --overwrite or --fix-suspicious
- Check only new_tags for fallback — must check existing too
- Use existing.get(field) as fallback value in assignment
- Move/rename from SPECIAL_FOLDERS
- Use tag-based MB lookup — always fingerprint
- Flatten multi-genre to single genre
- Put subgenre before parent in tag string
- Skip --dry-run check in any write operation
- Process 0videos/
- Apply .title() or auto-capitalisation
- Strip square brackets without checking keep/strip rules
- Remove the needs_mb gate
- Add grouping tag logic
- Edit GENRE_FOLDERS directly
- Rename genre roots, SPECIAL_FOLDERS, subgenre buckets, disc subfolders
- Omit --no-bpm, --no-mb, --fix-suspicious from 01_tag.py
- Import librosa at module level — lazy import only
- Apply N=3 logic in 03_folders.py — belongs in 04_move.py --restructure
- Move loose files from artist_mixed anywhere other than 0singles/
- Auto-decide artist_flat with mixed album tags — always flag for review
- Delete folders without verifying empty first
- Move files directly to genre folders during --intake — always stage first
- Bypass STAGED_ALBUMS_FOLDER — albums never go direct from intake to genre folders
- Mix --intake, --restructure, --promote — mutually exclusive
- Implement classify_folder() in 03_folders.py or 04_move.py — belongs in lib/context.py
- Implement resolve_folder_genre() in 01_tag.py — belongs in lib/context.py
- Implement infer_compilation_folder() outside lib/context.py — it belongs there
- Apply artist prefix to compilation/soundtrack folder names — use Album (Year)/ format
- Re-compute folder context multiple times per file — call get_folder_context() once
- Write albumartist tag for regular artist albums — compilation/soundtrack context only
- Embed spectral/energy features in audio tags — store in features.json or sqlite
- Implement 06_analyze.py logic inside 01_tag.py — keep analysis separate

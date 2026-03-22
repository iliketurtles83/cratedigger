# Copilot Instructions — cratedigger

## Project Purpose

A personal music collection management pipeline that:
1. Tags audio files with genre and metadata (artist, album, year, BPM, track)
2. Normalises filenames and folder names
3. Routes incoming music to the right place automatically

All scripts are safe, idempotent, and support `--dry-run` before touching files.

---

## Repository Structure

```
cratedigger/
  config.py                  ← user settings: music root, folder lists, preferences
  config.example.py          ← committed template, fully depersonalised
  .env                       ← API keys (never committed, in .gitignore)
  .env.example               ← committed template showing required keys (values empty)
  .gitignore                 ← covers: .env, config.py, *.log, *.json, __pycache__, .venv
  lib/
    genres.py                ← genre normalisation, merge logic, blacklist, parent lookup
    parsers.py               ← filename and folder name parsing, sanitisation
    tags.py                  ← mutagen read/write wrappers for all formats
    mb.py                    ← MusicBrainz / AcoustID lookup with rate limiting
    logger.py                ← shared logging setup
  01_tag.py                  ← fingerprint → MusicBrainz → folder genre → write tags
  02_rename.py               ← normalise audio filenames using tags as source of truth
  03_folders.py              ← normalise folder names, classify folder types
  04_move.py                 ← structural decisions: flatten, nest, route new files
  05_review.py               ← interactive resolution of files flagged in review log
  intake.py                  ← orchestrates 01→02→03→04 for new files in 0new/
  genre-tree.txt             ← AllMusic genre taxonomy (reference, never modified)
```

---

## Configuration — config.py

All user-specific settings live here. Imported by all scripts and lib modules.
Never hardcode paths, folder names, genre lists, or preferences outside this file.

```python
from pathlib import Path

MUSIC_ROOT = Path("/path/to/audio")

# Single source of truth: folder name → display genre.
# GENRE_FOLDERS is derived from this — never edit GENRE_FOLDERS directly.
FOLDER_TO_GENRE = {
    "50s": "50s", "60s": "60s", "70s": "70s", "80s": "80s",
    "african": "African", "blues": "Blues", "brazil": "Brazil",
    "classical": "Classical", "electronic": "Electronic",
    "estonian": "Estonian", "folk": "Folk", "funk": "Funk",
    "hip-hop": "Hip-Hop", "indie": "Indie", "industrial": "Industrial",
    "japan": "Japan", "jazz": "Jazz", "latin": "Latin", "metal": "Metal",
    "meditation": "Meditation",
    "pop": "Pop", "post-punk": "Post-Punk", "post-rock": "Post-Rock",
    "punk": "Punk", "reggae": "Reggae", "rnb": "R&B", "rock": "Rock",
    "soundtrack": "Soundtrack", "spoken": "Spoken Word",
}

# Derived automatically — never edit directly
GENRE_FOLDERS = set(FOLDER_TO_GENRE.keys())

# Subgenre bucket folders inside genre folders — folder name : display genre
# Start with 0, contain album subfolders. Add new ones as discovered.
SUBGENRE_BUCKETS = {
    "0alt-rock":      "Alt-Rock",
    "0blues-rock":    "Blues Rock",
    "0noise-rock":    "Noise Rock",
    "0post-hardcore": "Post-Hardcore",
}

# Special folders — tag only, never move or rename files
SPECIAL_FOLDERS = {
    "0faves", "0bestof", "0random", "0random_good",
    "0new", "0shacks", "0compilations", "0various", "0mixes",
}

SKIP_FOLDERS = {"0videos"}

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}

# Folders where BPM detection makes no sense
NO_BPM_FOLDERS = {"spoken", "0mixes"}

# Artist folder threshold — artists with this many or more albums get their
# own artist subfolder. Below this they live flat in the genre root.
ARTIST_FOLDER_THRESHOLD = 3

# MusicBrainz / AcoustID
MB_RATE_LIMIT_SECONDS = 1.1
ACOUSTID_MIN_SCORE    = 0.8
MB_MIN_TAG_VOTES      = 2
MB_MAX_GENRES         = 5
```

### .env (never committed)
```
ACOUSTID_API_KEY=your_key_here
MB_USER_AGENT_EMAIL=your@email.com
```

### Loading secrets
```python
from dotenv import load_dotenv
import os
load_dotenv()
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY", "")
```

---

## Pipeline Architecture

```
01_tag.py       fingerprint → MusicBrainz genres + folder genre → write tags
      ↓
02_rename.py    normalise audio filenames using tags as source of truth
      ↓
03_folders.py   classify folders, normalise names, move loose files to 0singles
      ↓
04_move.py      structural decisions: flatten/nest artist folders, route 0new files
      ↓
05_review.py    interactive resolution of flagged files and folders

intake.py       orchestrates 01→02→03→04 for files arriving in 0new/
```

### CLI flags all scripts must support
- `--dry-run`       show changes, write nothing (safe default)
- `--no-dry-run`    actually write changes
- `--folder NAME`   process one top-level folder only
- `--log FILE`      log file path (default: review.log)

### 01_tag.py additional flags
- `--overwrite`     overwrite existing artist/title/album/year (dangerous)
- `--no-bpm`        skip BPM detection (fast metadata-only pass)
- `--no-mb`         skip MusicBrainz/AcoustID lookup entirely

---

## Music Root — Full Folder Structure

```
audio/
  ├── rock/                              ← genre folder
  │   ├── 0alt-rock/                     ← subgenre bucket (starts with 0)
  │   │   └── Artist - Album (Year)/     ← album inside bucket
  │   │       ├── CD1/                   ← disc subfolder
  │   │       └── CD2/
  │   ├── 0compilations/                 ← local compilations
  │   ├── 0various/                      ← local various artists
  │   ├── AC-DC - Stiff Upper Lip (2000)/← flat album (few albums by artist)
  │   ├── Black Sabbath/                 ← artist folder (many albums)
  │   │   ├── Black Sabbath - Paranoid (1970)/
  │   │   ├── Black Sabbath - Heaven and Hell (1980)/
  │   │   └── 0singles/                  ← loose files from artist folder
  │   └── Allman Brothers Band, The - Best Of (1973)/
  ├── electronic/
  ├── jazz/
  ├── meditation/                        ← genre folder (not special)
  ├── [other genre folders...]
  ├── 0faves/          favourite songs ~last 5 years — tag only, never move
  ├── 0faves_alltime/  all-time faves — tag only, never move
  ├── 0random/         randomly collected, genre subfolders — tag only
  ├── 0random_good/    curated from 0random, genre subfolders — tag only
  ├── 0new/            intake staging
  │   ├── albums/
  │   └── singles/
  ├── 0shacks/         songs from a friend — tag only, never move
  ├── 0compilations/   genre-spanning compilations — tag only, never move
  ├── 0various/        various artists — tag only, never move
  ├── 0mixes/          DJ mixes — tag only, never move
  └── 0videos/         skip entirely
```

---

## Folder Classifier

`classify_folder()` in `03_folders.py` inspects each folder and returns one of:

| Classification | Description |
|---|---|
| `album` | `Artist - Album (Year)/` format, contains audio files |
| `artist` | Single name, contains album subfolders only |
| `artist_mixed` | Single name, contains albums AND loose audio files |
| `artist_flat` | Single name, contains only loose audio files (IS an album) |
| `subgenre` | Starts with 0, in SUBGENRE_BUCKETS |
| `local_special` | Starts with 0, not in SUBGENRE_BUCKETS |
| `disc` | CD1, CD2, Disc 1, Disc N etc. |
| `unknown` | Doesn't fit any pattern — flag for review |

### Classification logic
```python
has_audio = any audio files directly inside folder
has_album_subdirs = any subdirs that classify as album or artist
starts_with_0 = folder.name.startswith("0")
has_separator = " - " in folder.name

if is_disc_subfolder(name): return "disc"
if starts_with_0 and name in SUBGENRE_BUCKETS: return "subgenre"
if starts_with_0: return "local_special"
if has_separator:
    return "album"
if has_album_subdirs and not has_audio: return "artist"
if has_album_subdirs and has_audio: return "artist_mixed"
if not has_album_subdirs and has_audio: return "artist_flat"
return "unknown"
```

---

## 03_folders.py — Responsibilities

**Does:**
- Classify every folder using the classifier
- Fix `, The` / `, A` suffix on artist and album folder names
- Add missing year to album folder names (read from file tags inside)
- Move loose audio files from `artist_mixed` folders → `0singles/` subfolder
- Flag `artist_flat` with mixed album tags for review
- Flag `unknown` folders for review
- Flag rename conflicts for review

**Does NOT:**
- Move files between genre folders
- Create album subfolders or restructure artist folders (that's `04_move.py`)
- Rename genre root folders, SPECIAL_FOLDERS, subgenre buckets, disc subfolders
- Touch files in SPECIAL_FOLDERS or SKIP_FOLDERS

### The-suffix fix
```python
def _fix_the(name: str) -> str:
    if name.endswith(", The"):
        return "The " + name[:-5]
    if name.endswith(", A"):
        return "A " + name[:-3]
    return name
```
Apply to artist portion of any folder name.

### Year from file tags
```python
year = next(
    (read_tags(f).get("year", "")[:4]
     for f in sorted(folder.rglob("*"))
     if f.suffix.lower() in config.AUDIO_EXTENSIONS),
    None
)
```
Use `year[:4]` — handles full date formats like `1999-05-03`.

### Embedded year detection
Detect year in both round and square brackets:
```python
_EMBEDDED_YEAR = re.compile(r"[\(\[](\d{4})[\)\]]")
```
Strip from album text before building new folder name to avoid duplication.

### Disc subfolder detection
```python
_DISC_PATTERN = re.compile(r"^(cd|disc|disk)\s*\d+$", re.IGNORECASE)
```

### 0singles convention
When `artist_mixed` folder found — move loose audio files to `0singles/`
subfolder inside the artist folder. Never move files from album subfolders.
`0singles/` sorts to top with other `0` folders.

---

## 04_move.py — Responsibilities

**Does:**
- Apply N=3 threshold rule to `artist_flat` folders (from 03_folders review)
- Route new files from `0new/` to correct genre folder
- Restructure artist folders based on album count

**Does NOT:**
- Rename folders (that's `03_folders.py`)
- Write tags (that's `01_tag.py`)

### Artist Folder Threshold Rule (N=3)

Count existing album subfolders in the artist folder, then apply:

```
artist_flat, 0 existing albums, all files share same album tag:
  → flatten to genre root: Artist - Album (Year)/
  → delete empty artist folder

artist_flat, 0 existing albums, files have mixed album tags:
  → flag for review — cannot auto-decide

artist_flat, 1-2 existing albums (below threshold):
  → create Artist - Album (Year)/ in genre root
  → move files into it
  → if artist folder now empty → delete it

artist_flat, 3+ existing albums (at or above threshold):
  → create Artist - Album (Year)/ inside artist folder
  → move files into it
  → artist folder stays
```

Album count uses `ARTIST_FOLDER_THRESHOLD = 3` from config.
Always use `--dry-run` first — structural moves are hard to undo.

---

## resolve_folder_genre() — Path Walking Logic

Handle all folder depth levels:

```
Level 1 — genre folder (e.g. rock/)
  → base genre from FOLDER_TO_GENRE

Level 2 — may be:
  a) subgenre bucket (starts with 0, in SUBGENRE_BUCKETS)
       → genre = "Rock / Alt-Rock"
  b) local special (0compilations, 0various inside genre folder)
       → genre = base genre only
  c) artist folder (no dash, single name)
       → genre = base genre, go deeper
  d) album folder (contains " - ")
       → genre = base genre

Level 3 — may be:
  a) disc subfolder (CD1, CD2, Disc N)
       → genre already known
  b) album folder (if level 2 was artist folder)
       → genre = base genre

Level 4 — disc subfolder if level 2 was artist, level 3 was album
```

---

## Filename Conventions

### Input — parser handles all of these
```
01 - Artist Name - Song Title.mp3     ← standard target
Artist Name - 01 - Song Title.mp3     ← artist first
Artist Name - Song Title.mp3          ← no track number
08.Song Title.mp3                     ← dot-separated (ripper)
04-Song Title.mp3                     ← dash no spaces (ripper)
01 Song Title.mp3                     ← space-separated track
```

### Output target (02_rename.py)
```
01 - Artist Name - Song Title.mp3
```
Track zero-padded to 2 digits. Fields from tags, not filename.

### Folder naming target (03_folders.py)
```
Artist Name - Album Title (Year)/
Artist Name/Artist Name - Album Title (Year)/   ← two-level (≥3 albums)
```

---

## 01_tag.py — Critical Logic Rules

### API gate — only call MB when genuinely needed
```python
needs_mb = (
    not existing.get("artist") or
    not existing.get("title") or
    not resolve_folder_genre(path)
)
if needs_mb and not no_mb:
    # fingerprint_lookup...
```
Do NOT remove this gate. Test folders not in GENRE_FOLDERS always trigger
MB — add test folders to config temporarily to avoid this.

### Fill missing fields — always check existing first
```python
# CORRECT
if not existing.get("album") and not new_tags.get("album"):
    if folder_info.get("album"):
        new_tags["album"] = folder_info["album"]

# WRONG — overwrites good existing data
if not new_tags.get("album"):
    new_tags["album"] = folder_info.get("album") or existing.get("album")
```

### Genre merge — always include existing genre
```python
existing_genres = [
    g.strip()
    for g in (existing.get("genre") or "").split("/")
    if g.strip()
]
genre_str = merge_genres(folder_genre, existing_genres + normalised_mb)
```
A file tagged `Electronic` in `jazz/` → `Jazz / Electronic`, never just `Jazz`.

### Only write genre if it changed
```python
if genre_str and genre_str != existing.get("genre"):
    new_tags["genre"] = genre_str
```

### Track number — preserve existing format
Never overwrite `1/12` with `1`. Check `existing.get("track")` first.

### BPM — lazy import, skip for non-musical folders
```python
_librosa = None  # module-level sentinel

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
top = path.relative_to(config.MUSIC_ROOT).parts[0].lower()
if not no_bpm and not existing.get("bpm") and top not in config.NO_BPM_FOLDERS:
    bpm = detect_bpm(path)
```

### No grouping tag
No GROUPING_FOLDERS config, no grouping tag logic anywhere in pipeline.

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
1. Folder-derived genre → first
2. Existing genre tags → second (never lost)
3. MusicBrainz genres → appended, blacklisted discarded
4. Deduplicate case-insensitively
5. Join with ` / `

### Genre source by folder type
| Folder | Genre source |
|---|---|
| Genre folder | FOLDER_TO_GENRE[top] |
| Genre / subgenre bucket | FOLDER_TO_GENRE[top] / SUBGENRE_BUCKETS[level2] |
| 0random / 0random_good | FOLDER_TO_GENRE[subfolder] |
| 0faves, 0shacks, 0mixes | MB fingerprint only |
| 0compilations, 0various | Per-track MB only |

### FOLDER_TO_GENRE values are display names
```
"spoken":  "Spoken Word"    not "spoken"
"rnb":     "R&B"            not "rnb"
"hip-hop": "Hip-Hop"        not "hip-hop"
```

### Genre blacklist (lib/genres.py GENRE_BLACKLIST)
```python
GENRE_BLACKLIST = {
    "other", "unknown", "miscellaneous",
    "seen live", "favorites", "favourite", "good",
}
```

### Genre tree (genre-tree.txt)
Parsed into CHILD_TO_PARENT for resolving subgenres to root parents.
First genre in tag is always root-level, never a subgenre.

---

## lib/parsers.py — Critical Rules

### Filename patterns (most to least specific)
```python
r"^(?P<track>\d{1,3})\s*-\s*(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
r"^(?P<artist>.+?)\s*-\s*(?P<track>\d{1,3})\s*-\s*(?P<title>.+)$"
r"^(?P<artist>.+?)\s*-\s*(?P<title>.+)$"
r"^(?P<track>\d{1,3})\.(?P<title>.+)$"      ← dot-separated ripper
r"^(?P<track>\d{1,3})\s+(?P<title>.+)$"     ← space-separated
```

### Ripper annotations — strip vs keep
**Strip:** `[-]`, `[*]`, `[#]`, `[DM]`, `[DDR]`, `[NoFS]`, `[raw]`,
`[ChattChitto RG]`, `[www.anything.com]`, `[plixid.com]`, empty `[]`

**Keep:** `[Live]`, `[Instrumental]`, `[Remix]`, `[Bonus Tracks]`, `[EP]`,
`[Disc 1]`, `[Disc 2]`, `[deluxe edition]`, `[clean]`, `[2007]`

### _clean_track()
`"1/12"` → `"1"`. Apply in parse_filename() and build_filename().

### Capitalisation — never auto-apply
Never use .title() or any auto-casing. Flag for manual review only.

### Embedded year detection (03_folders.py)
```python
_EMBEDDED_YEAR = re.compile(r"[\(\[](\d{4})[\)\]]")
```
Handles both `(1960)` and `[1960]` in folder names.

---

## MusicBrainz / AcoustID

- Always fingerprint — never tag-based lookup
- Score threshold: ACOUSTID_MIN_SCORE (0.8)
- Rate limit: MB_RATE_LIMIT_SECONDS (1.1s)
- Fill missing fields only — never overwrite without --overwrite
- set_useragent() reads from .env — never hardcoded
- Degrade gracefully if pyacoustid not installed

---

## Tag Fields Reference

| Field | ID3 (MP3) | Vorbis (FLAC/OGG) | M4A | Notes |
|-------|-----------|-------------------|-----|-------|
| Title | TIT2 | title | ©nam | |
| Artist | TPE1 | artist | ©ART | |
| Album | TALB | album | ©alb | |
| Year | TDRC | date | ©day | |
| Genre | TCON | genre | ©gen | slash-separated |
| BPM | TBPM | bpm | — | integer |
| Track | TRCK | tracknumber | trkn | preserve x/total |

No grouping tag — removed from pipeline entirely.

---

## Compilation Placement

Three valid locations — leave where they are, tag correctly:
- `0compilations/` — genre-spanning
- `0various/` — various artists albums
- Inside genre folder — single-genre compilations

Do not auto-move. Flag ambiguous cases in review.json.

---

## Testing Protocol

1. Copy small genre folder → `foldername_test/`
2. Add `"foldername_test": "Genre"` to FOLDER_TO_GENRE in config.py
3. Run `--dry-run`, review output
4. Run `--no-dry-run`, verify in Strawberry
5. Remove test folder and config entry, run on real folder

Test folders without config entry always trigger MB — always add temporarily.

---

## Recommender Model (Future)

| Tag | Signal |
|---|---|
| Genre (multi-value) | Primary categorical — never flatten |
| BPM | Tempo similarity |
| Year | Era similarity |
| Artist | Collaborative filtering |
| 0faves membership | Positive preference label |
| 0faves_alltime membership | Strong positive preference label |
| 0random_good vs 0random | Implicit promotion signal |

---

## Secrets and Security

- API keys in .env only — python-dotenv
- .env never committed
- config.py never committed — contains local paths
- config.example.py committed — fully depersonalised
- No personal folder names or paths in any committed file

---

## Coding Conventions

- pathlib.Path always — never os.path
- --dry-run default, all writes gated — no exceptions
- Idempotent — safe to re-run
- Never move/rename files in SPECIAL_FOLDERS
- Never process SKIP_FOLDERS
- review.log + review.json for all failures and ambiguous cases
- Log format: YYYY-MM-DD HH:MM:SS LEVEL message
- Import order: stdlib → third-party → local (config, then lib/)
- Graceful degradation for optional deps
- .venv always — never system Python
- GENRE_FOLDERS = set(FOLDER_TO_GENRE.keys()) — derived, never edited directly
- librosa lazy import — never at module level

---

## What Copilot Should Never Do

- Use os.path — always pathlib.Path
- Hardcode paths, folder names, genre lists outside config.py
- Hardcode API keys — always .env
- Overwrite existing tags without --overwrite flag
- Check only new_tags for fallback — must check existing too
- Use existing.get(field) as fallback value in assignment
- Move/rename files from SPECIAL_FOLDERS
- Use tag-based MB lookup — always fingerprint
- Flatten multi-genre to single genre
- Put subgenre before parent in tag string
- Skip --dry-run check in any write operation
- Process 0videos/
- Apply .title() or auto-capitalisation
- Strip square brackets without checking keep/strip rules
- Remove the needs_mb API gate from 01_tag.py
- Add grouping tag logic — removed from pipeline
- Edit GENRE_FOLDERS directly — derived from FOLDER_TO_GENRE
- Rename genre root folders, SPECIAL_FOLDERS, subgenre buckets, disc subfolders
- Omit --no-bpm or --no-mb flags from 01_tag.py
- Import librosa at module level — must be lazy import
- Apply N-threshold logic in 03_folders.py — belongs in 04_move.py only
- Move loose files from artist_mixed to anywhere other than 0singles/
- Auto-decide artist_flat with mixed album tags — always flag for review
- Delete artist folders without verifying they are empty first

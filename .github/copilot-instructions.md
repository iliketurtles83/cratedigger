# Copilot Instructions — cratedigger

## Project Purpose

A personal music collection management pipeline that:
1. Tags audio files with genre and metadata (artist, album, year, BPM, track)
2. Normalises filenames and folder structure
3. Routes incoming music to the right place automatically
4. Builds toward a personal music recommender model ("poor man's Spotify")

All scripts are safe, idempotent, and support `--dry-run` before touching files.

---

## Repository Structure

```
cratedigger/
  config.py                  ← user settings: music root, folder lists, preferences
  config.example.py          ← committed template with placeholder values
  .env                       ← API keys (never committed, in .gitignore)
  .env.example               ← committed template showing required keys (values empty)
  .gitignore                 ← must cover: .env, config.py, *.log, *.json, __pycache__
  lib/
    genres.py                ← genre normalisation, merge logic, parent lookup from tree
    parsers.py               ← filename and folder name pattern parsing
    tags.py                  ← mutagen read/write wrappers for all formats
    mb.py                    ← MusicBrainz / AcoustID lookup with rate limiting
    logger.py                ← shared logging setup
  01_tag.py                  ← fingerprint → MusicBrainz → folder genre → write tags
  02_rename.py               ← normalise filenames using tags as source of truth
  03_move.py                 ← move files to genre folder based on genre tag
  04_review.py               ← resolve files flagged in review log
  intake.py                  ← orchestrates 01→02→03 for new files in 0new/
  genre-tree.txt             ← AllMusic genre taxonomy (reference, not modified)
```

---

## Configuration Pattern

### config.py
Contains all user-specific settings. Imported by all scripts and lib modules.
Never hardcode paths, folder names, or preferences outside this file.

```python
# config.py
from pathlib import Path

MUSIC_ROOT = Path("~/music").expanduser()

GENRE_FOLDERS = {
    "50s", "60s", "70s", "80s", "african", "blues", "brazil", "chill",
    "classical", "electronic", "estonian", "folk", "funk", "hip-hop",
    "indie", "industrial", "japan", "jazz", "latin", "metal", "pop",
    "post-punk", "post-rock", "punk", "reggae", "rnb", "rock",
    "soundtrack", "spoken"
}

SPECIAL_FOLDERS = {
    "0faves", "0faves_alltime", "0random", "0random_good",
    "0new", "0shacks", "0compilations", "0various", "0mixes", "0meditation"
}

SKIP_FOLDERS = {"0videos"}

FOLDER_TO_GENRE = {
    "50s": "50s", "60s": "60s", "70s": "70s", "80s": "80s",
    "african": "African", "blues": "Blues", "brazil": "Brazil",
    "chill": "Chill", "classical": "Classical", "electronic": "Electronic",
    "estonian": "Estonian", "folk": "Folk", "funk": "Funk",
    "hip-hop": "Hip-Hop", "indie": "Indie", "industrial": "Industrial",
    "japan": "Japan", "jazz": "Jazz", "latin": "Latin", "metal": "Metal",
    "pop": "Pop", "post-punk": "Post-Punk", "post-rock": "Post-Rock",
    "punk": "Punk", "reggae": "Reggae", "rnb": "R&B", "rock": "Rock",
    "soundtrack": "Soundtrack", "spoken": "Spoken",
    # Subgenre folders used inside 0random / 0random_good
    "ambient": "Ambient", "downtempo": "Downtempo", "house": "House",
    "techno": "Techno", "soul": "Soul", "experimental": "Experimental",
    "drum and bass": "Drum & Bass", "singer-songwriter": "Singer-Songwriter",
}

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}
MB_RATE_LIMIT_SECONDS = 1.1
ACOUSTID_MIN_SCORE = 0.8
MB_MIN_TAG_VOTES = 2
MB_MAX_GENRES = 5
```

### .env (never committed)
```
ACOUSTID_API_KEY=your_key_here
MB_USER_AGENT_EMAIL=your@email.com
```

### .env.example (committed)
```
ACOUSTID_API_KEY=
MB_USER_AGENT_EMAIL=
```

### Loading secrets
Always load from `.env` via `python-dotenv`. Never read from `os.environ`
without a fallback. Never hardcode keys anywhere in the codebase.

```python
from dotenv import load_dotenv
import os
load_dotenv()
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY", "")
```

---

## Pipeline Architecture

Scripts run in sequence. Each is standalone and re-runnable safely.

```
01_tag.py      fingerprint → MusicBrainz genres + folder genre → write tags
     ↓
02_rename.py   normalise filenames using tags as source of truth
     ↓
03_move.py     move to genre folder based on genre tag
     ↓
04_review.py   interactive resolution of flagged files

intake.py      runs 01 → 02 → 03 automatically for files arriving in 0new/
```

All scripts share these CLI flags:
- `--dry-run`        show changes, write nothing (must be default-safe)
- `--folder NAME`    process one top-level folder only
- `--log FILE`       log file path (default: review.log)

---

## Music Root Structure

```
~/music/
  ├── electronic/                     ← genre folder
  │   ├── Boards of Canada - Geogaddi (2002)/
  │   │   └── 01 - Track Title.mp3
  │   └── Artist - Album (Year)/
  ├── rock/
  ├── jazz/
  ├── [other genre folders...]
  │
  ├── 0faves/          favourite songs approx last 5 years — tag only, never move
  ├── 0faves_alltime/  all-time faves (growing slowly) — tag only, never move
  ├── 0random/         randomly collected, has genre subfolders — tag only
  ├── 0random_good/    curated picks from 0random, genre subfolders — tag only
  ├── 0new/            intake staging area
  │   ├── albums/
  │   └── singles/
  ├── 0shacks/         songs from a friend — tag only, never move
  ├── 0compilations/   compilations — per-track genre tagging, never move
  ├── 0various/        various artists albums — per-track genre tagging, never move
  ├── 0mixes/          DJ mixes — tag only, never move
  ├── 0meditation/     mood folder — tag genre + Grouping tag, never move
  └── 0videos/         skip entirely, never process
```

---

## Filename Conventions

### Input (mixed — parser must handle all of these)
```
01 - Artist Name - Song Title.mp3
Artist Name - 01 - Song Title.mp3
Artist Name - Song Title.mp3
01 Song Title.mp3
```

### Output target (02_rename.py normalises to this)
```
01 - Artist Name - Song Title.mp3
```

### Folder naming target
```
Artist Name - Album Title (Year)/
Artist Name/Artist Name - Album Title (Year)/   ← two-level, for prolific artists
```

---

## Genre System

### Tag format
Single field, slash-separated, main genre always first:
```
Electronic / Downtempo
Jazz / Soul
Rock / Shoegaze
```

### Why main genre first
- Most players display or filter on the first genre only
- Recommender model weights the first genre most heavily
- First genre = the folder it lives in (the owner's classification)
- Subsequent genres = MusicBrainz subgenres / qualifiers

### Merge logic (collect_set)
1. Folder-derived genre → always first
2. MusicBrainz genres → appended in order
3. Deduplicate case-insensitively
4. Join with ` / `
5. Never flatten or reduce to a single genre

### Genre source by folder type
| Folder type | Genre source |
|---|---|
| Genre folder (e.g. `electronic/`) | Top-level folder name |
| `0random` / `0random_good` | Subfolder name |
| `0faves`, `0shacks`, `0mixes` etc. | Fingerprint / MusicBrainz only |
| `0compilations` / `0various` | Per-track MusicBrainz only |

### Genre tree (genre-tree.txt)
The repo includes an AllMusic genre taxonomy. `lib/genres.py` parses this into
a `CHILD_TO_PARENT` dict for resolving subgenres to their root parent:
```python
# Examples derived from the tree
CHILD_TO_PARENT = {
    "shoegaze": "Rock",
    "bossa nova": "Latin",
    "idm": "Electronic",
    "downtempo": "Electronic",
    "dub": "Reggae",
    "soul": "R&B",
}
```
Use this to guarantee the first genre in a tag is always a root-level genre,
never a subgenre.

### Normalisation
All genre strings pass through `normalise_genre()` in `lib/genres.py`.
Add all new normalisations to the `GENRE_NORMALISE` dict there — never inline.

---

## MusicBrainz / AcoustID Rules

- Always use **audio fingerprinting** (AcoustID / fpcalc) — never tag-based lookup
  Tag-based lookup has caused artist misidentification and filename corruption before
- Only trust matches with AcoustID score > `ACOUSTID_MIN_SCORE` (0.8)
- Rate limit MB requests to `MB_RATE_LIMIT_SECONDS` (1.1s) minimum between calls
- Only **fill missing fields** from MB — never overwrite existing artist or title
  unless `--overwrite` flag is explicitly passed
- Filter MB tags to vote count ≥ `MB_MIN_TAG_VOTES`, cap at `MB_MAX_GENRES`
- Degrade gracefully if pyacoustid not installed or API key not set —
  fall back to folder-genre-only tagging, log a warning, continue

---

## Tag Fields Reference

| Field    | ID3 (MP3) | Vorbis (FLAC/OGG) | M4A   | Notes |
|----------|-----------|-------------------|-------|-------|
| Title    | TIT2      | title             | ©nam  | |
| Artist   | TPE1      | artist            | ©ART  | |
| Album    | TALB      | album             | ©alb  | |
| Year     | TDRC      | date              | ©day  | |
| Genre    | TCON      | genre             | ©gen  | slash-separated |
| BPM      | TBPM      | bpm               | —     | integer, M4A unsupported |
| Track    | TRCK      | tracknumber       | trkn  | |
| Grouping | TIT1      | grouping          | ©grp  | mood/context labels |

Use `Grouping` for mood/context (`meditation`, `shacks`) — keeps genre field clean.

---

## BPM Detection

- Uses `librosa.beat.beat_track` on first 60 seconds of audio
- Only written if BPM tag currently empty
- Stored as rounded integer
- Key feature for the future recommender model — do not skip or omit

---

## Recommender Model (Future)

Tags written now are feature vectors. Every tagging decision should consider
downstream recommender use. Key signals:

| Tag | Signal type |
|---|---|
| Genre (multi-value) | Primary categorical feature — never flatten |
| BPM | Tempo similarity |
| Year | Era similarity |
| Artist | Collaborative filtering anchor |
| Grouping | Mood/context — secondary feature |
| `0faves` membership | Positive preference label |
| `0faves_alltime` membership | Strong positive preference label |
| `0random_good` vs `0random` | Implicit promotion signal |

---

## Secrets and Security

- All API keys in `.env` only — loaded via `python-dotenv`
- `.env` in `.gitignore` — never committed
- `.env.example` committed with empty values — documents required keys
- `config.py` in `.gitignore` — contains local paths
- `config.example.py` committed — shows structure with placeholder paths
- No key or path may appear hardcoded anywhere in `lib/` or pipeline scripts

---

## Coding Conventions

- Always use `pathlib.Path` — never `os.path` string manipulation
- All file writes gated behind `--dry-run` check — no exceptions
- All scripts idempotent — safe to re-run on already-processed files
- Never move or rename files in `SPECIAL_FOLDERS`
- Never process `SKIP_FOLDERS` (`0videos`)
- Failures and ambiguous files → `review.log` + `review.review.json`
- Log format: `YYYY-MM-DD HH:MM:SS LEVEL message`
- Import order: stdlib → third-party → local (`config`, then `lib/`)
- Graceful degradation for all optional dependencies (pyacoustid, librosa)

---

## What Copilot Should Never Do

- Use `os.path` — always `pathlib.Path`
- Hardcode any path, folder name, or genre list outside `config.py`
- Hardcode any API key or credential anywhere — always use `.env`
- Overwrite existing artist/title tags from MusicBrainz without `--overwrite` flag
- Move or rename files from `SPECIAL_FOLDERS`
- Use tag-based MusicBrainz lookup — always fingerprint
- Flatten multi-genre strings to a single genre
- Put a subgenre before its parent genre in a tag string
- Skip the `--dry-run` check in any write operation
- Process `0videos/`
- Suggest storing secrets in `.bashrc` or hardcoded in source files

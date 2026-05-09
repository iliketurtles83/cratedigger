# Architecture — cratedigger

## Project Purpose

A personal music collection management pipeline that:
1. **Tags** audio files with genre and metadata (artist, album, year, BPM, track)
2. **Renames** files and folders to a consistent canonical format
3. **Routes** incoming music automatically to genre-appropriate locations
4. **Builds toward** a content-based recommender model using audio features and user preferences

All operations are **safe by default** (dry-run first), **idempotent** (safe to re-run), and **modular** (each stage can run independently if earlier stages have completed).

---

## Core Design Principles

### 1. Tags Own Content, Context Owns Structure

**Tags** answer: *What is this file?*
- Artist, title, track number, disc number, album, year
- Genre (multi-value, slash-separated)
- BPM, ISRC (deduplication anchor)
- AlbumArtist (compilation/soundtrack signal)

**Context** answers: *How should this file be treated?*
- Folder classification (album, artist, artist_mixed, artist_flat, subgenre, special, disc, unknown)
- Genre scope (which FOLDER_TO_GENRE applies)
- BPM eligibility (skip for spoken word, meditation, mixes)
- AlbumArtist eligibility (only write in compilation/soundtrack context)
- Naming pattern (disc prefix decision, folder name template)

These are **independent concerns**. Each script needs both, but for different reasons.
Correctness flows forward (bad tags → bad renames → bad folders), but each step's logic is independently grounded — it has a primary input from the previous stage **plus** its own structural awareness via context.

### 2. Fingerprint, Never Tag-Based Lookup

MusicBrainz queries are **always fingerprint-based** (via AcoustID):
- Fingerprints are deterministic (same audio = same result, regardless of metadata quality)
- Tag-based queries can fail silently or return wrong results if existing tags are bad
- Fingerprinting is slower but more reliable for deduplication and discovery

Consequence: BPM detection requires librosa audio analysis; online APIs are not used.

### 3. Fill, Never Overwrite (Unless Explicitly Requested)

Default behavior preserves existing tags:
- Existing **non-empty** fields are kept unless `--overwrite` or `--fix-suspicious` is passed
- `--overwrite` is dangerous (replaces all fields unconditionally); reserved for explicit user intent
- `--fix-suspicious` is safer (replaces only placeholder values like "Unknown Artist")
- Genre is always **merged**, never replaced (folder context + existing tags + MB results combined)

Consequence: Early, correct tagging prevents cascading errors downstream.

### 4. Single Source of Truth per Concern

- **`lib/context.py`** — all folder classification, genre resolution, path-based decisions
- **`config.py`** — all user settings, folder names, thresholds, genre mappings
- **`lib/tags.py`** — all tag read/write (format-agnostic wrapper for mutagen)
- **`lib/genres.py`** — all genre normalization, merge logic, blacklist

No script reimplements these concerns. All scripts import and trust their outputs.

### 5. Staging Before Promotion

New files never go directly to genre folders:
- `--intake` mode routes to `STAGED_ALBUMS_FOLDER` and `STAGED_TRACKS_FOLDER`
- User reviews staged content before `--promote` moves to final location
- Staging is a **confirmation gate** that prevents bad metadata from permanently misplacing music

Consequence: User can validate and fix issues before files are integrated into the library.

---

## Repository Structure

```
cratedigger/
  README.md                  ← setup, usage examples, typical workflows
  docs/
    architecture.md          ← this file: design patterns, structure, philosophy
    roadmap.md              ← planned features, known limitations, testing strategy
  config.py                  ← LOCAL: user settings (never committed)
  config.example.py          ← TEMPLATE: example config, committed for reference
  .env                       ← LOCAL: API keys (never committed)
  .env.example               ← TEMPLATE: example .env, committed for reference
  .gitignore                 ← covers .env, config.py, *.log, *.json, __pycache__, .venv
  
  lib/
    __init__.py
    context.py               ← FolderContext, get_folder_context(), classify_folder()
    genres.py                ← genre normalization, merge, blacklist, parent lookup
    parsers.py               ← filename/folder parsing, sanitization
    tags.py                  ← mutagen wrappers for MP3, FLAC, M4A, OGG, Opus
    mb.py                    ← MusicBrainz + AcoustID integration, rate limiting
    logger.py                ← shared logging setup
  
  01_tag.py                  ← fingerprint, lookup, fill tags
  02_rename.py               ← normalize filenames using tags
  03_folders.py              ← normalize folder names, classify folder types
  04_move.py                 ← intake routing, restructuring, promotion
  05_review.py               ← interactive resolution of flagged files/folders
  06_analyze.py              ← audio feature extraction (future)
  intake.py                  ← orchestrate 01→02→04 for new arrivals
  runscripts.py              ← helper for running scripts with standard args
  
  tests/
    test_parsers.py
    test_context.py
    test_real_audio.py       ← opt-in smoke test using real library
  
  genre-tree.txt             ← AllMusic taxonomy (reference, never edited)
  review.json                ← flagged files needing manual intervention
  review.log                 ← operation log
```

---

## Pipeline Architecture

### Data Flow

```
01_tag.py       fingerprint → MusicBrainz → context → write tags
      ↓
02_rename.py    tags → disc context (sibling scan) → write filenames
      ↓
03_folders.py   folder content (tags) → classify → write folder names
      ↓
04_move.py      genre (from tags) + context → route/restructure/promote
      ↓
05_review.py    review.json → interactive resolution
      ↓
06_analyze.py   tagged audio → extract features → store externally (future)

intake.py       INCOMING_FOLDER → orchestrate 01→02→04
```

### Per-Script Inputs and Responsibilities

| Script | Primary Input | Secondary Input | Output |
|--------|---------------|-----------------|--------|
| `01_tag.py` | Audio file | Existing tags, folder context | Filled/merged tags, review flags |
| `02_rename.py` | Tags (source of truth) | Sibling files (disc context) | Renamed audio files |
| `03_folders.py` | File tags inside folder | Folder structure | Renamed folders, review flags |
| `04_move.py` | Genre (from tags) | Folder classification | Moved/staged files, folder changes |
| `05_review.py` | review.json | User input | Resolved metadata, moved/renamed files |
| `intake.py` | INCOMING_FOLDER | — | Orchestrates 01→02→04 |
| `06_analyze.py` | Tagged audio | — | Feature vectors (external storage) |

### Why Each Script Exists

- **01_tag.py**: Fingerprint is reliable, files usually arrive untagged or poorly tagged. MB is the single source of truth for identity. Tags power everything downstream.
- **02_rename.py**: Canonical filenames are essential for searchability and organization. Parsed-from-filename is unreliable; tags are the source of truth.
- **03_folders.py**: Folder names must match tag content (artist, album, year). Run after tagging so tags are current.
- **04_move.py**: Genre and folder classification determine routing. Staging gates the final move into the library.
- **05_review.py**: Flagged cases are ambiguous by definition; only a human can resolve them reliably.
- **06_analyze.py**: Separated from tagging so audio features can be recomputed with better models without touching tags.

---

## lib/context.py — The Single Source of Truth for Structure

All path-based decisions flow from this module. No script reimplements folder classification.

### FolderContext Dataclass

```python
@dataclass
class FolderContext:
    top: str                  # raw top-level folder name (e.g., "rock", "pop")
    genre: str | None         # display genre (e.g., "Rock"), or None
    subgenre: str | None      # if in SUBGENRE_BUCKETS, (e.g., "Alt-Rock"), else None
    folder_kind: str          # album | artist | artist_mixed | artist_flat | 
                              # subgenre | local_special | disc | unknown
    is_genre_folder: bool     # top in GENRE_FOLDERS
    is_special_folder: bool   # top in SPECIAL_FOLDERS (favorites, mixes, etc.)
    is_compilation: bool      # should have albumartist tag
    is_soundtrack: bool       # should have albumartist tag
    needs_bpm: bool           # not in NO_BPM_FOLDERS
    skip: bool                # top in SKIP_FOLDERS (e.g., "videos")
    depth: int                # nesting depth below MUSIC_ROOT
```

### Key Functions

**`get_folder_context(path: Path) -> FolderContext`**
- Called once per file; result passed through all downstream logic
- Reads config at import time; never hardcodes folder names
- Returns complete context for any audio file path

**`classify_folder(folder: Path) -> str`**
- Determines folder type by structure (presence of audio files, subdirectories, naming patterns)
- Returns: `album`, `artist`, `artist_mixed`, `artist_flat`, `subgenre`, `local_special`, `disc`, `unknown`
- Used by 03_folders.py (folder naming) and 04_move.py (routing)

**`infer_compilation_folder(folder: Path, file_tag_dicts: list[dict] | None = None) -> bool`**
- Detects if a folder should be treated as a compilation
- Detection order:
  1. Folder context flag (`is_compilation` or `is_soundtrack`) → True
  2. AlbumArtist in compilation set ("Various Artists", "VA") → True
  3. Single AlbumArtist ≠ track artists → True
  4. `COMPILATION_ARTIST_THRESHOLD`+ distinct artists (no dominant artist > 50%) → True
- Used by 01_tag.py (consistency check) and 03_folders.py (folder naming)

**`is_disc_subfolder(name: str) -> bool`**
- Returns True if name matches `cd1`, `disc 2`, `disk3`, etc.
- Used by 02_rename.py and 03_folders.py to skip disc subfolders

---

## Tag System

### Supported Formats and Field Mapping

| Format | Field Type | Notes |
|--------|-----------|-------|
| MP3 | ID3v2.4 | TIT2, TPE1, TPE2, TALB, TDRC, TCON, TBPM, TRCK, TPOS, TSRC |
| FLAC | Vorbis Comments | title, artist, albumartist, album, date, genre, bpm, tracknumber, discnumber, isrc |
| OGG/Opus | Vorbis Comments | same as FLAC |
| M4A/AAC | iTunes-style atoms | ©nam, ©ART, aART, ©alb, ©day, ©gen, trkn, disk (no BPM support) |

All mappings centralized in `lib/tags.py`; no format-specific logic in pipeline scripts.

### Critical Tags

- **Genre**: slash-separated (e.g., `Rock / Alt-Rock / Shoegaze`). Main genre first. Never flattened.
- **AlbumArtist**: written only in compilation/soundtrack context. Never written for regular artist albums.
- **ISRC**: international standard recording code. Deduplication anchor. Written if MB provides it.
- **BPM**: integer. Detected via librosa. Not supported in M4A (ID3 limitation).
- **Track**: preserved in `x/total` format (e.g., `3/12`) for playlist ordering.
- **Disc**: for multi-disc albums (e.g., `1`, `2`).

No grouping tag. All required fields must have entries in `lib/tags.py`.

---

## Genre System

### Tag Format and Merge Strategy

Tag format: slash-separated hierarchy, main genre first.
```
Rock / Alt-Rock / Shoegaze
Electronic / Downtempo
Jazz / Soul
```

**Merge order** (lib/genres.py `merge_genres()`):
1. Folder context genre (FOLDER_TO_GENRE[top] or SUBGENRE_BUCKETS)
2. Existing tag genres (never lost)
3. MusicBrainz genres (appended, blacklisted entries discarded)
4. Deduplicate case-insensitively, join with ` / `

**Genre source by folder type**:
| Folder Type | Source |
|---|---|
| Genre folder (e.g., `rock/`) | `FOLDER_TO_GENRE[top]` |
| Subgenre bucket (e.g., `rock/0alt-rock/`) | `FOLDER_TO_GENRE[top]` + `SUBGENRE_BUCKETS[level2]` |
| Special folder (e.g., `0favorites/`) | MB fingerprint only (no default) |
| Compilation/various | Per-track MB lookup (no folder default) |

**Blacklist**: `other`, `unknown`, `miscellaneous`, `seen live`, `favorites`, `good` — these are filtered out from MB results.

Consequence: Genre tags grow richer over time without losing user edits or folder intent.

---

## Folder Naming and Classification

### Naming Templates

**Regular albums** (any folder where `infer_compilation_folder() == False`):
```
Artist Name - Album Title (Year)/
```

**Compilations and soundtracks**:
```
Album Title (Year)/
```
(No artist prefix — compilations span artists or have "Various Artists" as albumartist.)

**Artist folders** (when multiple albums are grouped under one artist):
```
Artist Name/
  Artist Name - Album Title 1 (Year)/
  Artist Name - Album Title 2 (Year)/
```

**Subgenre buckets** (e.g., `0alt-rock/`, `0dreampop/`):
- Prefix with `0` (sorts to top)
- Not renamed; treated as read-only containers

**Special folders** (e.g., `0favorites/`, `0random/`, `0mixes/`):
- Prefix with `0`
- Not renamed or moved
- Files inside stay in place for manual curation

### Folder Classification Logic

```python
def classify_folder(folder: Path) -> str:
    has_audio = any(f.suffix in AUDIO_EXTENSIONS for f in folder.iterdir() if f.is_file())
    has_subdirs_with_audio = any(
        has_audio_files(subdir) and classify(subdir) in ('album', 'artist')
        for subdir in folder.iterdir() if subdir.is_dir()
    )
    starts_with_0 = folder.name.startswith("0")
    has_separator = " - " in folder.name

    if is_disc_subfolder(folder.name): return "disc"
    if starts_with_0 and folder.name in SUBGENRE_BUCKETS: return "subgenre"
    if starts_with_0: return "local_special"
    if has_separator: return "album"
    if has_subdirs_with_audio and not has_audio: return "artist"
    if has_subdirs_with_audio and has_audio: return "artist_mixed"
    if not has_subdirs_with_audio and has_audio: return "artist_flat"
    return "unknown"
```

**Implications**:
- `artist_flat`: loose files directly under artist folder (e.g., `rock/Radiohead/*.mp3`). Should probably be grouped into albums.
- `artist_mixed`: both albums (subdirs) and loose files under artist. Loose files should move to `0singles/` subfolder.
- `disc`: subfolder like `cd1/`, `disc2/` — not renamed, used for disc prefix logic in filenames.

---

## Configuration (config.py)

User configuration is the single source of truth for:
- Music library root (`MUSIC_ROOT`)
- Folder taxonomy (`FOLDER_TO_GENRE`, `SUBGENRE_BUCKETS`, `SPECIAL_FOLDERS`, `SKIP_FOLDERS`)
- Feature eligibility (`NO_BPM_FOLDERS`, `ALBUMARTIST_FOLDERS`)
- Thresholds (`ARTIST_FOLDER_THRESHOLD`, `COMPILATION_ARTIST_THRESHOLD`)
- Quality gates (`ACOUSTID_MIN_SCORE`, `MB_RATE_LIMIT_SECONDS`)

**Critical principle**: Never hardcode folder names, genre lists, or paths outside `config.py`. All scripts import config and respect its values.

---

## Pipeline Dependencies

### Hard Dependencies
- **01_tag.py must run before 03_folders.py**: Folder names are built from file tags. Untagged files will cause 03_folders.py to flag for review.
- **02_rename.py should run after 01_tag.py**: Better tags → better filenames.
- **04_move.py runs after 03_folders.py (optional but recommended)**: Folder names should be canonical before routing.

### Soft Dependencies
- **05_review.py** can run anytime after flags are written (review.json from 01_tag.py, 03_folders.py, 04_move.py).
- **06_analyze.py** should run after 01_tag.py (needs ISRC in tags for cross-referencing).

### Why intake.py Exists
Orchestrates `01→02→04` in one command for new arrivals. Users can run one command instead of three, but the underlying pipeline is unchanged.

---

## Design Patterns

### Pattern 1: Dry-Run First, Write Second

All write operations are gated by a dry-run check:
```python
if not dry_run:
    file.write_text(content)
```

Default is `--dry-run` (safe). `--no-dry-run` enables writes. This prevents accidental data loss.

### Pattern 2: Lazy Imports for Optional Dependencies

Some libraries are optional (librosa for BPM, musicbrainzngs, pyacoustid). Use lazy imports:
```python
_librosa = None

def detect_bpm(path: Path) -> int | None:
    global _librosa
    if _librosa is None:
        try:
            import librosa
            _librosa = librosa
        except ImportError:
            _librosa = False
    if not _librosa:
        return None
    # use _librosa
```

Consequence: Script can run without optional libraries, degrading gracefully.

### Pattern 3: Logging and Flagging, Not Exceptions

When something can't be resolved:
- Log the issue to console and `review.log`
- Write the file/folder path to `review.json`
- Continue processing (don't crash)
- User reviews flagged items later with `05_review.py`

Consequence: Pipeline completes, even with ambiguous cases. No silent failures.

### Pattern 4: Idempotency

All operations are idempotent: running a script twice with the same input produces the same output (no unwanted changes on the second run).

Consequence: Safe to re-run on partially-processed folders or to catch missing files from an earlier incomplete run.

---

## Testing and Validation

### Test Coverage

- **`test_parsers.py`**: filename/folder parsing edge cases (no I/O, fast)
- **`test_context.py`**: folder classification and context inference (no I/O, fast)
- **`test_real_audio.py`**: opt-in smoke test using real library (read-only, scans for one audio file)

Run with:
```bash
python -m unittest discover -s tests
```

Opt-in real audio tests:
```bash
CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1 python -m unittest discover -s tests
```

### Manual Validation Protocol

Before applying to the full library:
1. Copy a folder to `foldername_test/`
2. Add temporary entry to `FOLDER_TO_GENRE`
3. Run with `--dry-run`, review changes
4. Run with `--no-dry-run` if satisfied
5. Verify manually (e.g., open in Strawberry music player)
6. Remove test folder and config entry

---

## Extension Points

### Adding a New Tag Field

1. Add format-specific mapping to `lib/tags.py` (ID3 frame, Vorbis comment name, M4A atom)
2. Add field to tag fill logic in `01_tag.py`
3. Update `lib/context.py` if it affects folder classification or context
4. Document in Tag Fields Reference section

### Adding a New Special Folder Type

1. Add folder name to `config.py` special set (e.g., `SPECIAL_FOLDERS = {"favorites", ...}`)
2. Update logic in `lib/context.py` `get_folder_context()` if it changes genre or behavior
3. Add test case to `test_context.py`

### Adding Genre Remapping Logic

1. Add entry to `config.py` `SUBGENRE_BUCKETS` or `FOLDER_TO_GENRE`
2. Genre merge logic in `lib/genres.py` will automatically include it
3. Test with `01_tag.py --dry-run` on a sample folder

### Adding a Post-Pipeline Feature

Create a new script (e.g., `07_something.py`) that:
- Imports from `lib/` (shared utilities)
- Reads tags via `lib/tags.py` (not raw mutagen)
- Uses `get_folder_context()` for structure (not hardcoded paths)
- Supports `--dry-run` and `--folder` flags
- Logs via `lib/logger.py`

Do not add new logic to existing scripts; keep them focused on their primary responsibility.

---

## Data Integrity and Safety

### What's Protected
- **config.py** — never committed (local settings)
- **.env** — never committed (API keys)
- **Existing tags** — never overwritten without `--overwrite` or `--fix-suspicious`
- **SPECIAL_FOLDERS** — never renamed or moved
- **SKIP_FOLDERS** — never processed
- **Disc subfolders** — never renamed

### What's Not Protected
- Folder structure (restructuring is a feature)
- Filenames (renaming is a feature)
- Genre tags (merging includes new data)

### Recovery Strategy
- Always test with `--dry-run` first
- Review `review.log` for warnings before applying changes
- Keep manual backups of important folders before running `--no-dry-run` for the first time
- Use git to track tag changes: commit `config.py`, check diffs in `review.json` before and after

---

## Future Architecture (06_analyze.py, Recommender)

Audio analysis is intentionally **separated from tagging**:
- Analysis runs after tagging completes
- Results stored **externally** (JSON lines, sqlite, or feature store), not in audio tags
- BPM is the only feature stored in tags (conventional, widely supported)
- Allows re-running analysis with better models without touching tags
- Enables incremental feature extraction (new files only)

**Recommender signals** (planned):
- Multi-genre tags (never flattened, essential input)
- Audio features (energy, danceability, spectral centroid)
- Artist graph (collaborative filtering)
- User preference signals (0faves folder, skip history)

---

## Coding Principles for Contributors

1. **Use pathlib.Path always** — never os.path
2. **Import order**: stdlib → third-party → local (config, then lib/)
3. **Don't repeat logic** — if it belongs in lib/, put it there
4. **Call get_folder_context() once per file** — pass result through, don't re-compute
5. **Respect config.py** — never hardcode folder names, thresholds, or paths
6. **Log and flag, don't crash** — pipeline should complete even with ambiguous cases
7. **Test with --dry-run first** — before touching files
8. **Document assumptions** — especially about folder structure, tag format, external APIs

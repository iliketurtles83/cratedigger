# cratedigger

Personal music collection management pipeline. Tags audio files with genre and metadata, normalises filenames and folder names, and routes incoming music automatically.

> **Still being tested — use at your own risk. Always run with `--dry-run` first.**

---

## What it does

| Script | Purpose |
|---|---|
| `intake.py` | Modern intake pipeline: uses beets with a transient catalog for MusicBrainz/AcoustID metadata, then runs cratedigger renaming, folder normalization, and Tier 1 placement. (Legacy staging flow available via `--legacy-move`). |
| `scripts/audit_repair.py` | Audit library tags against backup (`/media/jack/monodong/audio2`) to purge junk folksonomy tags, restore unflattened subgenres, and protect collaboration credits. |
| `01_tag.py` | Standalone tagger using AcoustID + MusicBrainz. Uses folder context as fallback only when existing tags are absent; guest/featured credits protected. |
| `02_rename.py` | Normalise audio filenames using tags as source of truth. Uses folder structure (disc subfolders, sibling scan) to decide whether to apply a disc-track prefix. |
| `03_folders.py` | Rename folders to canonical `Artist - Album (Year)/` format. Classifies each folder (album, artist, compilation, subgenre, etc.) to determine the right naming pattern and what to flag for review. |
| `04_move.py` | Legacy staging router and promotion utility. |
| `05_review.py` | Interactive resolution of files and folders flagged during earlier pipeline steps. |
| `06_analyze.py` | Audio feature extraction (librosa + pyloudnorm) into a pickled pandas DataFrame. *(Outside standard ingestion scope)*. |

Run new arrivals through `intake.py --dry-run`. Run tag hygiene/audits with `scripts/audit_repair.py`.


---

## Setup

**1. Clone and create a virtual environment**

```bash
git clone https://github.com/youruser/cratedigger.git
cd cratedigger
python3 -m venv .venv
source .venv/bin/activate
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

Fingerprinting also requires the `fpcalc` binary:

```bash
# Debian / Ubuntu
sudo apt install libchromaprint-tools

# macOS
brew install chromaprint
```

**3. Configure**

```bash
cp config.example.py config.py
```

Edit `config.py` to set your `MUSIC_ROOT`, genre folders, and other preferences.

**4. Set API keys**

```bash
cp .env.example .env
```

Edit `.env` and add:

```
ACOUSTID_API_KEY=your_key_here
MB_USER_AGENT_EMAIL=your@email.com
```

Get a free AcoustID API key at https://acoustid.org/login.

---

## Usage

All scripts support these common flags:

| Flag | Description |
|---|---|
| `--dry-run` | Show changes without writing anything *(default)* |
| `--no-dry-run` | Actually apply changes |
| `--folder NAME` | Process one top-level folder only |
| `--log FILE` | Log file path (default: `review.log`) |

### 01_tag.py — Tag files

```bash
# Preview what would be tagged in the "rock" folder
python3 01_tag.py --dry-run --folder rock

# Tag for real, skip BPM detection and MusicBrainz lookups
python3 01_tag.py --no-dry-run --no-bpm --no-mb --folder rock

# Replace suspicious placeholder values (e.g. "Unknown Artist") using MB
python3 01_tag.py --no-dry-run --fix-suspicious --folder rock

# Force overwrite all tags from MusicBrainz (use with caution)
python3 01_tag.py --no-dry-run --overwrite --folder rock
```

Additional flags: `--no-bpm`, `--no-mb`, `--fix-suspicious`, `--overwrite`

### 02_rename.py — Rename audio files

```bash
python3 02_rename.py --dry-run --folder rock
python3 02_rename.py --no-dry-run --folder rock
```

Renames files to `01 - Artist - Title.ext` (or `1-01 - Artist - Title.ext` for multi-disc). Run after `01_tag.py`.

### 03_folders.py — Normalise folder names

```bash
python3 03_folders.py --dry-run --folder rock
python3 03_folders.py --no-dry-run --folder rock
```

Renames folders to `Artist - Album (Year)/`. Run after `02_rename.py`.

### 04_move.py — Route and promote music

Three mutually exclusive modes:

```bash
# Route new arrivals from INCOMING_FOLDER to staging
python3 04_move.py --intake --dry-run
python3 04_move.py --intake --no-dry-run

# Apply artist folder threshold rule to existing genre folders
python3 04_move.py --restructure --dry-run --folder rock
python3 04_move.py --restructure --no-dry-run --folder rock

# Promote reviewed albums from staging to genre folders
python3 04_move.py --promote --dry-run
python3 04_move.py --promote --no-dry-run
```

Phase 4 additions in `config.py`:

- `GENRE_REMAP_RULES`: map incoming genres to local taxonomy before routing/writing.
- `PREFERENCE_OVERRIDES`: scoped overrides (`global`, `genre`, `artist`) for
	`artist_folder_threshold` and `preference_labels`.
- `MOVE_HOOKS`: optional `pre_move` / `post_move` callbacks.

Hook safety and failure semantics:

- Hook commands must resolve under `HOOK_ALLOWED_PATHS` (sandbox boundary).
- Hooks run with `shell=False`, JSON payload via stdin, and timeout control.
- `HOOK_FAILURE_POLICY` controls behavior:
	`continue` logs warning, `review` logs and appends `hook_failure` in
	`review.json`, `abort` stops processing.

Preference labels are written to `PREFERENCE_LABELS_PATH` as sidecar metadata
and do not alter folder routing unless explicitly used by custom logic.

### 05_review.py — Resolve flagged files

```bash
python3 05_review.py
```

Interactively resolves files and folders flagged in `review.json` during earlier pipeline steps.

### 06_analyze.py — Audio feature extraction

Walks a folder of songs/albums and extracts audio features (BPM, key, mode,
energy, LUFS loudness, spectral centroid, ZCR, danceability, duration) into a
pickled `pandas.DataFrame`. Runs independently of tagging and never mutates
audio tags.

```bash
# First run: full analysis
python3 06_analyze.py --folder rock --no-dry-run

# Re-run: incremental, only new/changed files are re-analyzed
python3 06_analyze.py --folder rock

# Re-analyze everything (e.g. after a schema bump)
python3 06_analyze.py --folder rock --force
```

The output path defaults to `config.FEATURES_STORE_PATH` (`features.pkl`).
The DataFrame is keyed by absolute file path with incremental detection via
`(mtime, size, schema_version)`. To migrate the schema, bump
`FEATURE_SCHEMA_VERSION` in `06_analyze.py`; rows with an older version are
re-analyzed automatically on the next run.

### intake.py — Full intake in one command

Orchestrates `01_tag.py → 02_rename.py → 04_move.py --intake` for files in `INCOMING_FOLDER`.

```bash
python3 intake.py --dry-run
python3 intake.py --no-dry-run
```

---

## Typical workflows

**Tag and rename an existing folder:**
```bash
# Quick check of what would change, where MB is needed and where BPM is missing
python3 01_tag.py --dry-run --no-bpm --no-mb --folder jazz
# Write tags, including MB lookups and BPM detection
python3 01_tag.py --no-dry-run --folder jazz
# Preview filename changes before writing
python3 02_rename.py --dry-run --folder jazz
# Actually rename files
python3 02_rename.py --no-dry-run --folder jazz
python3 03_folders.py --no-dry-run --folder jazz
```

**Process new music dropped in the incoming folder:**
```bash
# Preview beets matching and cratedigger's filename/folder/path changes
python3 intake.py --dry-run

# Match/write metadata, then let cratedigger organize into Tier 1
python3 intake.py --no-dry-run

# Metadata-only beets trial; this does not move or rename files
.venv/bin/python -m beets -c config/beets.yaml import --pretend /media/jack/buffer/audio/incoming
```

Cratedigger is the sole owner of physical layout. Beets path templates are not
configured, and `intake.py` gives beets a transient catalog so the subsequent
cratedigger moves cannot leave stale paths in the persistent beets database.

**Validate and repair Tier 1 layout:**
```bash
# Check that every top-level folder is covered by a config guardrail
python3 04_move.py --validate-config

# Preview explicitly tagged Various Artists releases currently under artists
python3 04_move.py --relocate-compilations

# Apply only after reviewing the dry-run
python3 04_move.py --relocate-compilations --no-dry-run
```

**Audit duplicate artist folders:**
```bash
# Report confirmed, partial, ID-less, and conflicting identity groups
python3 scripts/reconcile_artists.py

# Preview only MusicBrainz-confirmed, collision-free merges
python3 scripts/reconcile_artists.py --fix

# Apply the confirmed subset
python3 scripts/reconcile_artists.py --fix --no-dry-run

# Fetch missing artist catalogs during a dry-run and cache the reviewed set
python3 scripts/reconcile_artists.py --online-release-evidence --fix

# Apply exactly the cache-backed, collision-free set (no live API fetches)
python3 scripts/reconcile_artists.py \
  --online-release-evidence --fix --no-dry-run
```

Artist reconciliation is filesystem-first because the beets database may not
index the entire library. Automatic merges require the same single
MusicBrainz album-artist ID in every source folder, complete matching
track-artist ID coverage, or an exact match for every unproven album in the
known artist's MusicBrainz release-group catalog. Catalog evidence is fetched
only during dry-runs and cached; live mode never discovers new candidates.
Partial, missing, or conflicting evidence and any destination collision remain
report-only. Only `albumartist` is normalized; per-track artist credits are
preserved.

**Legacy intake staging flow:**
```bash
python3 intake.py --legacy-move --no-dry-run
# Review staged albums, then promote:
python3 04_move.py --promote --no-dry-run
```

**Testing a single folder safely:**
```bash
cp -r /music/jazz /music/jazz_test
# Add "jazz_test": "Jazz" to FOLDER_TO_GENRE in config.py temporarily
python3 01_tag.py --dry-run --no-bpm --folder jazz_test
# When happy:
python3 01_tag.py --no-dry-run --folder jazz_test
# Remove jazz_test entry from config.py when done
```

---

## File and folder naming conventions

**Audio files:**
```
01 - Artist Name - Song Title.mp3
1-01 - Artist Name - Song Title.mp3   # multi-disc
```

**Folders:**
```
Artist Name - Album Title (Year)/
Artist Name - Album Title (Year)/     # inside artist folder
Album Title (Year)/                   # compilations and soundtracks
```

---

## Requirements

- Python 3.10+
- `mutagen` — tag read/write
- `python-dotenv` — API key loading
- `pyacoustid` + `fpcalc` binary — audio fingerprinting *(optional)*
- `musicbrainzngs` — MusicBrainz metadata *(optional)*
- `librosa` — BPM detection *(optional)*

---

## Testing

The repo uses the standard library `unittest` runner; no extra test dependency is required.

```bash
.venv/bin/python -m unittest discover -s tests
```

That default suite only covers pure helper logic and temporary-directory folder-shape tests, so it is fast and does not touch your music library.

There is also an opt-in, read-only smoke test that uses your real `config.MUSIC_ROOT` and scans for one actual audio file:

```bash
CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1 .venv/bin/python -m unittest discover -s tests
```

Use the real-audio tests sparingly. They do not write anything, but they do depend on your local library and current `config.py`.

If you want those dry-run integration tests to target a specific top-level library folder, set `CRATEDIGGER_TEST_FOLDER`:

```bash
CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1 CRATEDIGGER_TEST_FOLDER=pop .venv/bin/python -m unittest discover -s tests
```

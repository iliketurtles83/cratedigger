# cratedigger

Personal music collection management pipeline. Tags audio files with genre and metadata, normalises filenames and folder names, and routes incoming music automatically.

> **Still being tested — use at your own risk. Always run with `--dry-run` first.**

---

## What it does

| Script | Purpose |
|---|---|
| `01_tag.py` | Fill missing or bad tags using AcoustID fingerprinting + MusicBrainz. Uses folder context (genre root, compilation/soundtrack flags, BPM eligibility) to decide which fields to write and how to merge genres. |
| `02_rename.py` | Normalise audio filenames using tags as source of truth. Uses folder structure (disc subfolders, sibling scan) to decide whether to apply a disc-track prefix. |
| `03_folders.py` | Rename folders to canonical `Artist - Album (Year)/` format. Classifies each folder (album, artist, compilation, subgenre, etc.) to determine the right naming pattern and what to flag for review. |
| `04_move.py` | Route incoming music to genre-appropriate staging folders; restructure existing folders using the artist threshold rule; promote reviewed albums to the library. Uses tags for genre routing and folder classification to determine destination. |
| `05_review.py` | Interactive resolution of files and folders flagged during earlier pipeline steps. |
| `06_analyze.py` | Audio feature extraction for recommender *(future)* |

Run in order: `01 → 02 → 03 → 04`. Or use `intake.py` to orchestrate `01 → 02 → 04` for new arrivals.

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

### 05_review.py — Resolve flagged files

```bash
python3 05_review.py
```

Interactively resolves files and folders flagged in `review.json` during earlier pipeline steps.

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
python3 01_tag.py --no-dry-run --folder incoming
python3 02_rename.py --no-dry-run --folder incoming
python3 04_move.py --intake --no-dry-run
# Review staged albums, then:
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


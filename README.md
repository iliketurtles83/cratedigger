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
| `06_analyze.py` | Audio feature extraction (librosa, pyloudnorm, Essentia, Discogs-EffNet) into a pickled pandas DataFrame. *(Outside standard ingestion scope)*. |

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
energy, LUFS loudness, spectral centroid, ZCR, danceability, duration, signal
level) plus both Discogs-EffNet outputs into a pickled `pandas.DataFrame`. Runs
independently of tagging and never mutates audio tags.

```bash
# First run: full analysis
.venv/bin/python 06_analyze.py --folder rock

# Re-run: incremental, only new/changed/stale files are analyzed
.venv/bin/python 06_analyze.py --folder rock

# Preview what would be analyzed (no extraction, no writes)
.venv/bin/python 06_analyze.py --dry-run
```

Unlike the tagging/movement scripts, analysis writes its feature store by
default; use `--dry-run` to preview without extracting or saving. Omit
`--folder` to scan the entire configured music root; `--folder` runs only prune
rows inside that folder. A preflight check stops the run before scheduling any
work if Essentia, ONNX Runtime, or the Discogs-EffNet model is unusable.

Progress is saved atomically at most every `--checkpoint-seconds` (default 300)
and on completion. Ctrl+C saves completed results, stops all workers, and exits
with status 130; re-run the same command to resume. Use the same Python
environment when resuming: pandas pickle files may not be readable by older
pandas versions. An unreadable existing store stops the run rather than
discarding saved progress. `--model-threads` sets ONNX Runtime threads per
worker (default: `cpu_count // workers`).

The output path defaults to `config.FEATURES_STORE_PATH` (`features.pkl`).
The DataFrame is keyed by absolute file path with incremental detection via
`(mtime, size, schema_version)`. To migrate the schema, bump
`FEATURE_SCHEMA_VERSION` in `06_analyze.py`; rows with an older version are
re-analyzed automatically on the next run, and a failed re-analysis keeps the
previous row for a later retry.

Embeddings are versioned separately by `EMBEDDING_RECIPE` and the model
checksum. Each row stores the normalized 400-dim `embedding` (compatibility),
raw `style_activations` (400) and `audio_embedding` (1280), and
`embedding_status` (`ok`, `failed`, `unavailable`, `skipped_too_short`,
`skipped_silent`, `skipped_duration`). Failed, unavailable, or stale embeddings
are recomputed without repeating DSP analysis, and a full re-analysis reuses a
current embedding when the audio hash is unchanged. Silent audio (peak at or
below −80 dBFS) and tracks over 20 minutes are not embedded.

`loudness_lufs` is ITU-R BS.1770 integrated loudness of the original channels
at the native sample rate (schema 11). Earlier schemas measured the mono
downmix, which read 3–6 dB low for stereo files. It describes the file's
mastered level; use it for playback leveling rather than rewriting audio.

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
configured, and `intake.py` gives beets a transient catalog. The historical
persistent Beets database is retained for now but is not part of the active
pipeline and is not synchronized after cratedigger moves.

All generated path components are Windows-safe: reserved device names,
control characters, `< > : " / \ | ? *`, leading dots, and trailing
spaces/dots are removed or replaced. Destination checks and the repair utility
also treat names case-insensitively, since Windows cannot store sibling paths
that differ only by capitalization.

`scripts/sync_beets_db.py` is legacy-only and should not be used as a routine
post-move step. Player refresh is also not run from this machine: after
`scripts/server_sync.sh` completes, refresh the player on the remote host
according to that host's deployment procedure.

`scripts/server_sync.sh` removes obsolete destination paths before transfer so
legacy capitalization variants cannot alias canonical paths on a
case-insensitive filesystem. Hidden files, editor backups, and VDJ stem files
are perishable exclusions: they are not copied and cannot keep a stale
directory alive. The excluded `0videos` tree remains protected from deletion.

**Validate and repair Tier 1 layout:**
```bash
# Check that every top-level folder is covered by a config guardrail
python3 04_move.py --validate-config

# Preview Various Artists releases and loose singles currently under artists
python3 04_move.py --relocate-compilations

# Apply only after reviewing the dry-run
python3 04_move.py --relocate-compilations --no-dry-run
```

**Audit and repair Windows path compatibility:**
```bash
# Report invalid names and case-insensitive collisions
python3 scripts/repair_windows_paths.py

# Apply the reviewed normalization and merges
python3 scripts/repair_windows_paths.py --fix --no-dry-run
```

**Audit unmatched releases against Beets/MusicBrainz:**
```bash
# Inventory one bounded artist bucket without network access
.venv/bin/python scripts/audit_beets_matches.py --bucket Q

# Match the inventory online using temporary catalogs without writing tags
.venv/bin/python scripts/audit_beets_matches.py --bucket Q --online
```

The audit command requires a single bucket or a folder relative to the music
root. Online mode forces `--nowrite --nomove --nocopy --noincremental`, uses a
separate temporary Beets catalog for each unit, verifies that source size and
timestamps did not change, and writes a JSON evidence report under
`~/.cache/cratedigger/`. It never reads or updates the legacy persistent Beets
database.

Apply only explicitly reviewed release IDs from that report:
```bash
# Preview exact tag writes
.venv/bin/python scripts/apply_beets_matches.py \
  --report ~/.cache/cratedigger/beets-match-audit-q.json \
  --release-id MUSICBRAINZ_RELEASE_ID

# Apply the same reviewed evidence
.venv/bin/python scripts/apply_beets_matches.py \
  --report ~/.cache/cratedigger/beets-match-audit-q.json \
  --release-id MUSICBRAINZ_RELEASE_ID --fix --no-dry-run
```

Application refuses stale source files, incomplete release matches, conflicting
existing release IDs, and release IDs not explicitly allowlisted. It writes an
application record alongside the audit report. Run `02_rename.py` and
`03_folders.py` in dry-run mode afterward; cratedigger remains the sole owner
of filenames and layout.

Full Various Artists releases move to `0compilations`. Loose tracks found
under `Bucket/Various Artists/0singles` move to the primary track artist's
Tier 1 `0singles` folder. Existing artist-folder spellings are reused, featured
credits remain in the `artist` tag, and release-scoped compilation metadata is
cleared so the extracted track behaves as a standalone single.

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

# Compare colliding albums without changing files
python3 scripts/reconcile_artists.py \
  --online-release-evidence --collision-report

# Preview collision resolution; differing encodes are retained as alternates
python3 scripts/reconcile_artists.py \
  --online-release-evidence --collision-report --resolve-collisions --fix

# Apply only the reviewed, cache-backed collision resolutions
python3 scripts/reconcile_artists.py \
  --online-release-evidence --resolve-collisions --fix --no-dry-run

# Fetch recording-title catalogs to corroborate compilation and loose tracks
python3 scripts/reconcile_artists.py --online-recording-evidence --fix

# Apply exactly the reviewed recording-cache-backed set
python3 scripts/reconcile_artists.py \
  --online-recording-evidence --fix --no-dry-run
```

Artist reconciliation is filesystem-first because the beets database may not
index the entire library. Automatic merges require the same single
MusicBrainz album-artist ID in every source folder, complete matching
track-artist ID coverage, or an exact match for every unproven album in the
known artist's MusicBrainz release-group catalog. For compilation, edition,
and loose tracks whose album title is not in that catalog, optional recording
catalog evidence requires every unproven track title to occur in the known
artist's MusicBrainz recordings. Catalog evidence is fetched only during
dry-runs and cached; live mode never discovers new candidates. Partial,
missing, or conflicting artist evidence remains report-only.
Destination collisions also remain report-only unless `--resolve-collisions`
is explicitly selected. Collision resolution requires compatible release IDs
and at least one matching recording ID or title/duration pair. Identical audio
is consolidated, complementary tracks are combined, and differing encodes are
retained with deterministic `[alternate-<hash>]` filename suffixes. Conflicting
release evidence is never merged. Only `albumartist` is normalized; per-track
artist credits are preserved.

**Repair misidentified tracks:**
```bash
# Report tracks whose title and album are both absent from the artist's catalogs
python3 scripts/repair_track_identity.py

# Fingerprint the uncached candidates during a dry-run and cache the evidence
python3 scripts/repair_track_identity.py --online-fingerprints

# Preview title corrections and relocations for selected artists
python3 scripts/repair_track_identity.py --fix --relocate \
  --artist "Kid606" --artist "Kiss"

# Apply exactly the reviewed, cache-backed repairs
python3 scripts/repair_track_identity.py --fix --relocate --no-dry-run \
  --artist "Kid606" --artist "Kiss"

# Bring filenames back in line with the repaired tags
python3 02_rename.py --no-dry-run --folder "D/The Dust Brothers"
```

Identity repair complements artist reconciliation: unproven tracks are what
hold a duplicate group at `partial`, so correcting or rehoming them usually
promotes the group to `confirmed`. Identity evidence is pooled across every
folder sharing an artist comparison key, because the duplicate spelling is
typically the one missing MusicBrainz IDs. A title is only rewritten when the
fingerprint confirms the expected artist *and* the corrected title occurs in
that artist's cached recording catalog; MusicBrainz placeholders such as
`[unknown]` are always refused. A foreign-credited album folder relocates as a
unit when at least two of its tracks, and a dominant share of its audio, agree
on one artist — so soundtracks move intact while guest credits keep their own
`artist` tag under the new `albumartist`. Isolated strays move to the root
singles holding pen instead. AcoustID lookups happen only during dry-runs and
are cached by audio hash, so a reviewed live run never contacts the network.

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

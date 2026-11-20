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
| `07_recommend.py` | Evaluate recommendations and export features for the player. |

Run new arrivals through `intake.py --dry-run`. Run tag hygiene/audits with `scripts/audit_repair.py`.

## Repository layout and local data

| Path | Purpose |
|---|---|
| `01_tag.py`–`07_recommend.py`, [`intake.py`](intake.py), [`runscripts.py`](runscripts.py) | Existing pipeline and orchestration entry points. |
| [`cratedigger/`](cratedigger/) | Unified CLI, operational database, scan/plan/apply engine, and web UI. |
| [`lib/`](lib/) | Shared tagging, path, metadata, audio analysis, and recommendation helpers. |
| [`scripts/`](scripts/) | Audits, repairs, migrations, artwork, playlists, and sync utilities. |
| [`tests/`](tests/) | Unit and integration tests; real-library audio tests are opt-in. |
| [`config/`](config/) and [`config.example.py`](config.example.py) | Beets configuration and the local settings template. |
| [`docs/`](docs/) | Architecture, roadmap, feature extraction, UI plans, and the player contract. |
| [`notebooks/`](notebooks/) | Exploratory analysis notebooks, including saved outputs. |

Local configuration and operational data are ignored by Git. These include
`config.py`, `.env`, `features.pkl` and its checkpoints/backups,
`features_export.json.gz`, `review.json`, `review.log`, and `cratedigger.db`.
Reports, archived logs, downloaded models, the virtual environment, generated
arrays, the derived `analytics/` snapshot, and audio files are also ignored. They may still be valuable or in use:
do not delete them merely because they are ignored. Deliberately reviewed
audio test fixtures may be tracked under `tests/fixtures/`.

Ignore rules match paths and file types, not file size. They do not remove
already tracked files or erase older commits; review the staged changes before
committing.

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

### Local web UI

Run `cratedigger ui` and open `http://127.0.0.1:8765/`. The UI is
localhost-only; it is not intended for LAN access or reverse-proxy exposure.
POST requests require a session CSRF token, and browser sessions expire when
the server restarts. The Library page browses the SQLite index rather than
scanning files live. Use `cratedigger sync-features` to refresh it from the
feature store only after extraction has stopped; that CLI command does not
coordinate with an independent extractor. The index may be incomplete or stale
between refreshes.

The Operations page exposes an allowlisted subset of script commands. Jobs run
as background subprocesses with persistent logs and resource locks; conflicting
jobs are rejected with a busy-resource message rather than started concurrently.
Compatible library readers can run together, while writers and direct UI
library mutations exclude conflicting jobs. Cancelling a job retains its locks
until its worker and same-process-group children have stopped; an orphaned
process group keeps its locks until it exits.

Applying a legacy operation requires a successful matching preview. The job
page can rerun or apply that specific preview. Its fingerprint includes the
library root, parameters, operation/config/registry files, and reviewed input
reports; report inputs are copied into job-local snapshots. UI jobs and direct
UI mutations invalidate older previews, but changes made by independent scripts
or other programs are not tracked: review a fresh preview immediately before
applying. Legacy script operations are not undoable through the UI.

On Linux, the UI checks for an already-running `06_analyze.py` and blocks
feature-store jobs and library-writing actions while it is detected. This is a
best-effort check, not a cross-process lock: do not start a standalone extraction
or other mutating script while UI operations are running. Review status changes
remain available. Scripts that cannot safely honor `ui --music-root` refuse to
run when it differs from `config.MUSIC_ROOT`. Report outputs default to
per-job files under `reports/`, and explicitly supplied report paths must also
remain there.

After extraction, Operations also offers **Backfill missing audio hashes** and
**Backfill missing or stale embeddings**. Preview lists candidate counts and a
sample of paths without computing features or changing the feature store.
Apply requires the matching preview, runs the existing recommender backfill on
a temporary copy, and atomically replaces the store only after successful
validation. Missing paths are reported and skipped. Other hash or retryable
embedding failures prevent publication; embeddings may download model files.
The optional maximum limits an embedding run, and these changes are not
undoable. Rebuild insights and the player export after a successful backfill.

The UI also checks for standalone `07_recommend.py` backfills before
feature-store jobs and library writes. These Linux process checks and the
preview's file identity, size, and timestamp checks are best effort, not locks
or content hashes; do not start independent extraction, backfills, or library
mutations concurrently. Cancellation before publication leaves the original
store intact; a late cancellation can occur after replacement. Forced
termination may leave a temporary `.feature-backfill-*` directory.
The standalone analytics command checks for both extraction and backfills;
the standalone export command still checks only for extraction.

For a guarded standalone hash backfill, run
`python scripts/run_recommend_backfill.py --hashes --dry-run`, then use the
preview's `sha256` with
`python scripts/run_recommend_backfill.py --hashes --no-dry-run --expected-plan SHA256`.
For embeddings, substitute `--embeddings` and optionally add
`--max-embeddings N` to both commands.

The Reports page browses files under `reports/`, with searchable listings,
sortable/paginated JSON tables, a raw view, safe image previews, and downloads.
Only text up to 5 MiB is rendered in the browser; symbolic links are omitted
from the listing. Job pages link to available report inputs and outputs. Online
Beets-match reports and evidence-bearing BPM dry-run reports can start a
reviewed-report **preview**; applying still requires the matching successful
job preview. The UI snapshots the reviewed report bytes and refuses changed
reports. BPM reports also require unchanged source identity, BPM, and genre
evidence; rerun old audits that lack those fields. This is a conservative stat
and tag check, not a cryptographic proof against all external changes.

The Insights page reads only a separate SQLite analytics snapshot, never the
live feature pickle during a page request. After extraction finishes, preview
**Build insights snapshot** in Operations, then apply that successful preview.
The snapshot is atomically replaced and does not modify the source feature
store; rebuild it after extraction to see newer results. Counts are per unique
`audio_hash`, reflect the feature-store snapshot rather than a live scan, and
exclude rows without a hash. Genre history uses valid release years and reports
undated tracks separately. The schema breakdown shows observed versions and
recorded extraction errors, not an assertion that all tracks are current. A
freshness badge compares file identity, size, and modification time, not file
contents.

The Musical features page visualizes recorded BPM, musical keys, integrated
loudness, and an energy/valence heatmap. Each chart reports coverage and omitted
tracks. Tempo, key, and mood are estimates; learned scores are model-derived
rather than calibrated mood labels. Older snapshots still show tempo, key, and
loudness, but need rebuilding for learned descriptors.

Analysis health lists recorded extraction errors, truncated or silent signals,
and tracks excluded from similarity, with searchable, paginated snapshot
evidence. Categories can overlap; silent or short tracks do not necessarily
need repair. This is separate from the dismissible Library Health queue. Use
the linked extraction or embedding-backfill operations to review a preview
before making changes, then rebuild the snapshot.

The similarity explorer searches snapshot tracks and ranks neighbors by cosine
similarity using the player's weighted vector recipe and learned descriptors.
It reports eligible and excluded tracks, offers audio previews inside the
configured library root, and is also linked from the Library page. Paths and
tags reflect the snapshot; a file may have moved or changed. The in-memory
index is rebuilt when the snapshot file changes, with a 256 MiB vector limit
and a 300,000-track limit. The explorer warns if its stored recipe no longer
matches the current one; its full-precision scores can differ slightly from
the player's quantized scores.
Snapshots from the previous schema still show the dashboard but must be
rebuilt for similarity. The preview counts eligible tracks; applying also
computes vectors, requires `onnxruntime` from the `audio` extra, and may
download SHA-256-verified descriptor model heads.

The equivalent standalone commands are
`python scripts/build_analytics.py --dry-run` and, after reviewing the preview,
`python scripts/build_analytics.py --no-dry-run`. Both refuse to read while an
already-running `06_analyze.py` or `07_recommend.py` backfill is detected.
These checks are best effort, so do not start either concurrently. Only use a
trusted feature pickle.

After extraction finishes, Operations can also **Export features for the
player**. Its preview builds and validates a temporary export without replacing
the current output. Apply requires that preview and atomically publishes the
validated JSON; the feature store is never changed. The default destination is
`features_export.json.gz`; alternate UI destinations must be `.json` or
`.json.gz` files under `reports/exports/`. Preview and apply can download
SHA-256-verified descriptor model heads if they are missing.

The export job reports the track and exclusion counts and checks the current
player contract, including vector dimensions, identity, descriptor metadata,
and finite values. The Operations page shows freshness for the last successful
UI export and its job page can download the current output. Freshness compares
file identity and timestamps, not content hashes, so it cannot detect every
external change; a missing log or replaced file is reported explicitly. Exports
created directly by the CLI are not included in this status. Transfer the
export to the player host separately; this action does not sync audio or
refresh the player.

The equivalent standalone commands are
`python scripts/export_features.py --dry-run` and, after reviewing the preview,
`python scripts/export_features.py --no-dry-run`. These also accept
`--features PATH` and `--output PATH` and refuse to read while an already-running
extractor is detected. The UI additionally fingerprints the source and output
for its preview/apply gate. **Validate library guardrails** exposes
`04_move.py --validate-config` as a read-only operation.

**Mirror library to player storage** runs `scripts/server_sync.sh` through a
guarded preview/apply workflow. The source must be the complete library or a
complete USB mirror, never a partial batch: `--delete-before` removes
destination paths missing from it. The form requires an explicit source,
destination, and complete-source confirmation. It refuses empty sources,
overlapping paths, filesystem roots, protected local library/repository/home
roots, and literal loopback SSH destinations. Other host aliases cannot be
reliably identified as local. Alternate remote destinations use
`user@host:/absolute/path`; configure non-interactive SSH authentication first.

The preview logs itemized transfers and deletions. Before applying, the worker
repeats the dry-run and refuses to proceed if the plan or checked root
identities changed. The plan compares rsync's change lines, sizes, and times,
not file content hashes. These checks cannot prove a mirror is complete or
prevent independent changes between preflight and rsync; do not mutate either
side during a sync. A sync is not atomic, and cancellation or failure may
leave a partially updated destination. After a successful sync, refresh the
player on the **remote host**; the UI never performs a local player refresh.

For a guarded standalone sync:

```bash
python scripts/run_server_sync.py --source /path/to/complete-mirror \
  --dest user@host:/path --complete-source --dry-run
# Inspect the itemized output and use the preview's sha256:
python scripts/run_server_sync.py --source /path/to/complete-mirror \
  --dest user@host:/path --complete-source --no-dry-run --expected-plan SHA256
```

The original shell script remains available for existing workflows and does
not enforce this UI guard.

The tagging and movement scripts commonly support these flags; consult each
script's `--help` for its exact options and defaults:

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

Modes are mutually exclusive. Common legacy and migration modes include:

```bash
# Route new arrivals from INCOMING_FOLDER to staging
python3 04_move.py --intake --dry-run
python3 04_move.py --intake --no-dry-run

# Migrate a legacy genre folder to Tier 1 artist roots
python3 04_move.py --restructure --dry-run --folder rock
python3 04_move.py --restructure --no-dry-run --folder rock

# Promote reviewed albums from staging to genre folders
python3 04_move.py --promote --dry-run
python3 04_move.py --promote --no-dry-run
```

Phase 4 additions in `config.py`:

- `GENRE_REMAP_RULES`: map incoming genres to local taxonomy before routing/writing.
- `PREFERENCE_OVERRIDES`: scoped preference labels (`global`, `genre`, `artist`).
  The legacy `artist_folder_threshold` setting is retained for compatibility
  but is not used by the Tier 1 migration.
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
downmix, which read 3–6 dB low for stereo files. `native_peak_dbfs` is the
sample peak of the original channels and can exceed 0 dBFS for decoded overs.
Together they describe the file's mastered level; use them for clipping-safe
playback leveling rather than rewriting audio.

Schema 12 retries audio that decodes substantially shorter than its mutagen
header with ffmpeg, recording `decode_coverage` and `decoder`; audio still
short after retry is marked `signal_status=truncated`. It removes heuristic
Spotify-like proxy columns from the feature store. Learned mood, danceability,
acousticness and vocal/instrumental descriptors are instead derived from
`audio_embedding` by the pinned Essentia heads in `lib/descriptors.py`, and
are versioned independently. `features_export.json.gz` recipe v2 retains the
410-dimensional vector and exports classifier descriptors as logits, plus
`loudness_lufs` and `native_peak_dbfs`. Player rule/filter thresholds should
use library percentiles; the player should level volume rather than
normalizing files. The isolated schema 12 pilot and full test suite pass; the
production migration is the next separate full-library run.

The producer-side feature-export and audio-identity contract is documented in
[docs/player-contract.md](docs/player-contract.md). The player repository
should record only the contract versions and fields it consumes, linking back
to that source.

### intake.py — Full intake in one command

For `INCOMING_FOLDER`, the default flow uses beets with a transient catalog,
optionally enriches covers, then runs `02_rename.py`, `03_folders.py`, and
`04_move.py --to-artist-root`. Use `--pre-tag` to run `01_tag.py` first or
`--legacy-move` for the older `01 → 02 → 04 --intake` staging flow.

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

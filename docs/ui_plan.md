# Cratedigger Web UI — Plan & Task Steps

> Tracks the implementation plan for the local FastAPI + Jinja2 web UI (`cratedigger ui` / `cratedigger/web/`).
> See [roadmap.md](roadmap.md) for overall project phases and [architecture.md](architecture.md) for folder and tag contracts.

---

## Baseline Already Implemented (Phase 2 + Phase 3 Wiring)

The foundation in `cratedigger/web/` (`app.py`, `services.py`, `templates/`, `static/`) already provides:

- **App shell & safety**
  - Top navigation with live SQLite badge counts (`actionable`, `health`, `duplicates / quarantine`, `undoable`)
  - Localhost-only binding, Host and Origin checks, and session CSRF tokens for POST requests
  - Read-only `music_root` mount detection (`_is_music_root_readonly`) with persistent warning banner
  - Consistent flash messaging (`msg` / `err`) across POST redirects
  - Path-traversal guard (`resolve_safe_path`) on `/media/image`, `/media/audio`, quarantine, and undo operations
- **Review Queue (`/review`)**
  - Status tabs (`pending`, `resolved`, `auto_resolved`, `dismissed`), reason pills, and substring search
  - Folder-grouped `no_genre` view with single-submit genre propagation (`/review/group-genre`)
  - Interactive resolvers for `ambiguous_cover_art` (side-by-side preview + `pick-cover`), `rename_conflict` (`resolve-conflict`), inline tag editing (`write-tags`), folder-value propagation (`apply-folder-tag`), and bulk `dismiss` / `resolve` / `apply_genre` / `reopen`
- **Library Health (`/health`)**
  - Passive telemetry notice queue with status/reason filters and bulk dismiss/reopen
- **Quarantine & Duplicates (`/quarantine`)**
  - `audio_hash` duplicate group viewer with size/canonical/missing badges and one-click quarantine + playlist relinking (`/quarantine/dedup`)
  - `0quarantine/` browser with per-file restore (`/quarantine/restore`) and empty-quarantine confirmation (`/quarantine/empty`)
- **Undo Journal (`/undo`)**
  - Reversible run history from `undo_journal` delegated to `cratedigger.engine.executor.undo_plan_run`
- **Library (`/library`)**
  - Read-only, paginated SQLite index with search and artist/album filters; the index is refreshed separately with `cratedigger sync-features`
- **Operations (`/ops`) and Jobs (`/jobs`)**
  - Allowlisted subset of scripts with typed parameters, preview/apply modes, persisted workers, read/write resource locks, cancellation, and resumable log streaming
  - Preview fingerprints cover effective roots, operation/config/registry files, and snapshotted reviewed reports; a job can be rerun or applied from its detail page
  - Direct UI file mutations share resource locks with jobs; external extraction detection is best effort, and external filesystem changes are not tracked by the preview gate
  - Legacy operations are not undoable and unsupported library-root overrides are rejected rather than sent to the wrong library

---

## Next UI Plan Steps

The Phase 3 unified engine (`cratedigger/engine/` and `cratedigger/config_schema.py`) currently exposes `index-artists`, `scan`, `plan`, `apply`, and `undo` via CLI only. The steps below bring those capabilities and remaining daily workflows into the Web UI.

### Step 1: Scan, Plan Preview & Apply (`/plans`)

Expose `scan_scope()`, `build_plan()`, and `execute_plan()` in the browser so dry-run diffs can be inspected before any file or tag mutation.

- [ ] **1.1 Navigation & Plan Index (`GET /plans`)**
  - Add `Plans` tab to `base.html` navigation with draft/applied badge count
  - List recent rows from `plans` table (`id`, `scope`, `status`, summary counts, `created_at`, `applied_at`)
- [ ] **1.2 Scan & Plan Builder Form (`POST /plans/create`)**
  - Scope selector with quick presets (`incoming/`, letter buckets `A`–`Z`, `#`, `0compilations/`, `0random/`, or custom subpath validated under `music_root`)
  - Checkboxes for `--online` (MusicBrainz / Beets no-write lookup) and `--effnet` (Discogs-EffNet genre fallback)
  - Optional read-only "Quick Scan Only" view (`scan_scope()`) showing detected unit kinds (`album`, `compilation`, `single`, `exempt_mix`, `random_single`, `soundtrack`) and track counts before building a full plan
- [ ] **1.3 Plan Diff & Preview Page (`GET /plans/{plan_id}`)**
  - Render summary breakdown by `action_type` (`tag_update`, `move`, `rename`, `transcode`, `cover_fetch`, `playlist_sync`, etc.) and any newly queued `review_items`
  - Filterable table of `plan_actions` showing `source_path` → `target_path`, tag diff (`before` vs `after`), and status (`pending`, `applied`, `skipped`, `failed`)
  - Allow toggling individual `plan_actions` to `skipped` before execution (`POST /plans/{plan_id}/toggle-action`)
- [ ] **1.4 Dry-Run & Live Execution (`POST /plans/{plan_id}/execute`)**
  - "Dry-Run Check" button (`dry_run=True`) and explicit "Apply Plan" confirmation button (`dry_run=False`)
  - Display execution summary and link directly to the generated `run_id` in `/undo`

### Step 2: Intake Workbench (`/intake`)

Provide a dedicated workflow for new arrivals in `settings.incoming_folder`.

- [ ] **2.1 Incoming Folder Inspector (`GET /intake`)**
  - Scan `settings.incoming_folder` on load and list pending release units, file formats (flagging `.wma`/`.mpc`/`.ogg`/`.opus` transcode candidates), and cover-art readiness
- [ ] **2.2 One-Click Intake Plan (`POST /intake/plan`)**
  - Build a scoped plan for `incoming_folder` (defaulting to `online=True` toggleable) and redirect to `/plans/{plan_id}`
  - Show linked `Intake_*` playlist preview (`auto_update_intake_playlist`) and surface any blocking review items inline

### Step 3: Settings & Layout Presets (`/settings`)

Expose `cratedigger.config_schema` (`Settings`, `PRESETS`, `load_settings`, `save_settings`, `apply_preset`) stored in SQLite `settings`.

- [ ] **3.1 Settings Form (`GET /settings`, `POST /settings`)**
  - Group inputs into the 5 schema sections:
    1. **Paths**: `music_root`, `incoming_folder`, `quarantine_folder`, `playlists_folder`, `server_sync_dest`
    2. **Layout Preset & Templates**: `preset_name`, `letter_bucketing`, `article_strip`, `symbol_bucket`, `album_template`, `compilation_template`, `single_template`, `exempt_mix_folders`, `dissolve_soundtracks`, `redistribute_random`
    3. **Small Releases & Curation**: `small_release_threshold`, `long_track_minutes`, `auto_update_singles_playlists`, `auto_update_intake_playlist`, `intake_playlist_prefix`
    4. **Audio & Artwork**: `transcode_extensions`, `transcode_bitrate`, `cover_max_dim`, `embed_single_covers`
    5. **Genre Taxonomy & Thresholds**: `genre_remap_rules`, `effnet_auto_threshold`, `effnet_review_threshold`, `consensus_auto_threshold`
- [ ] **3.2 Preset Switcher & Live Template Preview (`POST /settings/preset`)**
  - Apply named presets (`player_optimal_tier1`, `strict_albums_only`, `flat_artist_root`)
  - Render live sample paths for an album, compilation, and single using the edited templates before saving
- [ ] **3.3 Validation**
  - Guard against invalid path templates, empty `quarantine_folder`, or out-of-range thresholds (`0.0`–`1.0`) with inline `err` flash feedback

### Step 4: Artist Index & Canonical Name Manager (`/artists`)

Surface `cratedigger.core.artists` (`build_artist_index`, `snap_artist_name`, `split_featured_artists`) and the SQLite `artists` table.

- [ ] **4.1 Canonical Artist Index Browser (`GET /artists`)**
  - Filter by letter bucket (`A`–`Z`, `#`) and search by `canonical_name` or `comparison_key`
  - Show `folder_path`, `mb_artistid`, and track/album counts from `tracks`
- [ ] **4.2 Re-index & Collision Preview (`POST /artists/reindex`)**
  - Trigger `build_artist_index()` and report indexed count
  - Flag artist pairs sharing `comparison_key` or `mb_artistid` so duplicate Tier 1 folders can be inspected before merging

### Step 5: Library, Playlists & Feature Sync Views (`/library`, `/playlists`)

Give visibility into the indexed SQLite catalog (`tracks`, `track_paths`) and `.m3u8` outputs.

- [ ] **5.1 Catalog Browser (`GET /library`)**
  - Paginated search over `tracks` (artist, album, title, genre, year, `audio_hash`, `mb_trackid`)
  - Quick filter chips: missing genre, missing `mb_trackid`, transcode candidates (`.wma`/`.mpc`/`.ogg`/`.opus`), and tracks missing `features.pkl` entries
  - Trigger `sync-features` (`sync_features_into_db`) from the UI (`POST /library/sync-features`)
- [ ] **5.2 Playlist Viewer (`GET /playlists`)**
  - List `.m3u8` files under `settings.playlists_folder`, track counts, broken relative entries, and `Intake_*` / genre singles playlists

### Step 6: Background Jobs, Audio Preview & Test Coverage

Ensure longer operations remain responsive and regressions are caught automatically.

- [ ] **6.1 In-Process Background Job Runner (`GET /jobs/{job_id}`)**
  - Run slow operations (`plan` with `--online`/`--effnet`, large `apply` with `ffmpeg` transcoding, `index-artists`) in a background thread/task with progress state in memory/SQLite and auto-refresh polling
- [ ] **6.2 Review Ergonomics**
  - Add inline HTML5 `<audio>` preview on review cards with missing/suspicious tags or rename conflicts (using existing `/media/audio`)
  - Add keyboard navigation (`j`/`k` item focus, quick dismiss/resolve) in `static/app.js`
- [ ] **6.3 Automated Web UI Tests (`tests/test_web_ui.py`)**
  - Add FastAPI `TestClient` tests for `/plans`, `/intake`, `/settings`, `/artists`, `/library`, `/playlists`, and background job polling
  - Verify dry-run default safety, `resolve_safe_path` enforcement, and read-only `music_root` error handling on all new routes

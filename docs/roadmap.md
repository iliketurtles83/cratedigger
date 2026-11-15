# Roadmap

## Current Status

**Core pipeline, SQLite operational store, unified engine, and initial FastAPI Web UI are implemented**:
- Legacy scripts (`01_tag.py`–`07_recommend.py`, `intake.py`, and repair utilities in `scripts/`) handle tagging, renaming, normalization, cover enrichment, duplicate quarantine, playlist generation, feature extraction (`features.pkl`), and recommendation export (`features_export.json.gz`).
- The `cratedigger` package adds a SQLite operational database (`cratedigger.db`), `cratedigger` CLI (`db-init`, `sync-features`, `migrate-review`, `status`, `ui`, `index-artists`, `scan`, `plan`, `apply`, `undo`), a local FastAPI review/health/quarantine/undo Web UI, and the Phase 3 unified scan/plan/apply/undo engine.
- Always validate with `--dry-run` first; see [ui_plan.md](ui_plan.md) for the remaining Web UI work.

---

## Migration State

Manual pre-migration completed May 2026:

- All compilations moved to `0compilations/`
- All various artists moved to `0various/`
- All subgenre folders flattened; genre folders now contain artist folders and album folders directly
- Existing singles folders renamed to `0singles/` within artist folders where they existed
- `0random/` preserved with per-genre subfolders

Current on-disk shape before pipeline runs:

```
genre/artist/artist - album          # needs restructure to artist-root
genre/artist - album                 # needs restructure to artist-root
genre/artist/0singles/               # needs restructure to artist-root
0random/genre/                       # kept as-is, local_special
```

Pipeline Phase 1 will formalize `classify_folder()` against the Tier 1 contract and begin restructuring genre-root folders to artist-root.

---

## Known Limitations

### Tag Support
- **BPM detection unavailable for M4A/AAC files** — librosa analysis is supported, but the BPM tag itself has no standard ID3 equivalent in the M4A spec. Workaround: store in custom frame or external feature store (planned for 06_analyze.py).
- **No deduplication by ISRC** — files with identical ISRC tags are not detected or merged. Planned for post-tagging pass.
- **Genre hierarchy not enforced in tags** — parent and subgenre are slash-separated; no semantic validation of genre relationships.

### Folder Structure
- **Only 3-level artist threshold (N=3)** — `--restructure` mode applies a fixed threshold. Configurable per-genre thresholds not yet supported.
- **No automatic orphan cleanup** — empty folders are not deleted; must verify and delete manually.
- **Compilation detection heuristic-based** — inferred from artist count and albumartist tag; ambiguous cases flagged for review rather than auto-resolved.

### External Services
- **MusicBrainz and AcoustID optional but recommended** — without them, only filename parsing and folder context are available for tagging. Fingerprinting is the primary lookup method; tag-based MB queries are not implemented.
- **No fallback to streaming services** — Spotify, Apple Music, etc. not consulted for metadata enrichment.
- **Rate limiting is basic** — single global 1.1s delay per MB request; no adaptive backoff or queue.

### Testing and CI
- **Automated unit & integration tests** — `tests/` currently covers parsers, context, tags/artwork, MusicBrainz fallbacks, recommender, SQLite migration, Web UI routes, and the Phase 3 unified engine (`python -m unittest discover -s tests`).
- **No linting or type checking in CI** — code is not yet gated by `mypy` or `ruff` in automation.
- **No GitHub Actions** — no automated CI pipeline on PR or commit.
- **Real audio tests are opt-in smoke tests** — gated by `CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1`; they depend on the local library and do not mutate files.

### User Experience
- **Web UI covers triage & rollback, not planning/settings yet** — `/review`, `/health`, `/quarantine`, and `/undo` are live in `cratedigger ui`, while `scan`/`plan`/`apply`, `settings`, `intake`, and `artists` currently require the CLI (tracked in [ui_plan.md](ui_plan.md)).
- **Undo coverage** — operations executed through the Web UI and `cratedigger` engine (`execute_plan`) are recorded in `undo_journal` and reversible via `undo` / `/undo`; standalone legacy scripts still mutate directly after `--dry-run`.

---

## Finalized Decisions

### Canonical top-level structure: artist-root with letter bucketing (closed May 2026)

The library uses artist-root with letter bucketing as the Tier 1 canonical shape:

```
music/
  0compilations/ → 0various/ → 0mixes/ → 0singles/ → 0random/genre-name/
  #/ → A/ → B/ → ...
    Artist Name/
      Artist Name - Album (Year)/
      0singles/
```

Letter bucketing: articles stripped for bucketing only (`The Cure` → `C/`); symbols/numbers → `#/`; joint credits → first artist's letter; `Various Artists` → `0various/`. Genre is tag-only, never inferred from path. See Architecture — Folder Contract.

---

## Planned Features

### Phase 1: Folder Contract and Classification Rules — **COMPLETE**

Folder contract is defined (see Architecture — Folder Contract). Implementation tasks in order:

1. ✅ **Implement `SPECIAL_FOLDER_GENRE_POLICY`** — per-folder ordered genre sourcing for `0mixes`, `0random`, etc.; resolves open policy item blocking `01_tag.py` for special folders.
2. ✅ **Implement batch review mode in `05_review.py`** — group flagged items by reason; allow single resolution to apply to all items in group. Minimum viable: show count per reason, allow “apply to all” for unambiguous cases (e.g. all `genre_fallback_applied` flags with same folder context). Blocking pipeline validation at scale.
3. ✅ **Update `classify_folder()`** — distinguish Tier 1 canonical shapes from Tier 2 transitional shapes using the artist-root contract. Tier 1 fast path: no review flags. Tier 2: route to migration path.
4. ✅ **Update `get_folder_context()`** — handle letter-bucket root structure; remove genre-from-path inference for regular albums; `FolderContext.genre` returns `None` for letter-bucket paths.
5. ✅ **Add artist-root routing to `04_move.py`** — destination determined by `artist[0].upper()` after article stripping → letter bucket → artist folder; fully deterministic, no config lookup.

**Phase 1 Exit Criteria** (in order)
2. ✅ Batch review mode implemented — unblocks pipeline validation at scale
3. ✅`classify_folder()` distinguishes Tier 1 from Tier 2 with deterministic shape tests
1. ✅ `SPECIAL_FOLDER_GENRE_POLICY` implemented — unblocks `01_tag.py` for special folders
4. ✅ `get_folder_context()` updated for letter-bucket structure; classification-without-tags rule enforced
5. ✅ Artist-root routing in `04_move.py` passes dry-run on current genre-root library

Repeat dry-runs on canonical (Tier 1) folders must produce zero new review items. Batch review must be able to process a 300-item queue to under 20 manual items. Do not start Phase 2 until all criteria are met.

### Phase 2: Deterministic Metadata and Review Contract — **COMPLETE**

Make tag normalization and review semantics consistent across formats and scripts.

- ✅ **Canonical number fields** — enforce track/disc as `num` or `num/total` across ID3, Vorbis, and M4A tuples.
- ✅ **Canonical BPM policy** — enforce positive integer BPM values where supported; M4A skipped (no standard tag).
- ✅ **Deterministic source priority** — local file/folder context first, external MB/AcoustID only when local evidence is insufficient. Folder-name album/year pre-filled before MB lookup in `01_tag.py`.
- ✅ **Review identity keys** — preserve distinct review findings by payload-aware dedupe keys via `_review_item_key()`, not only `(path, reason)`.
- ✅ **Dry-run parity** — dry-run and live mode share identical decision logic; write side-effect only differs.

**Phase 2 Exit Criteria**
- ✅ Track/disc/BPM normalization is consistent across MP3, Vorbis, and M4A paths.
- ✅ Repeat runs on canonicalized fixtures produce no new review deltas.
- ✅ Review dedupe preserves distinct payload variants on the same path/reason.
- ✅ Dry-run and live mode share decision logic (write side-effect only differs).

### Phase 3: MusicBrainz and AcoustID Throughput Improvements — **COMPLETE**

- ✅ **Fallback strategies** — when AcoustID returns no match above threshold, `01_tag.py` now performs deterministic tag-based MB lookup and logs fallback reason per file
- ✅ **Genre enrichment** — MB genre tags continue to be merged with parent genres (from local taxonomy) for richer normalized genre output
- ✅ **Collaborative tagging** — MB recording search now breaks metadata ties using community tag votes and optional local genre hints
- ✅ **Adaptive rate limiting** — MB calls now use retry/backoff plus adaptive inter-request delay and bounded queueing; requests degrade gracefully when queue wait would exceed limit

**Phase 3 Exit Criteria**
- ✅ Fallback lookup strategy is deterministic and logged per file.
- ✅ API failures degrade gracefully to local-only behavior without aborting folder runs.
- ✅ Rate-limit/backoff behavior is covered by tests with mocked MB responses.

### Phase 4: User Preferences and Hooks — **COMPLETE**

- ✅ **Per-artist thresholds** — restructure mode resolves thresholds by scoped precedence (`artist` > `genre` > `global` > default).
- ✅ **Genre remapping rules** — remapping applies to tag writes and intake routing.
- ✅ **Custom hooks** — configurable pre/post move callbacks run from allow-listed paths with timeout and failure policy handling.
- ✅ **Preference labels** — labels persist in sidecar metadata without changing folder structure.

**Phase 4 Exit Criteria**
- ✅ Preference overrides are config-driven and scoped (global, genre, artist).
- ✅ Hook execution is sandboxed/documented with failure handling semantics.
- ✅ Preference labels do not alter canonical folder contract unless explicitly configured.

### Phase 5: Audio Analysis (06_analyze.py) **COMPLETE**

Extract audio features for a personal recommender model. Runs independently of `01_tag.py` and never mutates audio tags. Results are persisted to a pickled `pandas.DataFrame` (default: `features.pkl`).

| Feature | Library | Status |
|---|---|---|
| BPM (tempo) | librosa | ✅ |
| Key + mode | librosa (Krumhansl-Schmuckler) | ✅ |
| Energy (RMS) | librosa | ✅ |
| Loudness (LUFS) | pyloudnorm | ✅ |
| Spectral centroid | librosa | ✅ |
| Zero crossing rate | librosa | ✅ |
| Danceability (heuristic proxy) | librosa | ✅ |
| Duration (ms) | librosa | ✅ |
| speechiness / acousticness / instrumentalness / liveness / valence | reserved | columns present, populated by Phase 6 |

**Storage:** pickled pandas DataFrame at `config.FEATURES_STORE_PATH`. Primary key: absolute file path. Incremental key: `(mtime, size, schema_version)`. Schema migration: bump `FEATURE_SCHEMA_VERSION` in `06_analyze.py` and rerun — rows with an older version are automatically re-analyzed.

**Phase 5 Exit Criteria**
- ✅ Feature extraction is incremental (skips files whose `mtime`, `size`, and `schema_version` are unchanged).
- ✅ Storage schema is finalized with migration guidance (`FEATURE_SCHEMA_VERSION` + auto-reanalyze on mismatch).
- ✅ Analysis can be rerun independently of tagging without mutating tags (no `write_tags` in `06_analyze.py`).

### Phase 6: Feature Export & Recommender Model — **COMPLETE**

Build a content-based feature export and offline similarity model consumed by the mobile music player app (offline dot product) and home sync server (`GET /library/features?model=<version>`):
- ✅ **Audio hash identity** — `lib/audiohash.py` (`a1:hex`) hashes head/tail audio bitstreams, ignoring tags. Moves and renames never orphan features.
- ✅ **Deep music embeddings** — Discogs-EffNet ONNX model extracts 400-dim style/genre activations, providing semantic musical representation far beyond heuristic proxies.
- ✅ **Baked hybrid vector** — standardizes and scales components ($w_{\text{emb}} \cdot \hat{e} + w_{\text{tempo}} \cdot \hat{t} + w_{\text{harm}} \cdot \hat{h} + w_{\text{scalar}} \cdot \hat{s}$) into an L2-normalized unit vector so phone dot product computes exact weighted similarity offline.
- ✅ **Offline evaluation harness** — `07_recommend.py` provides 20% holdout recall/ranking benchmarks on `0faves`.
- ✅ **Feature export** — `export_features()` serializes tracks, unit vectors, and readable filter scalars to compact `features_export.json.gz`.

**Phase 6 Exit Criteria**
- ✅ Baseline model quality is measurable via offline evaluation metrics (Recall@5/10/20, MRR).
- ✅ Feature pipeline is reproducible and keyed by tag-invariant `audio_hash`.
- ✅ Feature export satisfies phone client schema and payload budget (~5-15 MB across library).

### Phase 7: ISRC Deduplication (deferred)

Post-tagging pass to detect and resolve files with identical ISRC tags (physical duplicates across folders or imports).

- Scan all tags for ISRC collisions
- Propose merges (keep best-tagged version, highest bitrate, earliest add date)
- Flag for manual review if merge is ambiguous
- Remove or move duplicates to a `0duplicates/` folder

**Phase 7 Exit Criteria**
- Collision scan is complete and repeatable across the full library.
- Merge proposal scoring is deterministic and reviewable.
- Ambiguous merges are routed to review with enough context to decide quickly.

### Phase 8: Fixes found by using music-player (2026-10-13) — **COMPLETE**

Found while browsing the library in music-player; details and counts are in [cratedigger-feedback.md](cratedigger-feedback.md).

1. ✅ **Cover art under other names → `cover.jpg`** — `scripts/normalize_covers.py` and `scripts/enrich_covers.py` normalize/enrich album cover art and embed single-track covers without altering audio hashes.
2. ✅ **Placeholder tags are empty tags** — `01_tag.py` and `02_rename.py` treat CD-ripper placeholders (`New Artist`, `Track 01`, `Album Title`, `Unknown`, etc.) as missing and refuse to write placeholder filenames.
3. ✅ **Curated lists converted to `.m3u8` playlists** — `scripts/convert_lists.py` and `scripts/retag_lists.py` retag list tracks to their real releases and emit relative `.m3u8` playlists.

**Phase 8 Exit Criteria**
- ✅ A dry run lists every cover rename and every ambiguous folder; a live run leaves no album folder with a lone, differently named image.
- ✅ No track in the library keeps a placeholder album, artist or title.

---

## Cratedigger 2.0: Operational Store, Unified Engine & Web UI

### Engine Phase 1: SQLite Store & Core Package — **COMPLETE**
- ✅ Added `cratedigger` package (`pyproject.toml`, `cratedigger/db.py`, `cratedigger/cli.py`, `cratedigger/review_migration.py`).
- ✅ Added WAL-mode SQLite schema (`tracks`, `track_paths`, `artists`, `review_items`, `plans`, `plan_actions`, `undo_journal`, `mb_cache`, `settings`).
- ✅ Added `db-init`, `sync-features` (`features.pkl` import), `migrate-review` (with >=75% consensus auto-triage), and `status` CLI commands.

### Engine Phase 2: Web UI Foundation (Review, Health, Quarantine & Undo) — **COMPLETE**
- ✅ Added FastAPI + Jinja2 Web UI (`cratedigger ui`) in `cratedigger/web/`.
- ✅ Implemented `/review` (status/reason filters, folder-grouped `no_genre`, cover-art picker, rename-conflict resolver, inline tag edits, bulk actions), `/health`, `/quarantine` (`audio_hash` dedup + playlist relink, restore, empty), `/undo`, and guarded `/media/image` & `/media/audio` endpoints.
- ✅ Added read-only `music_root` mount warning banner and flash `err`/`msg` redirect handling.

### Engine Phase 3: Unified Scan / Plan / Apply / Undo Engine & Settings Schema — **COMPLETE**
- ✅ Added SQLite-backed `Settings` and layout presets (`cratedigger/config_schema.py`).
- ✅ Added `cratedigger/core/artists.py` for featured-credit splitting and canonical Tier 1 artist index snapping.
- ✅ Added `cratedigger/engine/` (`scanner.py`, `metadata.py`, `artwork.py`, `transcoder.py`, `playlists.py`, `planner.py`, `executor.py`) supporting deterministic plan persistence, `ffmpeg` transcoding with tag/rating preservation, and journaled undo (`undo_plan_run`).
- ✅ Exposed `index-artists`, `scan`, `plan`, `apply`, and `undo` CLI subcommands and delegated Web UI `/undo` to `undo_plan_run`.

### Engine Phase 4: Web UI Completion (Plans, Intake, Settings & Library Workflows) — **PLANNED**
- Expose the Phase 3 engine, settings presets, artist index, intake workbench, catalog/playlist browsers, and background job tracking in the browser.
- **Detailed task checklist:** see [docs/ui_plan.md](ui_plan.md).

---

## Breaking Changes (Planned)

### No breaking changes currently anticipated
Current config.py and folder structure are stable. Future breaking changes would be announced here with migration guidance.

---

## Deprecated Patterns

None yet. All code follows current conventions.

---

## Testing Strategy

### Current
- `tests/` contains 40 test files covering helpers (`test_parsers.py`, `test_context.py`, `test_genres.py`, `test_audiohash.py`), tagging/artwork/pipeline (`test_tag_pipeline.py`, `test_tags_artwork.py`, `test_mb_phase3.py`), repair/curation scripts, recommender (`test_recommender.py`), SQLite/migration (`test_db_and_migration.py`), Web UI (`test_web_ui.py`), and Phase 3 engine (`test_engine_phase3.py`).
- `test_real_audio.py` — opt-in read-only smoke tests using real library (`CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1`).

### Planned
- **Web UI Phase 4 tests** — `TestClient` coverage for `/plans`, `/intake`, `/settings`, `/artists`, `/library`, `/playlists`, and background jobs (see [ui_plan.md](ui_plan.md)).
- **Regression tests** — snapshots of known-good metadata for specific albums.
- **Linting & CI** — `ruff` and `mypy` checks in automated CI.

---

## Development Notes

### Architecture Stability

The core pipeline (01→02→03→04) is stable. Changes are localized:
- `lib/context.py` — folder classification, no breaking changes expected
- `lib/tags.py` — format-agnostic wrappers, minor API additions for new tag fields
- `lib/mb.py` — external API integration, subject to MusicBrainz rate-limit changes
- Main scripts — orchestration logic, safe to refactor without affecting others

### Why 06_analyze.py is Separate

Audio analysis and tagging are independent concerns. Keeping them separate:
- Allows re-running analysis with better models without touching tags
- Supports external storage (not all features fit in ID3/Vorbis comments)
- Enables incremental feature extraction (process new files only)
- Simplifies testing (no need to re-tag to test analysis)

### Contributing

For contributors:
1. Always use `--dry-run` first on a test folder
2. Follow conventions in `docs/architecture.md` (especially file ownership, lib/ modularity)
3. Run `python -m unittest discover -s tests` before submitting
4. Document changes in comments; avoid magic numbers
5. Import order: stdlib → third-party → local (config, then lib/)
6. Use pathlib.Path always; never os.path

---

## Future Possibilities (Not Committed)

- **Streaming sync** — export playlist suggestions to Spotify/Apple Music
- **Mobile client** — companion app for mobile playback and preference sync
- **Collaborative recommendations** — share taste with friends, blend playlists
- **Real-time watcher** — auto-process new files as they arrive in `INCOMING_FOLDER`
- **Audio quality metrics** — bitrate, sample rate normalization, codec efficiency

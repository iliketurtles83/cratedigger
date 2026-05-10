# Roadmap

## Current Status

**Pipeline is stable for core operations**: tagging, renaming, folder normalization, and intake routing all work well in `--dry-run` and production modes. Testing is ongoing; use with caution and always validate with `--dry-run` first.

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
- **No automated test suite** — unit tests exist for helper logic only (`test_parsers.py`, `test_context.py`). Integration tests are manual.
- **No linting or type checking** — code is written for readability and correctness but not validated by mypy, pylint, or black.
- **No GitHub Actions** — no CI pipeline, no automated validation on PR or commit.
- **Real audio tests are read-only smoke tests** — they depend on your local library and do not write anything.

### User Experience
- **Logging is console + file only** — no structured logging, metrics collection, or audit trail.
- **No undo mechanism** — file moves and renames are performed directly; no rollback on error.

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

### Phase 1: Folder Contract and Classification Rules — **ACTIVE**

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

### Phase 2: Deterministic Metadata and Review Contract

Make tag normalization and review semantics consistent across formats and scripts.

- **Canonical number fields** — enforce track/disc as `num` or `num/total` across ID3, Vorbis, and M4A tuples.
- **Canonical BPM policy** — enforce positive integer BPM values where supported.
- **Deterministic source priority** — local file/folder context first, external MB/AcoustID only when local evidence is insufficient.
- **Review identity keys** — preserve distinct review findings by payload-aware dedupe keys, not only `(path, reason)`.
- **Dry-run parity** — ensure dry-run and live mode evaluate identical decision paths.

**Phase 2 Exit Criteria**
- Track/disc/BPM normalization is consistent across MP3, Vorbis, and M4A paths.
- Repeat runs on canonicalized fixtures produce no new review deltas.
- Review dedupe preserves distinct payload variants on the same path/reason.
- Dry-run and live mode share decision logic (write side-effect only differs).

### Phase 3: MusicBrainz and AcoustID Throughput Improvements

- **Batch fingerprinting** — fingerprint N files in parallel, queue lookups to respect rate limits
- **Fallback strategies** — if AcoustID score < 0.8, try tag-based lookup as secondary signal
- **Genre enrichment** — consult MusicBrainz genre taxonomy for parent/child relationships, auto-fill subgenre when available
- **Collaborative tagging** — use MB's community-voted genre tags as a tie-breaker for ambiguous metadata
- **Adaptive rate limiting** — add retry/backoff and bounded queueing for MB failures and transient throttling

**Phase 3 Exit Criteria**
- Batch lookup path meets configured throughput without violating MB limits.
- Fallback lookup strategy is deterministic and logged per file.
- API failures degrade gracefully to local-only behavior without aborting folder runs.
- Rate-limit/backoff behavior is covered by tests with mocked MB responses.

### Phase 4: User Preferences and Hooks

- **Per-artist thresholds** — override N=3 for specific artists (e.g., prolific producer gets higher threshold)
- **Genre remapping rules** — transform incoming tags to local folder scheme (e.g., "IDM" → "Electronic")
- **Custom hooks** — user-defined callbacks before/after move operations
- **Preference labels** — tag files as "love", "skip", "study", etc. without modifying folder structure

**Phase 4 Exit Criteria**
- Preference overrides are config-driven and scoped (global, genre, artist).
- Hook execution is sandboxed/documented with failure handling semantics.
- Preference labels do not alter canonical folder contract unless explicitly configured.

### Phase 5: Audio Analysis (06_analyze.py)

Extract audio features for a personal recommender model. Run after `01_tag.py`; store results externally (not in audio tags).

| Feature | Library | Storage | Purpose |
|---|---|---|---|
| BPM | librosa | audio tag (done) | Already in 01_tag.py |
| Key | librosa | audio tag (new) | Harmonic compatibility |
| Energy | librosa | features.json or sqlite | Mood signal |
| Loudness (LUFS) | librosa | features.json or sqlite | Normalization anchor |
| Spectral centroid | librosa | features.json or sqlite | Timbre characterization |
| Zero crossing rate | librosa | features.json or sqlite | Roughness/noise signal |
| Danceability | librosa | features.json or sqlite | Groove signal |

**Decision pending:** JSON lines (one feature set per file), JSON structure (nested by artist), or lightweight sqlite (indexed by ISRC).

**Phase 5 Exit Criteria**
- Feature extraction is incremental (new/changed files only).
- Storage schema is finalized with migration guidance.
- Analysis can be rerun independently of tagging without mutating tags.

### Phase 6: Recommender Model

Build a content-based + collaborative filtering hybrid model using:
- Multi-genre tags (not flattened)
- Audio features from Phase 5
- Artist co-occurrence graph
- User preference signals (0faves, 0random_good, etc.)
- Positive examples: liked files, frequently played
- Negative examples: skipped, in 0random_bad (if added)

**Storage:** sqlite or DuckDB. **Interface:** CLI or TUI for "play something like this."

**Phase 6 Exit Criteria**
- Baseline model quality is measurable via offline evaluation metrics.
- Feature pipeline is reproducible from tagged files + analysis store.
- CLI/TUI interface supports deterministic query inputs and explainable outputs.

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
- `test_parsers.py` — filename and folder name parsing
- `test_context.py` — folder classification and context inference
- `test_real_audio.py` — read-only smoke test using real library (opt-in)

### Planned
- **Integration test suite** — safe, repeatable tests on test folders with synthetic audio
- **Mock MB/AcoustID** — test tagging logic without external API calls
- **Regression tests** — snapshots of known-good metadata for specific albums
- **Linting** — ruff or pylint for code quality
- **Type checking** — mypy with strict mode

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
- **Web UI** — lightweight Flask/FastAPI server for browsing and reviewing
- **Mobile client** — companion app for mobile playback and preference sync
- **Collaborative recommendations** — share taste with friends, blend playlists
- **Real-time watcher** — auto-process new files as they arrive in INCOMING_FOLDER
- **Format migration** — detect and alert on obsolete/lossy formats
- **Audio quality metrics** — bitrate, sample rate normalization, codec efficiency

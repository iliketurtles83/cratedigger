# Roadmap

## Current Status

**Pipeline is stable for core operations**: tagging, renaming, folder normalization, and intake routing all work well in `--dry-run` and production modes. Testing is ongoing; use with caution and always validate with `--dry-run` first.

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
- **`05_review.py` is fully interactive** — no batch mode for automated resolution of flagged files. Large flagging backlogs require manual review.
- **Logging is console + file only** — no structured logging, metrics collection, or audit trail.
- **No undo mechanism** — file moves and renames are performed directly; no rollback on error.

---

## Planned Features

### Phase 1: Improved MusicBrainz Integration

- **Batch fingerprinting** — fingerprint N files in parallel, queue lookups to respect rate limits
- **Fallback strategies** — if AcoustID score < 0.8, try tag-based lookup as secondary signal
- **Genre enrichment** — consult MusicBrainz genre taxonomy for parent/child relationships, auto-fill subgenre when available
- **Collaborative tagging** — use MB's community-voted genre tags as a tie-breaker for ambiguous metadata

### Phase 2: User Preferences and Hooks

- **Per-artist thresholds** — override N=3 for specific artists (e.g., prolific producer gets higher threshold)
- **Genre remapping rules** — transform incoming tags to local folder scheme (e.g., "IDM" → "Electronic")
- **Custom hooks** — user-defined callbacks before/after move operations
- **Preference labels** — tag files as "love", "skip", "study", etc. without modifying folder structure

### Phase 3: Audio Analysis (06_analyze.py)

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

### Phase 4: Recommender Model

Build a content-based + collaborative filtering hybrid model using:
- Multi-genre tags (not flattened)
- Audio features from Phase 3
- Artist co-occurrence graph
- User preference signals (0faves, 0random_good, etc.)
- Positive examples: liked files, frequently played
- Negative examples: skipped, in 0random_bad (if added)

**Storage:** sqlite or DuckDB. **Interface:** CLI or TUI for "play something like this."

### Phase 5: ISRC Deduplication (deferred)

Post-tagging pass to detect and resolve files with identical ISRC tags (physical duplicates across folders or imports).

- Scan all tags for ISRC collisions
- Propose merges (keep best-tagged version, highest bitrate, earliest add date)
- Flag for manual review if merge is ambiguous
- Remove or move duplicates to a `0duplicates/` folder



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

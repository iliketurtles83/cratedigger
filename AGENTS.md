# AGENTS.md

This file helps OpenCode agents avoid mistakes and ramp up quickly. Every line answers: "Would an agent likely miss this without help?"

## Key Commands

- Run tests: `.venv/bin/python -m unittest discover -s tests` (real audio tests: `CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1 .venv/bin/python -m unittest discover -s tests`)
- Dry-run intake pipeline: `python intake.py --dry-run`
- Beets import trial: `.venv/bin/python -m beets -c config/beets.yaml import --pretend /path/to/album`
- Audit unmatched releases with isolated Beets catalogs: `.venv/bin/python scripts/audit_beets_matches.py --bucket Q [--online]`
- Apply reviewed Beets evidence: `.venv/bin/python scripts/apply_beets_matches.py --report reports/... --release-id MB_RELEASE_ID [--fix --no-dry-run]`
- Audit & repair tags: `python scripts/audit_repair.py --dry-run --folder NAME [--fix]`
- Repair folder-parse damage: `python scripts/repair_folder_parse.py --tags --structure --dry-run`
- Audit duplicate artist folders: `python scripts/reconcile_artists.py` (recording evidence for compilation/loose tracks: `--online-recording-evidence --fix`; collision evidence: `--online-release-evidence --collision-report`; apply only reviewed cache-backed sets by adding `--no-dry-run`)
- Repair misidentified tracks: `python scripts/repair_track_identity.py` (fingerprint uncached candidates in a dry-run with `--online-fingerprints`; apply reviewed cache-backed repairs with `--fix --relocate --no-dry-run [--artist NAME]`, then re-run `02_rename.py` on the touched folders)
- Audit & repair Windows paths: `python scripts/repair_windows_paths.py` (apply reviewed case-insensitive merges and invalid-name normalization with `--fix --no-dry-run`)
- Normalize cover art: `python scripts/normalize_covers.py --dry-run [--folder NAME]`
- Enrich missing cover art: `python scripts/enrich_covers.py --dry-run [--folder NAME] [--bucket BUCKET] [--singles]`
- Convert curated lists: `python scripts/convert_lists.py --dry-run --folder "The Pitchfork 500" [--retag]`
- Retag curated lists: `python scripts/retag_lists.py --dry-run --folder "The Pitchfork 500" [--as-compilation]`
- Reconcile 2-tier genres: `python scripts/reconcile_genres.py --dry-run [--report reports/genres/NAME.json] [--fix]`
- Consolidate small/incomplete releases: `python scripts/consolidate_small_releases.py --dry-run [--bucket BUCKET] [--report reports/consolidation/NAME.json] [--fix]`
- Quarantine identical album duplicates: `python scripts/quarantine_album_duplicates.py --dry-run [--fix --no-dry-run]`
- Generate top-level genre playlists for singles: `python scripts/generate_genre_playlists.py --dry-run [--fix --no-dry-run]`
- Standalone tagger: `python 01_tag.py --dry-run --folder jazz` (fast local preview skipping MB/BPM: add `--no-mb --no-bpm`)
- Validate Tier 1 guardrails: `python 04_move.py --validate-config`
- Relocate compilations and loose Various Artists singles: `python 04_move.py --relocate-compilations` (dry-run by default; loose singles move to the primary artist's Tier 1 `0singles`)
- Review flagged items: `python 05_review.py` (interactive: blocks on stdin; never invoke in unattended agent sessions)
- Recommender evaluation & export: `python 07_recommend.py --evaluate` (export: `python 07_recommend.py --export`)

## Architecture & Pipeline Flow

Cratedigger uses **beets** for release-level identity and AcoustID/MB metadata matching. Cratedigger exclusively owns filenames and physical layout:

1. **intake.py** - New arrivals in `incoming/` are matched by beets using a transient catalog, then pass through `scripts/enrich_covers.py`, `02_rename.py`, `03_folders.py`, and `04_move.py --to-artist-root`. The historical persistent Beets DB is not part of this pipeline. (Legacy `01→02→04` staging flow preserved via `--legacy-move`).
2. **scripts/audit_repair.py** - Scans library against backup (`/media/jack/monodong/audio2`) to purge junk folksonomy tags, restore unflattened genres, and protect collaboration credits.
3. **01_tag.py** - Standalone/pre-tagger. Folder genre is only a fallback when existing genres are absent (prevents feedback loops). Genre remapping is opt-in (`--remap-genres`). Guest/collaboration credits are protected during consistency passes.
4. **02_rename.py / 03_folders.py** - File/folder normalizers according to cratedigger rules. `02_rename.py` refuses to write placeholder tags (`New Artist`, `Track 01`) into filenames and flags them instead. `03_folders.py` nests and re-buckets artist roots that hold loose files.
5. **04_move.py** - Legacy staging router and promotion utility.
6. **05_review.py** - Review triage for flagged items.
7. **06_analyze.py** - Audio feature extraction (librosa/pyloudnorm/Discogs-EffNet) keyed by `audio_hash` into `features.pkl`.
8. **07_recommend.py** - Recommender lab, holdout evaluator (20% holdout on `0faves`), and feature exporter (`features_export.json.gz`).

## Architecture Notes

- Folder naming contract: Tier 1 artist-root with letter bucketing (`A-Z`, `#` for symbols)
- Windows path contract: no reserved device names, control characters, `< > : " / \ | ? *`, leading dots, trailing spaces/dots, or case-insensitive sibling collisions
- Special folders: `0compilations`, `0various`, `0mixes`, `0singles`, `0random`, `#`
- Letter bucketing: articles stripped for bucketing only (`The Cure` → `C/The Cure/`)
- Beets config: `config/beets.yaml` (metadata matching only; no path templates or moves). The persistent Beets DB at `/home/jack/.config/beets/library.db` is retained as legacy state and is not consumed or synchronized by the active pipeline. Always invoke beets with `-c config/beets.yaml`.
- Tier 1 always keeps an artist directory, including artists with only one album. The former album-count threshold is retired because it conflicts with this contract.
- Folder context is read-only; scripts import from `lib/context.py`
- Tags are written via `lib/tags.py` (format-agnostic wrapper)
- External dependencies: `fpcalc` (Chromaprint binary) must be available on `$PATH` (e.g. `/usr/bin/fpcalc`) for AcoustID fingerprinting; without it, `pyacoustid` lookups fail even if python packages are installed
- External dependencies: `ffmpeg` must be available on `$PATH` for schema 12's retry of audio that decodes short of its header; without it, extraction records coverage and marks files still short as truncated but cannot recover damaged frames. Learned descriptor heads download to ignored `models/heads/` and are SHA-256 verified.
- Path convention: Use `pathlib.Path` consistently across all tools and scripts
- Repo layout & outputs: Numbered CLI scripts (`01`–`07`), `intake.py`, and `runscripts.py` reside in repo root for now. Active operational state (`features.pkl`, `features_export.json.gz`, `review.json`, `review.log`) lives in repo root; never delete `features.pkl` (feature store for ~44k tracks) or `review.json`. All one-off audit reports, reconciliation data, and cluster plots belong in `reports/` (e.g. `reports/consolidation/`, `reports/genres/`, `reports/clusters/`); never scatter reports or plot images in the repo root. Log archives live in `logs/archive/`. Both `reports/` and `logs/` are gitignored.

## Configuration

- Main config: `config.py` (never committed; required at module import time by all scripts; initialize from `config.example.py` if missing)
- Beets config: `config/beets.yaml`
- API keys: `.env` (never committed)
- Genre mapping: `FOLDER_TO_GENRE` dict
- Special folder genre policy: `SPECIAL_FOLDER_GENRE_POLICY`
- Thresholds: `ARTIST_FOLDER_THRESHOLD`
- BPM disabled for: `NO_BPM_FOLDERS`
- Skip folders: `SKIP_FOLDERS`

## Important Constraints & Player Contract

- Always test with `--dry-run` or `--pretend` first
- Scripts are idempotent but may produce review flags
- Folder classification logic in `lib/context.py`
- Genre hierarchy: slash-separated (e.g., "Rock / Alt-Rock")
- All audio operations respect dry-run mode
- Never re-encode audio during cleanup (changes `audio_hash`, invalidating feature store and client cache)
- Audio hash identity: features and client tracks are keyed by `audio_hash`, not path or track_id
- Player refresh is remote-only: after `scripts/server_sync.sh` completes, refresh the player on the remote host; do not run a local player refresh or synchronize the legacy persistent Beets database
- Preserve ratings: never wipe `POPM`, `TXXX:FMPS_Rating`, `TXXX:FMPS_Playcount`, `FMPS_RATING`, or `RATING`. Beets config must not use `scrub`
- Tag standard: follow MusicBrainz Picard mappings and write ID3v2.4. `albumartist` must be written on all album tracks to bind featured/guest credits (`feat.`) without splitting albums
- Compilations vs Lists: real compilations receive `albumartist = Various Artists` and `compilation = 1`. Curated lists (Pitchfork 500, etc.) are converted to `.m3u8` playlists via `scripts/convert_lists.py`
- Ogg, Opus, and WMA (1%): hashed whole. Never move and retag an Ogg/Opus/WMA file in the same pass if phone track identity needs to be preserved
- Cover art: embed front cover (APIC type 3) or place `cover.jpg` / `folder.jpg` in the album directory
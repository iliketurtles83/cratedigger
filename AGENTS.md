# AGENTS.md

This file helps OpenCode agents avoid mistakes and ramp up quickly. Every line answers: "Would an agent likely miss this without help?"

## Key Commands

- Run tests: `python -m unittest discover -s tests` (real audio tests: `CRATEDIGGER_RUN_REAL_AUDIO_TESTS=1 python -m unittest discover -s tests`)
- Dry-run all pipeline: `python intake.py --dry-run`
- Process folder: `python 01_tag.py --dry-run --folder jazz`
- Review flagged items: `python 05_review.py`

## Pipeline Flow

1. **01_tag.py** - Fingerprint → MusicBrainz → write tags
2. **02_rename.py** - Normalize filenames using tags
3. **03_folders.py** - Normalize folder names, classify folder types
4. **04_move.py** - Intake routing, restructuring, promotion

## Architecture Notes

- Folder naming contract: artist-root with letter bucketing (A-Z, # for symbols)
- Special folders: `0compilations`, `0various`, `0mixes`, `0singles`, `0random`, `#`
- Letter bucketing: articles stripped for bucketing only (`The Cure` → `C/`)
- Artist-folder threshold: configurable (default 3 albums)
- Folder context is read-only; scripts import from `lib/context.py`
- Tags are written via `lib/tags.py` (format-agnostic wrapper)

## Configuration

- Main config: `config.py` (never committed)
- API keys: `.env` (never committed)
- Genre mapping: `FOLDER_TO_GENRE` dict
- Special folder genre policy: `SPECIAL_FOLDER_GENRE_POLICY`
- Thresholds: `ARTIST_FOLDER_THRESHOLD`
- BPM disabled for: `NO_BPM_FOLDERS`
- Skip folders: `SKIP_FOLDERS`

## Important Constraints

- Always test with `--dry-run` first
- Scripts are idempotent but may produce review flags
- Use `intake.py` for new arrivals (orchestrates 01→02→04)
- `05_review.py` handles flagged items from earlier steps
- All scripts support `--folder NAME` to process one top-level folder
- Folder classification logic in `lib/context.py`
- Genre hierarchy: slash-separated (e.g., "Rock / Alt-Rock")
- All audio operations respect dry-run mode
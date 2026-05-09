# Copilot Instructions for cratedigger

Trust this file first. Only search the repo when these instructions are missing needed detail or are contradicted by the current code.

## Repository summary

`cratedigger` is a small Python CLI repository for maintaining a local music library. The pipeline tags audio files, normalizes filenames, normalizes album folders, routes incoming music into staging folders, and supports manual review of flagged cases. There is no web app, no package build, and no framework scaffolding.

- Language/runtime: Python. README says Python 3.10+; validated here on Python 3.13.5.
- Project shape: top-level numbered scripts plus shared helpers in `lib/`.
- External services/tools: optional MusicBrainz + AcoustID lookups, optional BPM detection via `librosa`, external `fpcalc` binary for fingerprinting.
- Automation: there are no GitHub Actions workflows, no CI config, no lint config, and no automated test suite.

## Required setup before any command

Always run from the repo root and prefer the repo virtualenv interpreter directly:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Validated bootstrap facts:

- `.venv/bin/python -m pip install -r requirements.txt` succeeds in this repo.
- `config.py` is required for every script; they import `config` at module import time. If it is missing, copy `config.example.py` to `config.py` first.
- Use the real `config.py` for operational behavior. `config.example.py` is only a template and does not necessarily match the active folder names or staging paths.
- `.env` is only required for live MusicBrainz/AcoustID lookups. `lib/mb.py` loads it at import time and degrades gracefully when keys are missing.
- `fpcalc` is installed at `/usr/bin/fpcalc` in this workspace. Without `fpcalc`, `pyacoustid` lookups will fail even if the Python package is installed.

## Command map and validated workflow

There is no `build`, `lint`, or `test` script. Use these instead:

- Bootstrap: `.venv/bin/python -m pip install -r requirements.txt`
- Syntax check: `.venv/bin/python -m py_compile 01_tag.py 02_rename.py 03_folders.py 04_move.py 05_review.py 06_analyze.py intake.py lib/*.py`
- Automated tests: `.venv/bin/python -m unittest discover` currently reports `Ran 0 tests`
- Safe manual validation: use the pipeline scripts in `--dry-run` mode against one folder

Always validate in this order before any live write:

1. `01_tag.py --dry-run --no-mb --no-bpm --folder <folder>` for a fast, local-only preview.
2. `01_tag.py --dry-run --folder <folder>` only when you intentionally want MB/BPM behavior exercised.
3. `02_rename.py --dry-run --folder <folder>`
4. `03_folders.py --dry-run --folder <folder>`
5. `04_move.py --restructure --dry-run --folder <folder>` or `04_move.py --intake --dry-run`, depending on the change.

Validated behavior/caveats:

- `01_tag.py` dry-runs are safe, but without `--no-mb --no-bpm` they can still call external services and run BPM analysis.
- `intake.py --dry-run` runs `01_tag.py`, then `02_rename.py`, then `04_move.py --intake`; it was validated successfully and does perform MB/BPM work by default.
- `05_review.py --dry-run` is still interactive and blocks for user input. Do not run it in unattended cloud-agent flows.
- Live runs write or append `review.json` and log to `review.log` in the repo root unless `--log` overrides that path.

## File ownership and architecture

Use these files first instead of broad searching:

- `config.py`: local, untracked source of truth for library paths, folder taxonomy, thresholds, and special-folder rules.
- `01_tag.py`: tagging entrypoint. Owns per-file tag fill logic and MB/BPM gates.
- `02_rename.py`: filename normalization. Tags are the source of truth; folder structure only decides disc-prefix behavior.
- `03_folders.py`: folder normalization and `artist_flat` handling.
- `04_move.py`: routing, restructure, and promotion. Owns the artist-threshold move logic.
- `05_review.py`: interactive resolver for `review.json`.
- `intake.py`: orchestrates `01_tag.py -> 02_rename.py -> 04_move.py --intake`.
- `06_analyze.py`: empty stub.

Shared library ownership:

- `lib/context.py`: single source of truth for folder classification, `get_folder_context`, compilation inference, and best-of inference. Do not reimplement folder-shape logic elsewhere.
- `lib/tags.py`: mutagen read/write wrappers for MP3, FLAC/OGG/Opus, and M4A/AAC.
- `lib/parsers.py`: filename/folder parsing and filename sanitization.
- `lib/genres.py`: genre normalization, blacklist, and parent-genre lookup from `genre-tree.txt`.
- `lib/mb.py`: dotenv loading, AcoustID fingerprinting, MusicBrainz metadata/genre lookups, and rate limiting.
- `lib/logger.py`: common console/file logging.

## Repo-specific rules that matter for PR survival

- Default behavior is dry-run; preserve that pattern in any new write path.
- Prefer `pathlib.Path`; the repo is written that way throughout.
- Do not hardcode folder names, genre names, or paths outside `config.py`.
- Treat `lib/context.py` as the authoritative boundary for path-derived behavior.
- `SPECIAL_FOLDERS` and `SKIP_FOLDERS` are operational guardrails; do not bypass them.
- If you change tagging, validate with `01_tag.py` first. If you change naming, validate with `02_rename.py` or `03_folders.py`. If you change movement logic, validate with `04_move.py` dry-runs.
- Because there is no CI, the minimum confidence check for Python changes is `py_compile` plus the narrowest relevant dry-run command.
# Feature extraction and player export plan

Status: v10 extraction committed (`ee9bba2`) and validated by a 240-track
pilot; schema 11 corrects stereo loudness; production migration ready to run;
export versioning not started.
Date: 2026-11-15.
Reviewed baseline: `884ae83`.

## Scope and decisions

- Extract reusable descriptions of audio, independent of personal taste.
- Primary consumer: the music player's "more like this" playlists.
- Secondary consumers: music analysis, future recommendation models, and
  evidence-backed database enrichment.
- Migrate by incrementing extraction versions and resuming normal analysis.
  Do not use `--force`; changing its behavior is outside this implementation.
- A mixed-version store during migration is expected. Do not discard existing
  work or replace the deployed player export with a partial pilot.
- Do not build a personalized recommender, synchronize player favorites, change
  player code, retag audio, or migrate the feature store to a new database here.

Ratings and favorites are optional evaluation or personalization labels, not
audio features. Existing file ratings could support a later offline experiment;
player favorites could support the same purpose if made available. Neither is
required for this extraction work, and unlabeled tracks are not dislikes.

## Completed engineering pilot

[Detailed results and sample manifest](../reports/features/extraction_pilot_2026-11-15.json)
are local, gitignored operational data. The report records the source commit,
model checksum, dependency versions, hashes, paths, and excerpt positions.

Design:

- 24 distinct tracks: two different artists from each of 12 genre roots.
- Six additional same-artist/same-album tracks as structural controls.
- Unique audio hashes; deterministic seed `20261115`; no ratings used.
- The same central 24-second excerpt for each preprocessing variant.
- Current `22050 -> 16000` QQ resampling versus HQ resampling, plus direct
  native-rate-to-16000 HQ as a reference.
- Both ONNX outputs collected: 400 style activations and 1280 latent features.
- Synthetic out-of-band tone, silence, and repeated-inference controls.

| Observation | Result | Consequence |
| --- | --- | --- |
| Same-track style cosine, QQ versus HQ | Median 0.999202; minimum 0.992184 | Correct resampling without claiming a large perceptual improvement |
| Same-track latent cosine, QQ versus HQ | Median 0.998841; minimum 0.987887 | Preprocessing changes still need explicit versioning |
| HQ through 22050 versus direct native HQ | Minimum cosine 0.999998 for style, 0.999999 for latent | Keep shared decoding for now; no demonstrated need for a second decoder |
| Style versus latent top-five overlap | Median 4/5 neighbors | Different representations; not proof that latent is better |
| 10 kHz tone after downsampling | QQ RMS 0.451904; HQ RMS about 0.000000072 | Use anti-aliased downsampling |
| Silence | Nonzero outputs from both model heads | Model-output validity alone is insufficient |
| Repeated inference on identical patches | Maximum absolute difference 0 for both heads | Same-environment repeatability established for this control |

Both outputs were finite and correctly shaped for all 30 tracks and all three
variants. The production feature store's size and modification timestamp were
unchanged. Pilot runtime was 7.43 seconds on this host, excluding the failed
first attempt to serialize missing album metadata. The report writer was
corrected to encode missing metadata as JSON null before the successful rerun.

Limitations: short excerpts, small candidate pool, existing genre tags, and
album controls concentrated in Rock/Metal/Electronic/Pop. Album membership is
not a human similarity label. These results do not select a winning player
recipe or establish full-track extraction throughput.

### Follow-up full-track timing

[Paired timing results](../reports/features/extraction_timing_2026-11-15.json)
compare six complete MP3 tracks spanning 75-277 seconds, with two repeats per
variant, alternating pair order, excluded warmups, and a shared single-threaded
CPU ONNX session.

- Current extraction: mean 2.790 seconds per track.
- Prototype with HQ embedding resampling and both raw outputs retained:
  mean 2.821 seconds per track.
- Total elapsed increase: 1.1%; median paired increase: 0.006 seconds.

This suggests roughly the same three-second processing cost, not a significant
new inference expense. The current inference call already returns both model
outputs. This is a controlled prototype of the expensive audio/model changes,
not the completed implementation. It excludes checkpoint serialization of the
larger store, future provenance/status handling, cold startup, and multi-worker
batch effects. Measure those separately in the full-track rollout pilot.

### v10 implementation pilot

[Consolidated results](../reports/features/pilot_v10_2026-11-15.json) (local,
gitignored) cover 240 full tracks: 20 per genre root, one track per artist, an
artist-disjoint 120 development / 120 control split, and nine synthetic edge
fixtures. Both schema versions ran with 8 workers against the same symlinked
library; the production store was not touched.

| Gate | Result |
| --- | --- |
| Runtime | v10 0.63 s/file vs v9 0.69 s/file wall time; peak RSS unchanged (~2.6 GB) |
| DSP regression | BPM, key, mode, and librosa scalars bit-identical; 4/240 `bpm_confidence` differ by <=0.01 |
| Style vector, v9 vs v10 | Cosine median 0.9994, minimum 0.983; dev and control splits match |
| Fixtures | Silence and out-of-phase stereo `skipped_silent`; 1.5 s tone `skipped_too_short`; 21 min `skipped_duration`; corrupt files are error rows |
| Interrupt and resume | Exit 130 in 0.25 s, no orphan workers, saved rows = completed files (87); resume bit-identical |
| Embedding retry | Failed and stale-recipe rows run embedding-only; DSP and `analyzed_at` unchanged |
| Storage | ~366 MiB for 43,882 rows (8.6 KiB/row, currently 87 MiB); atomic save 0.6 s, load 0.5 s |

The `bpm_confidence` drift is pre-existing: Essentia `RhythmExtractor2013`
returns slightly different confidences for an identical buffer within one
process, with ONNX Runtime not involved.

EffNet embeddings depend on input level because the model frontend uses
`log10(1 + 10000 * mel)`. A pure gain of -6 dB gives median style cosine 0.955
to the original. Two follow-up studies tested LUFS-normalized model input:

- 1,188 tracks: normalizing to -16 LUFS left leave-one-out genre precision@10
  unchanged (+0.003, 95% CI includes zero) and replaced about 25% of
  neighbors.
- 104 same-recording pairs from different releases: retrieval was unchanged;
  recall@10 was 0.70 raw vs 0.70 normalized for style vectors.

Real mastering differences are not pure gain, so v10 keeps the model's
reference (unnormalized) input. `loudness_lufs` is stored if a future recipe
revisits this.

### Schema 11 loudness correction

Through schema 10, `loudness_lufs` was measured on the mono 22.05 kHz analysis
downmix. BS.1770 sums channel power, so that underestimated stereo files by 3
dB (identical channels) to 6 dB (uncorrelated channels). Schema 11 decodes once
at the native rate, measures the original channels with pyloudnorm, then
derives the analysis buffer exactly as `librosa.load` does (downmix, then
`soxr_hq`). On the pilot (240 tracks plus fixtures):

- Embeddings were bit-identical to v10. DSP matched apart from the known
  `bpm_confidence` jitter (4 tracks) and one danceability value (0.0005).
- Stereo loudness rose by a median of 3.5 dB (5th-95th percentile 3.0-5.0,
  maximum 6.9); mono files were unchanged. Pilot median: -12.7 LUFS.
- Wall time was 0.72 s/file (v10: 0.63) and peak RSS 3.1 GB. On a 19.5-minute
  stereo file, peak RSS rose from 2.0 to 2.3 GB.
- Out-of-phase stereo now has a finite loudness, but it still skips embedding
  because its mono analysis signal is silent.

A vectorized BS.1770 implementation matched pyloudnorm exactly but saved only
about 15 minutes over the full run, so v11 keeps pyloudnorm.

Only `loudness_lufs` changes meaning. `energy`, `rms_dbfs`, `peak_dbfs`,
valence (through its energy term), and the embeddings still follow file level.

Estimated production migration: about 8.7 hours for 43,882 files at the
measured rate.

## 1. Correct preprocessing and retain reusable model outputs

Main surfaces:
[lib/embeddings.py](../lib/embeddings.py),
[06_analyze.py](../06_analyze.py),
[requirements.txt](../requirements.txt),
[pyproject.toml](../pyproject.toml).

1. Replace QQ downsampling in the model frontend with `soxr_hq`.
2. Give path-based and supplied-audio extraction an explicit, shared
   preprocessing contract. Keep the existing shared 22050 Hz decode unless
   full-track tests identify a reason to change it.
3. Verify frontend parameters, patch framing, padding, and output names against
   the model contract before freezing the new extraction version. Do not change
   patch sampling and resampling simultaneously without an isolated comparison.
4. Return a typed result containing both mean-pooled raw outputs from one model
   inference call:
   - `style_activations`: float32, exactly 400 dimensions.
   - `audio_embedding`: float32, exactly 1280 dimensions.
5. Preserve raw activation magnitude. Normalization belongs to derived
   similarity vectors, not the reusable stored evidence.
6. Keep the existing normalized 400-dimensional `embedding` contract for
   current consumers during transition. Do not put 1280 dimensions into a field
   that existing consumers interpret as 400 genre classes. Any temporary
   compatibility copy must be derived from the same activations.
7. Add missing model-runtime dependencies to the appropriate audio dependency
   manifests. Perform a dependency/model preflight before scheduling a batch;
   do not mark embedding-free extraction as fully successful.

Continue using track-level mean pooling and the existing bounded patch policy
initially. Retaining all frame-level embeddings, changing models, and adding
multiple segment statistics are deferred until there is a concrete consumer.

Acceptance:

- Tests assert exact shapes, float32 storage, finite values, and both outputs
  coming from one inference call.
- Regression test for out-of-band alias suppression with HQ RMS below 1e-5
  for the pilot's 10 kHz tone control.
- Equivalent path/buffer inputs agree within a declared numerical tolerance.
- Existing genre and player callers still receive their documented shapes.

## 2. Make completion, quality, and provenance explicit

Main surfaces:
[06_analyze.py](../06_analyze.py),
[lib/tempo.py](../lib/tempo.py),
[07_recommend.py](../07_recommend.py).

- Increment `FEATURE_SCHEMA_VERSION` after the extraction changes are complete
  (expected next version: 10). Add narrowly scoped component recipe identifiers
  so a later scalar change does not require repeating unchanged model inference.
- Record model checksum, class-label mapping identity, frontend/pooling recipe,
  and extraction method. Keep the existing audio hash as content identity.
- Represent component outcomes explicitly: successful, failed, unavailable, or
  intentionally skipped, with a reason. Distinguish beatless audio from tempo
  extraction failure and short audio from a missing model dependency.
- Retry failed or unavailable required components on resume. Do not repeatedly
  retry intentional exclusions without an input or policy change.
- Never stamp retained old values with a new component version after a failed
  recomputation. Preserve old evidence as old evidence.
- Keep atomic checkpoint replacement and preservation of unrelated rows.
- Bring embedding backfill and hash backfill into the same version rules.
  Hash backfill must not overwrite every row's extraction version with `8`.
- Preserve full raw tempo confidence and its method, and raw key strength.
  Neither should be represented as a calibrated probability without calibration.
- Record sanitation counts/fractions and signal RMS. All-silent audio must not
  be export-eligible merely because the neural network returned a vector.
  Near-silent and heavily corrupted thresholds need representative controls;
  do not guess a blanket cutoff that removes quiet music.
- Preserve useful raw summaries underlying existing proxies, particularly
  dynamic complexity and the MFCC/rhythm summaries already computed. Avoid
  adding expensive analysis solely to populate more descriptive column names.

The existing liveness, instrumentalness, acousticness, speechiness and valence
fields remain documented heuristic compatibility fields initially. Add explicit
raw measurements rather than silently assigning new meanings to old fields.
They are not reliable automatic tags or ground-truth labels.

Acceptance:

- A missing embedding is retried without needlessly repeating valid components.
- Interrupted/restarted and subset runs preserve unrelated saved rows.
- Quiet valid music, silence, corrupt samples, short tracks, intentional
  long-track skips, missing dependencies, and transient inference failures have
  distinct tested outcomes.
- Unchanged successful inputs retain their extraction timestamps.
- No test changes tags, audio content, or audio hashes.

## 3. Validate representations separately from personalization

Main surfaces:
[lib/recommender.py](../lib/recommender.py),
[07_recommend.py](../07_recommend.py).

First run the revised extractor on an isolated, full-track pilot store:

- Proposed scope: 240 distinct audio hashes, 20 per pilot genre root.
- Split into 120 development tracks and 120 control tracks by artist, balancing
  genre where feasible; freeze the manifest before evaluating.
- Include album companions across genres and engineering fixtures for silence,
  short files, stereo/downmix edge cases, codec variation, and corrupt samples.
  Genre/album agreement remains a structural check, not relevance truth.
- Rank against the identical real-track candidate pool for every recipe.
- Compare normalized 400-class styles, normalized 1280-dimensional latent
  features, and a small number of confidence-aware hybrids.
- Do not assume the current 70/15/10/5 weighting is optimal. Include an
  embedding-only baseline; retain tempo/key/scalars only when they help.

For musical quality, prepare blinded neighbor lists for 12 development seeds
and 12 artist-disjoint control seeds. Judge similarity/playlist usefulness rather
than asking whether every candidate is a favorite. Do not tune against control
judgments. Report top-five judged relevance, same-artist concentration, duplicate
exclusion, and the number of seeds improving or regressing.

Fix the existing evaluation path to use real distractors and deterministic
sampling. If retained, its favorites benchmark must be labeled as optional taste
retrieval, kept separate from seed similarity, and must not silently substitute
the whole library when no usable favorites source exists.

Acceptance:

- No synthetic random-vector distractors in reported quality comparisons.
- No exact audio-hash duplicates or the seed itself in recommendation lists.
- Candidate pools, feature eligibility, and calibration are comparable.
- A latent/hybrid recipe is not promoted merely because album controls improve.
- If judged control quality does not improve, keep the existing player recipe
  while still retaining richer extraction data for future use.

## 4. Harden and independently version the player export

Main surfaces:
[lib/recommender.py](../lib/recommender.py),
[07_recommend.py](../07_recommend.py).

- Preserve the current 410-dimensional recipe while extraction evolves.
  Corrected preprocessing still requires a distinct export recipe identity.
- Do not silently replace the 400-dimensional block with 1280 dimensions.
  Any such change requires confirmation of the actual player/server contract.
- Export only valid, nonempty string hashes and finite vectors with the exact
  declared dimension. Reject NaN/Infinity serialization and zero-vector success.
- Apply explicit eligibility rules for required feature components; report
  excluded counts and reasons. Missing optional tempo is not automatically an
  extraction failure, but its effect on normalization must be tested.
- Fit scalar calibration only on eligible, finite, validated observations.
  Persist calibration parameters, feature order, weights, and component recipe
  identities so the vector space is reproducible.
- Deduplicate by audio hash with feature compatibility/freshness considered
  before metadata-path preference.
- Allow mixed versions in the local store, but never silently combine
  incompatible component recipes in one declared export vector space.
- Write exports atomically. Keep the deployed export until the replacement
  meets coverage and client-compatibility requirements.
- Consider exporting schema 11 `loudness_lufs` and `peak_dbfs` so the player
  can level playback volume without rewriting audio.

Measure payload size and player-facing dimensional/memory costs before choosing
any larger recipe. The current saved gzip export is about 34.3 MiB, so the older
5-15 MiB documentation claim is not a reliable budget. Do not add PCA,
quantization, or a new transport format without an actual measured requirement.

## 5. Keep metadata enrichment behavior compatible

Relevant consumer:
[predict_effnet_genre](../cratedigger/engine/metadata.py).

- Audit every caller of the existing embedding helper when adding the typed
  dual-output interface.
- Keep existing automatic genre behavior unchanged in the extraction rollout;
  its thresholds currently operate on normalized style scores.
- Expose raw style activations for future enrichment experiments, but do not
  apply the old thresholds to the new raw scores. Raw model activations are
  also not automatically calibrated probabilities.
- Any future automatic enrichment needs labeled validation, source/model
  provenance, reviewable proposals, and separate approval.
- No SQLite feature-storage migration is needed now. The current database sync
  imports identity/metadata; rich feature storage remains independent.

## Rollout and verification

Suggested implementation sequence:

1. Dual-output extraction, HQ frontend, provenance/status, version migration,
   compatibility wrappers, and focused regression tests. (Implemented.)
2. Isolated 240-track full-track development/control pilot; repair evaluation
   semantics; measure feature quality, runtime, and storage overhead.
   (Pilot complete; evaluator repair remains and does not block extraction.)
3. Resume normal version-driven library extraction after engineering gates pass.
   Do not make this extraction migration depend on choosing a new player recipe.
   (Ready at schema 11: `.venv/bin/python 06_analyze.py --workers 8`.)
4. Validate/export the compatible style-based recipe; promote a new recipe only
   after seed-quality checks and player contract confirmation.

Use isolated output paths under `reports/features/` for pilot stores and
exports. Do not point a partial pilot at the production feature store.
Keep reports in `reports/`; do not export audio or send it to external services.

Focused tests start with:

```bash
.venv/bin/python -m unittest tests.test_analyze tests.test_embeddings tests.test_recommender tests.test_tempo tests.test_recommend_backfill
```

Add affected genre-consumer and database-sync tests when those interfaces are
touched. Expand coverage only as needed. Update the extraction documentation in
[README.md](../README.md), the status claims in [roadmap.md](./roadmap.md), and
the export/schema contract alongside implementation.

Completion means extraction can resume safely, rich evidence is preserved,
existing consumers remain compatible, and exports are validated and
reproducible. A personalized model is explicitly not a completion requirement.

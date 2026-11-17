# Music player integration contract

Cratedigger owns this producer-side contract. The player repository should
document only the fields and versions it consumes, plus its behavior when
data is missing. Update this document when the producer's output or identity
rules change.

## Feature export

Generate the current export with:

```bash
.venv/bin/python 07_recommend.py --export
```

By default this writes `features_export.json.gz` in the repository root.
`--output PATH` selects another destination. A `.gz` suffix produces a
gzip-compressed UTF-8 JSON document; another suffix produces plain UTF-8 JSON.
The current manual handoff to the player host is separate from the audio
library sync.

The local UI can preview and publish the same export through **Export features
for the player**. It checks the current contract before an atomic publish and
reports whether the source or published file has changed since the last
successful UI export. Those freshness checks use file metadata rather than a
content hash. They do not validate exports produced outside the UI or deliver
the export to the player host.

The top-level JSON object contains:

| Field | Meaning |
|---|---|
| `model_version` | Export recipe identifier, currently `cratedigger-v2.0-discogs-effnet-hybrid`. |
| `created_at` | UTC generation timestamp in ISO 8601 form. |
| `dims` | Vector length, currently 410 for a non-empty export and 0 when empty. |
| `weights` | Normalized component weights used to bake the similarity vector; current defaults are embedding 0.70, tempo 0.15, harmonic 0.10, and scalars 0.05. |
| `descriptor_recipe` | Identifier for the learned descriptor calculation. |
| `descriptor_scale` | `logit`; classifier descriptor values are not probabilities. |
| `track_count` | Number of tracks in `tracks`. |
| `excluded_count` | Hashed, deduplicated rows excluded for export eligibility. Rows without a hash are dropped before this count. |
| `tracks` | Array of per-track feature objects. |

Each track contains:

- `audio_hash`: non-empty identity string used to match the export entry to
  audio. It is not a path and is not guaranteed globally unique.
- `vector`: 410 rounded numeric components in the current recipe; this is the
  player's similarity vector.
- `bpm`, `key`, `mode`: tempo and harmonic values; unknown values are JSON
  `null`.
- `energy`, `valence`, `danceability`, `acousticness`, `instrumentalness`,
  `happy`, `sad`, `relaxed`, `aggressive`, `party`, and `electronic`: learned
  classifier logits. Use their ordering (for example, library percentiles)
  unless a probability is explicitly needed; then apply a sigmoid.
- `approachability` and `engagement`: learned regression outputs, not
  classifier probabilities.
- `loudness_lufs` and `native_peak_dbfs`: measured playback-level fields.
- `artist`, `album`, and `genre`: optional string metadata when present.
- `truncated`: optional `true` flag when decoding remained truncated but the
  track passed the export coverage threshold.

Tracks without a usable style embedding, silent tracks, tracks below 50% decode
coverage, and rows without a non-empty `audio_hash` are not exported. A
truncated track that passes the coverage threshold can still be exported and
is marked `truncated: true`.

The current `model_version` identifies the feature/vector recipe; it is not a
separate serialization-format version. Bump it for a breaking field, meaning,
or vector change. If format compatibility later needs to vary independently
from the recipe, add an explicit format version before making that change.
Additive optional fields should be tolerated by consumers.

## Audio identity

The player should use the `audio_hash` value supplied by the export rather than
reimplementing the hash when possible. The current algorithm is `a1`:

The UI's guarded hash backfill fills missing identities without changing audio
files or replacing existing hashes. Rebuild the player export after a
successful backfill so newly identified tracks can be included.

1. Select the audio region based on file type. MP3/MP2 skip ID3v2 headers and
   recognized ID3v1, APEv2, and Lyrics3v2 trailers; FLAC skips leading
   metadata blocks and recognized trailers; MP4/M4A-family files use the first
   `mdat` atom; WAV uses the first `data` chunk. If a recognized container has
   no usable audio region, or the format is unrecognized, the whole file is
   used.
2. Hash the decimal ASCII byte length of that region followed by its content.
   For regions up to 512 KiB, use the whole region. For larger regions, use
   only the first 256 KiB and last 256 KiB.
3. Return `a1:` followed by the first 32 lowercase hexadecimal characters of
   the SHA-256 digest.

This is a fast matching key, not a full-file checksum or a collision-proof
global identifier. Changes only in the unsampled middle of a large region do
not change the hash. For formats that use the whole file, tag edits change
the hash; this currently includes Ogg, Opus, and WMA. Hash collisions and
duplicate audio are possible, so consumers should not treat the hash alone as
a unique database key.

## Delivery and library assumptions

The export and audio library are currently delivered separately: the export
is copied to the player host manually, while audio is synced from this PC to
the NAS by rsync or, for a large initial/full sync, via a complete USB mirror
that is then synced to the NAS. There is no automated export publish step or
guarantee that the two deliveries are simultaneous.

`scripts/server_sync.sh` is a mirror operation: its source must be a complete
copy of the intended library because `--delete-before` removes destination
paths missing from the source. It applies the same exclusions for either sync
hop. It is not safe for a partial USB batch.

The local UI can preview and apply this mirror operation with an explicit
complete-source confirmation. It checks a repeated itemized rsync dry-run
against the reviewed plan, but cannot prove completeness or exclude concurrent
external changes. A failed or cancelled sync may leave partial changes. The UI
does not refresh the player; refresh it on the player host after a successful
sync.

Audio paths are owned by cratedigger and organized as letter-bucketed artist
roots, with special roots such as `0compilations`, `0various`, `0mixes`,
`0singles`, `0random`, and `#`. The feature export does not contain file paths;
match tracks by `audio_hash`.

For playlist paths, tag fields, and player refresh expectations, see the
existing [README guidance](../README.md) and
[player feedback notes](./cratedigger-feedback.md). The player refresh runs
on the player host after its library sync, not as part of this producer export.

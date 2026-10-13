# Feedback for cratedigger, from music-player (2026-10-12)

Notes from fixing the Artists tab in the player. In it, "!!!", "Aaron Neville" and "1000mods" showed hundreds of tracks. The player now imports Beets as singletons, so Beets keeps each file's own tags. What the Artists and Albums views show now depends on the tags cratedigger writes. Numbers are from the server's Beets DB (45,687 tracks).

The rule behind most of this is to separate what a song *is* (its release: artist, album, year) from what *you did with it* (favourited it, rated it, put it on a list). The first belongs in tags and the folder layout follows from it. The second belongs in ratings and playlists, never in album tags or folder names.

## 1. Real compilations vs lists

Ask of each folder in `0compilations/`: was it released as a record, or is it someone's list?

- **Released compilation** (Ultra Disco Hits, Far Cry 5 OST, *Arabic Music (2011)*): set `albumartist = Various Artists` and `compilation = 1` on every track. Keep each track's own `artist`. The player then shows one album under "Various Artists", and each song still counts for its own artist.
- **List** (The Pitchfork 500, Rolling Stone 500, `djcity`, "Classic Rock … Playlist", "Top 100 Latin Hits Playlist"): tag each song with its real release, as in section 2, and turn the folder into a playlist. Don't tag it as Various Artists.

## 2. `0faves` and other "judgement" folders

| Folder | What it encodes | Better form |
|---|---|---|
| `0faves/` | favourites | rating (section 3) and/or `Playlists/Faves.m3u8` |
| `0random/<genre>/` | a genre | `genre` tag |
| `0random_good/<genre>/` | a genre plus "good" | `genre` tag plus a rating |
| list folders (section 1) | someone's ranking | playlist, in list order |

For each song in these folders:

1. Tag it with its real release: `artist`, `album`, `albumartist`, `year`, `track`, `compilation = 0`. Most `0faves` files already have this (e.g. ABC → *The Lexicon of Love*, album artist ABC). Where AcoustID/MusicBrainz can identify the recording, use that release.
2. If the release can't be identified, **leave `album` empty**. Never write a folder name ("0faves", "Favorites") or a guess into it. An empty album makes it a single, and the player lists it as a loose track by its artist.
3. Move the file into the normal layout (section 6) and add it to the playlist with a relative path. Moves and retags keep the player's track IDs, so this is safe.
4. If the same song already exists on its album elsewhere in the library, keep one copy (see section 5).

## 3. Ratings: keep them in the file, don't strip them

Strawberry wrote ratings into the files: every sampled file in `0faves/` and `0random_good/` has `TXXX:FMPS_Rating = 1` and `POPM` rating 255 (5 stars), and some have `TXXX:FMPS_Playcount`.

- When cratedigger rewrites tags, it **must preserve** `POPM`, `TXXX:FMPS_Rating` and `TXXX:FMPS_Playcount` (MP3), and `FMPS_RATING`/`RATING` (Vorbis/FLAC). A tag writer that rebuilds the tag from scratch loses them.
- A rating is the right place for "good" and "favourite". Once ratings are set, the `0random_good` and `0faves` folders aren't needed.
- The player doesn't read ratings yet (Beets can't). Reading them during the registry scan and sending them to the phone is planned on the player side.

## 4. Artist names

- **320 artists appear under more than one spelling**: `ABBA`/`Abba`, `ABC`/`Abc`, `The Arcade Fire`/`Arcade Fire`, `The Animals`/`Animals`, `Althea and Donna`/`Althea & Donna`/`Althea And Donna`, `ANTHONY B`/`Anthony B`, `A. R. Rahman`/`A R Rahman`, `arctic monkeys`/`Arctic Monkeys`. Each spelling becomes its own artist in the player. Use the MusicBrainz canonical name where there's a match. Otherwise fold case, a leading "The", `&`/`and` and dots to one spelling.
- **From using the player (2026-10-13):**
  - **`&` vs `and`:** don't apply a blanket rule. Some names really are written with `&` (Simon & Garfunkel) and some with "and" (Florence and the Machine). Take MusicBrainz's spelling where there's a match; fold to one spelling only for the comparison key.
  - **Small words in titles and names** (`and`/`And`, `of`/`Of`, `the`/`The`): pick one title-case style and apply it to tags without a MusicBrainz match. Expect exceptions: deliberately lowercase or all-caps names and acronyms, `feat.`, and the first and last words.
  - **`VA`, `V.A.`, `Various`, `various artists`** in `albumartist`: normalise to `Various Artists` with `compilation = 1` (section 1).
  - Most of this goes away with a MusicBrainz lookup for every track (section 5). Hand-written rules will keep running into edge cases, so it may be worth paying for the slow lookups (1 request per second) once.
- **Junk values:** there's an artist literally named `artist`, one that's just `-` and one that's a single space. Some "artists" are really titles from filenames, e.g. `(Tell_Me)_Have_You_Ever_Seen_Me` and `(We_Ain't_Got)_Nothin'_Yet`.
- **Folder names starting with a dot** (`J/Jazzanova/...Mixing (2004)`) are skipped by the player's import, which ignores hidden files (`.*`). Rename them, e.g. drop the leading dots.
- **Featured artists:** 593 tracks have "feat."/"ft." in `artist`, so "X feat. Y" becomes a separate artist. Pick one convention: either move the feature into the title (`Song (feat. Y)`), or keep the credit and write the multi-value `artists` tag with the main artist first. Either way, `albumartist` is the main artist only.
- `albumartist` should be the same on every track of a release. The player groups albums by album artist + album, so one track with a different album artist splits the album.

## 5. IDs and duplicates

- Only 14,014 of 45,687 tracks have a MusicBrainz track ID. Write `mb_trackid`, `mb_albumid` and `mb_artistid` whenever a match is found. They're the stable key for de-duplication, artist merging and joining recommender features.
- 3,082 artist + title pairs exist more than once (7,751 files). Some are legitimate (live versions, remasters). Most are list folders duplicating album copies, which section 1 resolves.
- When deleting a duplicate, prefer keeping the copy that's already in its album folder and already synced to phones. A deleted file's track ID goes away, and phone playlists and favourites point at track IDs. A copy with identical audio is fine to drop, because the player relinks by audio hash.

## 6. Gaps and filename leftovers

- Missing: year 4,314, track number 4,251, album 1,802, genre 1,062, mostly in `0random*` and the list folders. Release matching fills most of these. Leave a field empty rather than guess.
- Titles carrying characters that were replaced in filenames: `_Heroes_`, `B-52\`s`. 1,517 titles are all lowercase (`the passenger`); some are intentional, most aren't.
- 27 tracks have impossible years: two-digit (`98`, `99`, `19`), one- or three-digit (`8`, `198`), or in the future (`2097`, `3010`). The player's Years tab shows them as decades like "90s" and "190s". Write four-digit years, or leave the field empty.
- Two VirtualDJ stem files are in the library as tracks: `44 LateNightTales - BADBADNOTGOOD (2017)/*.mp3.vdjstems`. They aren't music, the server won't serve them, and phones that keep the folder list them as failed downloads. Delete them, or keep them out of the synced tree.
- `0videos/` holds four MPEG music videos (Nine Inch Nails) and an `.asx` playlist, which Beets imported as tracks. 151 `.wma` and 11 `.mpc` files are real audio, but Android can't play either. The player's catalog now leaves all of these out. Converting WMA and Musepack would mean re-encoding, which the contract rules out, so replacing them with better sources is the way to get them on the phone.

## 7. Layout follows tags

Once the tags are right, derive the folders from them, for example:

```
Album Artist/Year - Album/NN Title.ext
Various Artists/Year - Album/NN Artist - Title.ext
Artist/Singles/Title.ext            # no album
Playlists/Name.m3u8                 # relative paths, list order
```

## 8. Album artist holding the wrong value (Kylesa)

Found 2026-10-12. Every track of Kylesa – *Spiral Shadow* (2010) has its **track number in the album artist field**:

```
01 - Kylesa - Tired Climb.Mp3       TPE1=Kylesa  TPE2=01  TALB=Spiral Shadow  TRCK=1/11
02 - Kylesa - Cheating Synergy.Mp3  TPE1=Kylesa  TPE2=02  TALB=Spiral Shadow  TRCK=2/11
```

The player groups albums by album artist + album, so this one album shows as 11 one-track albums under the "artists" `01`…`11`. Cratedigger's layout followed the bad tag too: the folder is `#/01/Kylesa - Spiral Shadow (2010)/`. Rammstein – *Mutter* has one track with album artist `07`.

- **Validate `albumartist` before trusting it.** Reject a value that is only digits and equals or resembles the track number, a URL (one compilation has `http://www.ansarimusic.net`), the literal `artist`, `-` or whitespace. Don't blanket-reject numeric names: `3`, `36`, `213` and `666` are real artists in this library, and each one's album artist matches its artist.
- **Make it consistent per release.** 270 album folders (same folder, same album) have more than one album artist. Most are in `0compilations/` (blank values mixed with track artists and `Various Artists`, section 1). Pick one value per release: the release's credited artist, or `Various Artists` with `compilation = 1`.
- **An empty album artist is fine.** The player falls back to `artist`. A wrong one is worse than none.
- **Fix the tag, then derive the path.** After the fix, Kylesa should move to `K/Kylesa/2010 - Spiral Shadow/` (or wherever section 7's layout puts it).

## 9. Tagging standard

There is no single formal standard. In practice the reference is the **MusicBrainz Picard tag mapping** (https://picard-docs.musicbrainz.org/en/appendices/tag_mapping.html). Beets (through its `mediafile` library), foobar2000, Strawberry, Navidrome and Android's MediaStore read the same frames. The table lists what the player relies on, with the frames `mediafile` reads (checked against beets 2.14.1 in this repo's venv).

| Meaning | Beets field | ID3v2 (MP3) | Vorbis comment (FLAC/Ogg/Opus) | MP4 (M4A) |
|---|---|---|---|---|
| Title | `title` | `TIT2` | `TITLE` | `©nam` |
| Track artist (display credit) | `artist` | `TPE1` | `ARTIST` | `©ART` |
| Track artists (multi-value) | `artists` | `TXXX:ARTISTS` | `ARTISTS` (repeated) | `----:com.apple.iTunes:ARTISTS` |
| Album artist | `albumartist` | `TPE2` | `ALBUMARTIST` (also read: `ALBUM ARTIST`, `ALBUM_ARTIST`) | `aART` |
| Album | `album` | `TALB` | `ALBUM` | `©alb` |
| Track number / total | `track` / `tracktotal` | `TRCK` = `n/total` | `TRACKNUMBER` + `TRACKTOTAL` | `trkn` |
| Disc number / total | `disc` / `disctotal` | `TPOS` = `n/total` | `DISCNUMBER` + `DISCTOTAL` | `disk` |
| Release date | `year` (`month`, `day`) | `TDRC` (mutagen reads v2.3 `TYER` as this) | `DATE` | `©day` |
| Original release date | `original_year` | `TDOR` (mutagen reads v2.3 `TORY` as this) | `ORIGINALDATE` | `----:…:ORIGINALDATE` or `ORIGINAL YEAR` |
| Genre | `genre` | `TCON` | `GENRE` | `©gen` |
| Compilation | `comp` | `TCMP` = `1` | `COMPILATION` = `1` | `cpil` |
| Sort names | `artist_sort`, `albumartist_sort` | `TSOP`, `TSO2` | `ARTISTSORT`, `ALBUMARTISTSORT` | `soar`, `soaa` |
| MusicBrainz recording ID | `mb_trackid` | `UFID:http://musicbrainz.org` | `MUSICBRAINZ_TRACKID` | `----:…:MusicBrainz Track Id` |
| MusicBrainz release ID | `mb_albumid` | `TXXX:MusicBrainz Album Id` | `MUSICBRAINZ_ALBUMID` | `----:…:MusicBrainz Album Id` |
| MusicBrainz artist / album artist IDs | `mb_artistid`, `mb_albumartistid` | `TXXX:MusicBrainz Artist Id`, `TXXX:MusicBrainz Album Artist Id` | `MUSICBRAINZ_ARTISTID`, `MUSICBRAINZ_ALBUMARTISTID` | `----:…` (same names) |
| Release group ID | `mb_releasegroupid` | `TXXX:MusicBrainz Release Group Id` | `MUSICBRAINZ_RELEASEGROUPID` | `----:…` |
| Rating (preserve, section 3) | none | `POPM`, `TXXX:FMPS_Rating` | `FMPS_RATING` / `RATING` | none |
| Front cover | `art` (embedded) | `APIC` type 3 | `METADATA_BLOCK_PICTURE` (FLAC picture block) | `covr` |

The conventions that matter for grouping:

- **`artist` is the credit as printed** (`Daft Punk feat. Pharrell Williams`). **`artists` lists each artist** (`Daft Punk`; `Pharrell Williams`). **`albumartist` is the release's credited artist**, the same on every track, never a featured guest.
- **Compilations:** `albumartist = Various Artists` (MusicBrainz's spelling) plus `compilation = 1`. Each track keeps its own `artist`.
- **Numbers are numbers.** `TRCK`/`TPOS` hold `n` or `n/total`, never in another field. The Kylesa tags look like a tagger that mapped a CSV/CDDB column one slot off.
- **Dates:** ISO `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. `date` is this release's date, `original_date` the first release (a 2011 remaster of a 1979 album has `date = 2011` and `original_date = 1979`). The player shows `year`.
- **ID3 version:** write ID3v2.4, UTF-8. Beets writes 2.4 by default (`id3v23: no`). If a target device needs v2.3, multi-value fields are joined with `/`, which breaks names like `AC/DC`. Use v2.4 unless something forces v2.3.
- **One value per field, except the list fields** (`artists`, `genres`, the MusicBrainz ID lists). Don't put `;`-joined lists into `albumartist`.
- **Leave a field empty rather than guess** (as in sections 2 and 6).

Beets with MusicBrainz autotagging writes all of this when it finds a match. The player's own Beets runs with autotag off and `write: no`, so cratedigger is the only place these tags get written.

## Contract reminders (docs/architecture.md, "Contract with cratedigger")

- Never re-encode. Tag edits, renames and moves are free.
- Store extracted features by `audio_hash`, not by path or track ID, so renames and moves never trigger re-extraction.
- Cover art: embed it as the front cover (picture type 3), or put `cover.jpg`/`folder.jpg` next to the files.
- The player only sees the library after it reaches the server, and `refresh` runs **on the server**: `python -m music.maintenance refresh` from the player's venv with its env loaded, after each sync finishes, not after each local batch.

### Syncing from the local drive to the server

Cratedigger works on a local copy that is then synced to the server. The server only ever sees each sync's end result, which matters in three ways:

- **Ogg, Opus and WMA files (about 1%) are hashed whole, tags included.** If a file is retagged *and* moved between two syncs, the server sees a new file and gives it a new track ID, which drops it from phone playlists and favourites. Doing the two steps in separate local passes doesn't help if both land in one sync. For these formats: retag, sync, refresh, then move, sync, refresh. MP3, FLAC, M4A and WAV don't have this problem.
- **Propagate deletions.** A move is a delete plus a new file on the server side. A sync that doesn't delete (rsync without `--delete`, for example) leaves the old copy, and the library ends up with duplicates. Plain rsync also re-sends renamed files in full; `--fuzzy` or a rename-aware tool avoids that.
- **Let the sync finish before refreshing.** A refresh mid-sync (the nightly timer can start one) sees moved files as deleted. The next refresh links them back to their old track IDs by audio hash, so nothing is lost, but phones may briefly see them as gone. Avoid syncing across the nightly refresh time.
- The player doesn't read `.m3u8` playlists yet; serving `Playlists/` to the app is a small player feature to add when the lists are converted.

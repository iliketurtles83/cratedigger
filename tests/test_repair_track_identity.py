from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

import config
from scripts.repair_track_identity import (
    ArtistCatalogs,
    TrackCandidate,
    _album_relocations,
    classify_candidate,
    collect_candidates,
    load_identity_cache,
    relocate_album,
    relocate_track,
    repair_track_identity,
    save_identity_cache,
)
from lib.parsers import normalise_title


class RepairTrackIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.cache_path = self.root / "identity.json"
        self.patchers = [
            patch.object(config, "MUSIC_ROOT", self.root),
            patch.object(config, "AUDIO_EXTENSIONS", {".mp3"}),
            patch.object(config, "LETTER_BUCKETS", set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
            patch.object(config, "SYMBOL_BUCKET", "#"),
            patch.object(config, "ROOT_SINGLES_FOLDER", "0singles"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tempdir.cleanup)
        self.written: list[tuple[Path, dict[str, str]]] = []
        read_patch = patch(
            "scripts.repair_track_identity.read_tags",
            side_effect=self._read,
        )
        write_patch = patch(
            "scripts.repair_track_identity.write_tags",
            side_effect=self._write_tags,
        )
        hash_patch = patch(
            "scripts.repair_track_identity.audio_hash",
            side_effect=lambda path: f"a1:{Path(path).name}",
        )
        for patcher in (read_patch, write_patch, hash_patch):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _read(self, path) -> dict[str, str]:
        path = Path(path)
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_tags(self, path, updates, *, dry_run=True):
        path = Path(path)
        if not dry_run:
            tags = self._read(path)
            tags.update(updates)
            path.write_text(json.dumps(tags), encoding="utf-8")
            self.written.append((path, dict(updates)))
        return dict(updates)

    def _track(self, relative: str, **tags: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(tags), encoding="utf-8")
        return path

    def _catalogs(self, *recordings: str, releases: tuple[str, ...] = ()) -> ArtistCatalogs:
        return ArtistCatalogs(
            mb_id="artist-id",
            releases={normalise_title(title) for title in releases},
            recordings={normalise_title(title) for title in recordings},
        )

    def _candidate(self, path: Path, artist: str, title: str) -> TrackCandidate:
        return TrackCandidate(
            path=path,
            artist_folder=path.parent.parent,
            expected_artist=artist,
            artist_mb_id="artist-id",
            title=title,
            album="Album",
            digest=f"a1:{path.name}",
        )

    def test_collect_candidates_skips_catalogued_tracks(self) -> None:
        folder = self.root / "K" / "Kid606"
        self._track(
            "K/Kid606/Kid606 - Album (2002)/01 Known.mp3",
            albumartist="Kid606",
            title="Known Song",
            album="Album",
        )
        unknown = self._track(
            "K/Kid606/Kid606 - Album (2002)/02 Getranke Nasty.mp3",
            albumartist="Kid606",
            title="Getranke Nasty",
            album="Album",
        )

        candidates = collect_candidates(folder, self._catalogs("Known Song"))

        self.assertEqual([c.path for c in candidates], [unknown])
        self.assertEqual(candidates[0].expected_artist, "Kid606")

    def test_collect_candidates_accepts_release_title_evidence(self) -> None:
        folder = self.root / "K" / "Kid606"
        self._track(
            "K/Kid606/Kid606 - Album (2002)/01 Track.mp3",
            albumartist="Kid606",
            title="Anything",
            album="Known Album",
        )

        candidates = collect_candidates(
            folder,
            self._catalogs(releases=("Known Album",)),
        )

        self.assertEqual(candidates, [])

    def test_classify_requires_cached_evidence(self) -> None:
        path = self._track("K/Kid606/Kid606 - Album (2002)/02 X.mp3")
        decision = classify_candidate(
            self._candidate(path, "Kid606", "Getranke Nasty"),
            {},
            self._catalogs("Getränke Nasty"),
        )
        self.assertEqual(decision.action, "unknown")

    def test_classify_confirms_title_correction(self) -> None:
        path = self._track("K/Kid606/Kid606 - Album (2002)/02 X.mp3")
        cache = {
            f"a1:{path.name}": {
                "artist": "Kid606",
                "title": "Getränke Nasty",
                "recording_id": "48292427",
            }
        }
        decision = classify_candidate(
            self._candidate(path, "Kid606", "Getranke Nasty"),
            cache,
            self._catalogs("Getränke Nasty"),
        )
        self.assertEqual(decision.action, "title")
        self.assertEqual(decision.fingerprint_title, "Getränke Nasty")

    def test_classify_rejects_title_absent_from_catalog(self) -> None:
        path = self._track("K/Kid606/Kid606 - Album (2002)/02 X.mp3")
        cache = {
            f"a1:{path.name}": {
                "artist": "Kid606",
                "title": "Some Other Thing",
                "recording_id": "x",
            }
        }
        decision = classify_candidate(
            self._candidate(path, "Kid606", "Getranke Nasty"),
            cache,
            self._catalogs("Getränke Nasty"),
        )
        self.assertEqual(decision.action, "unverified")

    def test_classify_flags_misattribution(self) -> None:
        path = self._track("K/KISS/KISS - Hits (1996)/03 She.mp3")
        cache = {
            f"a1:{path.name}": {
                "artist": "Anthrax",
                "title": "She",
                "recording_id": "rec",
            }
        }
        decision = classify_candidate(
            self._candidate(path, "KISS", "She (Antrax)"),
            cache,
            self._catalogs("Strutter"),
        )
        self.assertEqual(decision.action, "misattributed")
        self.assertEqual(decision.fingerprint_artist, "Anthrax")

    def test_classify_reports_no_match(self) -> None:
        path = self._track("K/Kid606/Kid606 - Album (2002)/02 X.mp3")
        decision = classify_candidate(
            self._candidate(path, "Kid606", "Mystery"),
            {f"a1:{path.name}": None},
            self._catalogs("Known"),
        )
        self.assertEqual(decision.action, "unmatched")

    def test_album_relocation_requires_dominant_agreement(self) -> None:
        full = [
            self._track(f"C/Chemical Brothers/Chemical Brothers - Fight Club (1999)/{n:02d}.mp3")
            for n in (1, 2)
        ]
        partial_folder = "K/KISS/KISS - Hits (1996)"
        self._track(f"{partial_folder}/01 Strutter.mp3")
        loose_track = self._track(f"{partial_folder}/02 She.mp3")

        decisions = []
        for path in full:
            candidate = self._candidate(path, "Chemical Brothers", "x")
            decisions.append(
                classify_candidate(
                    candidate,
                    {f"a1:{path.name}": {
                        "artist": "The Dust Brothers",
                        "title": "x",
                        "recording_id": "r",
                    }},
                    self._catalogs(),
                )
            )
        decisions.append(
            classify_candidate(
                self._candidate(loose_track, "KISS", "She (Antrax)"),
                {f"a1:{loose_track.name}": {
                    "artist": "Anthrax",
                    "title": "She",
                    "recording_id": "r",
                }},
                self._catalogs(),
            )
        )

        albums, loose = _album_relocations(decisions)

        self.assertEqual(
            albums,
            {full[0].parent: "The Dust Brothers"},
        )
        self.assertEqual([d.candidate.path for d in loose], [loose_track])

    def test_relocate_album_moves_and_retags(self) -> None:
        source = self.root / "C" / "Chemical Brothers" / "Chemical Brothers - Fight Club (1999)"
        track = self._track(
            "C/Chemical Brothers/Chemical Brothers - Fight Club (1999)/01 Track.mp3",
            albumartist="Chemical Brothers",
            artist="Chemical Brothers",
        )
        path_map: dict[Path, Path] = {}

        destination = relocate_album(
            source,
            "The Dust Brothers",
            "Chemical Brothers",
            path_map,
            dry_run=False,
        )

        self.assertEqual(
            destination,
            self.root / "D" / "The Dust Brothers" / "The Dust Brothers - Fight Club (1999)",
        )
        self.assertTrue((destination / "01 Track.mp3").exists())
        self.assertFalse(track.exists())
        self.assertEqual(path_map[track], destination / "01 Track.mp3")
        moved_tags = self._read(destination / "01 Track.mp3")
        self.assertEqual(moved_tags["albumartist"], "The Dust Brothers")
        self.assertEqual(moved_tags["artist"], "The Dust Brothers")

    def test_soundtrack_relocates_with_guest_credit_intact(self) -> None:
        folder = "C/Chemical Brothers/Chemical Brothers - Fight Club (1999)"
        tracks = []
        for number in range(1, 6):
            tracks.append(
                self._track(
                    f"{folder}/{number:02d} Track.mp3",
                    albumartist="Chemical Brothers [as Dust Bros.]",
                    artist="Chemical Brothers [as Dust Bros.]",
                )
            )
        decisions = []
        for index, path in enumerate(tracks[:4]):
            artist = "Pixies" if index == 3 else "The Dust Brothers"
            decisions.append(
                classify_candidate(
                    self._candidate(path, "The Chemical Brothers", "x"),
                    {f"a1:{path.name}": {
                        "artist": artist,
                        "title": "x",
                        "recording_id": "r",
                    }},
                    self._catalogs(),
                )
            )

        albums, loose = _album_relocations(decisions)

        self.assertEqual(albums, {self.root / folder: "The Dust Brothers"})
        self.assertEqual(loose, [])

        destination = relocate_album(
            self.root / folder,
            "The Dust Brothers",
            "Chemical Brothers",
            {},
            track_credits={
                d.candidate.path: d.fingerprint_artist for d in decisions
            },
            dry_run=False,
        )

        self.assertEqual(
            destination,
            self.root / "D" / "The Dust Brothers" / "The Dust Brothers - Fight Club (1999)",
        )
        for number in range(1, 6):
            tags = self._read(destination / f"{number:02d} Track.mp3")
            self.assertEqual(tags["albumartist"], "The Dust Brothers")
            expected = "Pixies" if number == 4 else "The Dust Brothers"
            self.assertEqual(tags["artist"], expected)

    def test_classify_rejects_placeholder_title(self) -> None:
        path = self._track("B/Boards Of Canada/Album (2002)/02 X.mp3")
        cache = {
            f"a1:{path.name}": {
                "artist": "Boards of Canada",
                "title": "[unknown]",
                "recording_id": "rec",
            }
        }
        decision = classify_candidate(
            self._candidate(path, "Boards of Canada", "Live set"),
            cache,
            self._catalogs("[unknown]"),
        )
        self.assertEqual(decision.action, "unverified")

    def test_relocate_track_moves_to_singles_pen(self) -> None:
        track = self._track(
            "K/KISS/KISS - Hits (1996)/03 She.mp3",
            albumartist="KISS",
            artist="KISS",
            album="Hits",
            title="She (Antrax)",
        )
        decision = classify_candidate(
            self._candidate(track, "KISS", "She (Antrax)"),
            {f"a1:{track.name}": {
                "artist": "Anthrax",
                "title": "She",
                "recording_id": "rec-id",
            }},
            self._catalogs(),
        )
        path_map: dict[Path, Path] = {}

        destination = relocate_track(decision, path_map, dry_run=False)

        self.assertEqual(destination, self.root / "0singles" / "Anthrax - She.mp3")
        self.assertTrue(destination.exists())
        self.assertFalse(track.exists())
        tags = self._read(destination)
        self.assertEqual(tags["artist"], "Anthrax")
        self.assertEqual(tags["albumartist"], "Anthrax")
        self.assertEqual(tags["title"], "She")
        self.assertEqual(tags["album"], "")
        self.assertEqual(tags["mb_trackid"], "rec-id")

    def test_dry_run_writes_nothing(self) -> None:
        self._track(
            "K/Kid606/Kid606 - Album (2002)/02 Getranke Nasty.mp3",
            albumartist="Kid606",
            mb_albumartistid="artist-id",
            title="Getranke Nasty",
            album="Album",
        )
        save_identity_cache(
            self.cache_path,
            {
                "a1:02 Getranke Nasty.mp3": {
                    "artist": "Kid606",
                    "title": "Getränke Nasty",
                    "recording_id": "rec",
                }
            },
        )

        with patch.object(config, "ACOUSTID_IDENTITY_CACHE", self.cache_path, create=True), \
                patch.object(
                    config,
                    "MB_RECORDING_CACHE",
                    self._recording_cache({"artist-id": ["Getränke Nasty"]}),
                    create=True,
                ), \
                patch.object(
                    config, "MB_RELEASE_GROUP_CACHE", self.root / "missing.json", create=True
                ):
            decisions = repair_track_identity(dry_run=True, fix=True)

        self.assertEqual([d.action for d in decisions], ["title"])
        self.assertEqual(self.written, [])

    def test_live_run_applies_cached_title_fix(self) -> None:
        track = self._track(
            "K/Kid606/Kid606 - Album (2002)/02 Getranke Nasty.mp3",
            albumartist="Kid606",
            mb_albumartistid="artist-id",
            title="Getranke Nasty",
            album="Album",
        )
        save_identity_cache(
            self.cache_path,
            {
                "a1:02 Getranke Nasty.mp3": {
                    "artist": "Kid606",
                    "title": "Getränke Nasty",
                    "recording_id": "rec",
                }
            },
        )

        with patch.object(config, "ACOUSTID_IDENTITY_CACHE", self.cache_path, create=True), \
                patch.object(
                    config,
                    "MB_RECORDING_CACHE",
                    self._recording_cache({"artist-id": ["Getränke Nasty"]}),
                    create=True,
                ), \
                patch.object(
                    config, "MB_RELEASE_GROUP_CACHE", self.root / "missing.json", create=True
                ):
            repair_track_identity(dry_run=False, fix=True)

        stored = self._read(track)
        self.assertEqual(stored["title"], "Getränke Nasty")
        self.assertEqual(stored["mb_trackid"], "rec")

    def test_online_fingerprints_refused_outside_dry_run(self) -> None:
        with patch("scripts.repair_track_identity.fingerprint_lookup") as lookup:
            decisions = repair_track_identity(
                dry_run=False, fix=True, online_fingerprints=True
            )
        self.assertEqual(decisions, [])
        lookup.assert_not_called()

    def test_identity_cache_round_trip(self) -> None:
        payload = {"a1:x": {"artist": "A", "title": "T", "recording_id": "r"}, "a1:y": None}
        save_identity_cache(self.cache_path, payload)
        self.assertEqual(load_identity_cache(self.cache_path), payload)

    def _recording_cache(self, payload: dict[str, list[str]]) -> Path:
        path = self.root / "recordings.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()

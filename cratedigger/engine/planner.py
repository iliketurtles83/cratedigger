"""Plan builder: converts a ScanResult into deterministic SQLite plan_actions & review items."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any

from cratedigger.config_schema import Settings, load_settings
from cratedigger.core import (
    artist_letter_bucket,
    audio_hash,
    build_filename,
    is_suspicious_tag,
    sanitise_component,
    sanitise_name,
    validate_year,
)
from cratedigger.core.artists import (
    build_artist_index,
    format_artists_tag,
    snap_artist_name,
    split_featured_artists,
)
from cratedigger.db import utc_now_iso
from cratedigger.engine.metadata import (
    beets_match_album,
    lookup_track_metadata,
    resolve_genre_4step,
)
from cratedigger.engine.scanner import ScanResult, ScanUnit, ScannedTrack, scan_scope
from scripts.generate_genre_playlists import resolve_root_genre
from scripts.quarantine_album_duplicates import score_track_candidate

log = logging.getLogger("cratedigger.engine.planner")


def _insert_review_item(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    queue_type: str,
    reason: str,
    path: Path,
    audio_hash_val: str | None,
    payload: dict[str, Any],
) -> int:
    ts = utc_now_iso()
    conn.execute(
        """
        INSERT INTO review_items (item_key, queue_type, reason, path, audio_hash, payload_json, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(item_key) DO UPDATE SET
            queue_type = excluded.queue_type,
            reason = excluded.reason,
            path = excluded.path,
            audio_hash = COALESCE(excluded.audio_hash, review_items.audio_hash),
            payload_json = excluded.payload_json
        """,
        (item_key, queue_type, reason, str(path), audio_hash_val, json.dumps(payload, ensure_ascii=False), ts),
    )
    row = conn.execute("SELECT id FROM review_items WHERE item_key = ?", (item_key,)).fetchone()
    return int(row["id"])


def _effective_tags(
    track: ScannedTrack,
    *,
    mb_track: dict[str, Any] | None = None,
    beets_item: dict[str, Any] | None = None,
) -> dict[str, str]:
    merged: dict[str, str] = {k: (v or "").strip() for k, v in track.tags.items()}
    for src in (mb_track or {}, beets_item or {}):
        for k in (
            "artist",
            "artists",
            "albumartist",
            "album",
            "title",
            "year",
            "track",
            "disc",
            "compilation",
            "mb_trackid",
            "mb_albumid",
            "mb_artistid",
            "mb_albumartistid",
            "mb_releasegroupid",
        ):
            if not merged.get(k) and src.get(k):
                merged[k] = str(src[k]).strip()

    # Fall back cleanly to filename parsing when tags lack artist/title/track
    if not merged.get("artist") and track.parsed.get("artist"):
        merged["artist"] = str(track.parsed["artist"]).strip()
    if not merged.get("title") and track.parsed.get("title"):
        merged["title"] = str(track.parsed["title"]).strip()
    if not merged.get("track") and track.parsed.get("track"):
        merged["track"] = str(track.parsed["track"]).strip()
    if not merged.get("disc") and track.parsed.get("disc"):
        merged["disc"] = str(track.parsed["disc"]).strip()
    return merged


def _build_album_dir(
    *,
    music_root: Path,
    artist_dir: Path,
    artist_name: str,
    album: str,
    year: str,
) -> Path:
    clean_album = sanitise_component(sanitise_name(album or "Unknown Album"))
    clean_artist = sanitise_component(artist_name)
    folder_name = f"{clean_artist} - {clean_album}"
    if year:
        folder_name = f"{folder_name} ({year})"
    return artist_dir / sanitise_component(folder_name)


def _build_compilation_dir(
    *,
    music_root: Path,
    album: str,
    year: str,
) -> Path:
    clean_album = sanitise_component(sanitise_name(album or "Unknown Compilation"))
    folder_name = f"{clean_album} ({year})" if year else clean_album
    return music_root / "0compilations" / sanitise_component(folder_name)


def _auto_consensus_value(tracks_tags: list[dict[str, str]], field: str, threshold: float) -> str | None:
    vals = [t.get(field, "").strip() for t in tracks_tags if t.get(field, "").strip()]
    if not vals or len(set(vals)) <= 1:
        return vals[0] if vals else None
    top_val, top_cnt = Counter(vals).most_common(1)[0]
    if top_cnt / len(tracks_tags) >= threshold:
        return top_val
    return None


def build_plan(
    scope: Path,
    conn: sqlite3.Connection,
    *,
    settings: Settings | None = None,
    scan_result: ScanResult | None = None,
    online: bool = False,
    use_effnet: bool = False,
    progress_cb=None,
) -> dict[str, Any]:
    """Scan *scope*, resolve metadata, build ordered plan actions, and persist in SQLite."""
    settings = settings or load_settings(conn)
    music_root = settings.music_root_path()
    build_artist_index(conn, music_root)

    if scan_result is None:
        scan_result = scan_scope(scope, settings=settings, conn=conn, progress_cb=progress_cb)

    actions: list[dict[str, Any]] = []
    review_created = 0
    seen_hashes_in_scan: dict[str, Path] = {}
    planned_targets: dict[str, str] = {}  # lower resolved path -> audio_hash
    planned_singles_for_playlists: list[dict[str, str]] = []
    dissolved_dirs: set[Path] = set()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def add_action(
        action_type: str,
        *,
        source_path: Path | None = None,
        target_path: Path | None = None,
        ah: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        actions.append(
            {
                "action_type": action_type,
                "source_path": str(source_path) if source_path else None,
                "target_path": str(target_path) if target_path else None,
                "audio_hash": ah,
                "payload": payload or {},
            }
        )

    for unit in scan_result.units:
        if unit.kind == "exempt_mix":
            continue

        if "full_album_inside_mix_folder" in unit.notes:
            _insert_review_item(
                conn,
                item_key=f"mix_album:{unit.folder}",
                queue_type="actionable",
                reason="mix_folder_album_detected",
                path=unit.folder,
                audio_hash_val=None,
                payload={"folder": str(unit.folder), "track_count": len(unit.tracks)},
            )
            review_created += 1
            continue

        if unit.kind == "multi_artist_review":
            _insert_review_item(
                conn,
                item_key=f"multi_artist:{unit.folder}",
                queue_type="actionable",
                reason="multi_artist_folder_intent",
                path=unit.folder,
                audio_hash_val=None,
                payload={
                    "folder": str(unit.folder),
                    "track_count": len(unit.tracks),
                    "choices": ["compilation", "playlist", "loose_singles"],
                },
            )
            review_created += 1
            continue

        if unit.ambiguous_covers:
            _insert_review_item(
                conn,
                item_key=f"cover:{unit.folder}",
                queue_type="actionable",
                reason="ambiguous_cover_art",
                path=unit.folder,
                audio_hash_val=None,
                payload={"candidates": [p.name for p in unit.image_candidates]},
            )
            review_created += 1

        # Optional transient no-write Beets album lookup for album/compilation units
        beets_by_path: dict[str, dict[str, Any]] = {}
        beets_match = None
        if online and unit.kind in {"album", "compilation", "soundtrack_score", "soundtrack_va"}:
            beets_match = beets_match_album(unit.folder, conn=conn, online=True)
            if beets_match and beets_match.get("items"):
                for bit in beets_match["items"]:
                    beets_by_path[str(Path(bit["path"]).resolve())] = bit

        # Prepare per-track enriched metadata
        unit_tags: list[dict[str, str]] = []
        mb_per_track: list[dict[str, Any]] = []
        for tr in unit.tracks:
            b_item = beets_by_path.get(str(tr.path.resolve()))
            mb_data = (
                lookup_track_metadata(tr.path, conn, audio_hash_val=tr.audio_hash, tags=tr.tags, online=online)
                if online and not b_item
                else {}
            )
            mb_per_track.append(mb_data)
            unit_tags.append(_effective_tags(tr, mb_track=mb_data, beets_item=b_item))

        # Auto-triage consensus album / year across multi-track release units
        is_album_like = unit.kind in {"album", "compilation", "soundtrack_score", "soundtrack_va"}
        consensus_album = _auto_consensus_value(unit_tags, "album", settings.consensus_auto_threshold) if is_album_like else None
        consensus_year = _auto_consensus_value(unit_tags, "year", settings.consensus_auto_threshold) if is_album_like else None
        if consensus_year:
            consensus_year = validate_year(consensus_year) or ""

        if "tag_conflict:album" in unit.notes and not consensus_album:
            _insert_review_item(
                conn,
                item_key=f"conflict:album:{unit.folder}",
                queue_type="actionable",
                reason="mixed_tags",
                path=unit.folder,
                audio_hash_val=None,
                payload={"field": "album", "reason": "mixed_tags"},
            )
            review_created += 1
        if "tag_conflict:year" in unit.notes and not consensus_year:
            _insert_review_item(
                conn,
                item_key=f"conflict:year:{unit.folder}",
                queue_type="actionable",
                reason="mixed_tags",
                path=unit.folder,
                audio_hash_val=None,
                payload={"field": "year", "reason": "mixed_tags"},
            )
            review_created += 1

        # Curated list conversion plan
        if unit.kind == "curated_list":
            playlists_dir = settings.playlists_path()
            if playlists_dir == Path("Playlists"):
                playlists_dir = music_root / "Playlists"
            pl_path = playlists_dir / f"{sanitise_component(unit.folder.name)}.m3u8"
            pl_entries: list[str] = []

            for idx, tr in enumerate(unit.tracks):
                tags = unit_tags[idx]
                row = conn.execute(
                    "SELECT canonical_path FROM tracks WHERE audio_hash = ?",
                    (tr.audio_hash,),
                ).fetchone()
                canon = Path(row["canonical_path"]) if row and row["canonical_path"] else None
                if canon and canon.exists():
                    add_action(
                        "quarantine_duplicate",
                        source_path=tr.path,
                        target_path=canon,
                        ah=tr.audio_hash,
                        payload={"keep": str(canon), "drop": str(tr.path)},
                    )
                    pl_entries.append(str(canon))
                    continue

                primary, _ = split_featured_artists(tags.get("artist"), title=tags.get("title"))
                canon_artist, _, artist_dir = snap_artist_name(
                    conn,
                    primary or "Unknown Artist",
                    music_root=music_root,
                    mb_artistid=tags.get("mb_artistid") or None,
                    letter_bucketing=settings.letter_bucketing,
                )
                ext = ".mp3" if tr.needs_transcode else tr.path.suffix.lower()
                fname = build_filename(None, canon_artist, sanitise_name(tags.get("title") or tr.path.stem), ext)
                dest = artist_dir / "0singles" / fname
                if tr.needs_transcode:
                    add_action(
                        "transcode",
                        source_path=tr.path,
                        target_path=dest,
                        ah=tr.audio_hash,
                        payload={"bitrate": settings.transcode_bitrate, "clear_album": True},
                    )
                elif tr.path.resolve() != dest.resolve():
                    add_action("move", source_path=tr.path, target_path=dest, ah=tr.audio_hash, payload={"kind": "list_single"})
                pl_entries.append(str(dest))

            add_action(
                "write_playlist",
                source_path=unit.folder,
                target_path=pl_path,
                payload={"entries": pl_entries, "mode": "replace"},
            )
            dissolved_dirs.add(unit.folder)
            continue

        # Process tracks within unit
        unit_dest_dir: Path | None = None
        if unit.kind in {"compilation", "soundtrack_va"}:
            alb = consensus_album or (unit_tags[0].get("album") if unit_tags else "") or unit.folder.name
            yr = consensus_year or ""
            unit_dest_dir = _build_compilation_dir(music_root=music_root, album=alb, year=yr)
        elif unit.kind in {"album", "soundtrack_score"}:
            rep = unit_tags[0] if unit_tags else {}
            raw_aa = rep.get("albumartist") or rep.get("artist") or ""
            primary_aa, _ = split_featured_artists(raw_aa, title=rep.get("title"))
            canon_artist, _, artist_dir = snap_artist_name(
                conn,
                primary_aa,
                music_root=music_root,
                mb_artistid=rep.get("mb_albumartistid") or rep.get("mb_artistid") or None,
                letter_bucketing=settings.letter_bucketing,
            )
            alb = consensus_album or rep.get("album") or unit.folder.name
            yr = consensus_year or validate_year(rep.get("year")) or ""
            unit_dest_dir = _build_album_dir(
                music_root=music_root,
                artist_dir=artist_dir,
                artist_name=canon_artist,
                album=alb,
                year=yr,
            )

        for idx, tr in enumerate(unit.tracks):
            tags = unit_tags[idx]
            mb_data = mb_per_track[idx]

            # 1. Duplicate audio_hash detection (library DB OR earlier track in same scan)
            existing_dups = [Path(p) for p in tr.library_duplicate_paths if Path(p).exists()]
            if not existing_dups and tr.audio_hash in seen_hashes_in_scan:
                existing_dups = [seen_hashes_in_scan[tr.audio_hash]]

            if existing_dups:
                candidates = [tr.path, *existing_dups]
                # Prefer already-canonical Tier 1 copy or highest filename score
                canon_db = conn.execute(
                    "SELECT canonical_path FROM tracks WHERE audio_hash = ?",
                    (tr.audio_hash,),
                ).fetchone()
                canon_p = Path(canon_db["canonical_path"]) if canon_db and canon_db["canonical_path"] else None
                if canon_p and canon_p.exists() and canon_p in candidates and canon_p.resolve() != tr.path.resolve():
                    keep_p = canon_p
                else:
                    candidates.sort(key=score_track_candidate, reverse=True)
                    keep_p = candidates[0]
                if keep_p.resolve() != tr.path.resolve():
                    add_action(
                        "quarantine_duplicate",
                        source_path=tr.path,
                        target_path=keep_p,
                        ah=tr.audio_hash,
                        payload={"keep": str(keep_p), "drop": str(tr.path)},
                    )
                    if unit.kind == "small_release_singles":
                        dissolved_dirs.add(unit.folder)
                    continue
            seen_hashes_in_scan.setdefault(tr.audio_hash, tr.path)

            # 2. Compute metadata updates (featured artists, genre 4-step, soundtrack, singles)
            tag_updates: dict[str, str] = {}
            raw_artist = tags.get("artist") or ""
            raw_title = tags.get("title") or ""
            primary_artist, all_artists = split_featured_artists(raw_artist, title=raw_title)

            if is_suspicious_tag(primary_artist, "artist") or is_suspicious_tag(raw_title, "title"):
                _insert_review_item(
                    conn,
                    item_key=f"placeholder:{tr.path}",
                    queue_type="actionable",
                    reason="placeholder_tags",
                    path=tr.path,
                    audio_hash_val=tr.audio_hash,
                    payload={"artist": raw_artist, "title": raw_title},
                )
                review_created += 1
                continue

            if all_artists:
                formatted_artists = format_artists_tag(all_artists)
                if formatted_artists and formatted_artists != (tr.tags.get("artists") or ""):
                    tag_updates["artists"] = formatted_artists

            # Genre 4-step
            genre_res = resolve_genre_4step(
                tr.path,
                tags,
                conn,
                settings=settings,
                mb_data=mb_data,
                online=online,
                use_effnet=use_effnet,
            )
            eff_genre = genre_res["genre"] or ""
            if unit.kind in {"soundtrack_score", "soundtrack_va"} and "soundtrack" not in eff_genre.casefold():
                eff_genre = "Soundtrack"
            if eff_genre and genre_res["action"] in {"apply", "keep_or_normalize"}:
                if eff_genre != (tr.tags.get("genre") or ""):
                    tag_updates["genre"] = eff_genre
            elif genre_res["action"] == "review":
                _insert_review_item(
                    conn,
                    item_key=f"genre:{tr.path}",
                    queue_type="actionable",
                    reason="no_genre",
                    path=tr.path,
                    audio_hash_val=tr.audio_hash,
                    payload={
                        "reason": "no_genre",
                        "suggested_genre": genre_res.get("genre"),
                        "confidence": genre_res.get("confidence", 0.0),
                        "source": genre_res.get("source"),
                    },
                )
                review_created += 1

            # Determine single vs album routing
            is_single_route = unit.kind in {"small_release_singles", "loose_singles", "singles_folder"}
            if is_single_route:
                # Dissolve album tag so player never displays 1-2 track album fragments
                if tr.tags.get("album"):
                    tag_updates["album"] = ""
                if primary_artist and primary_artist != (tr.tags.get("albumartist") or ""):
                    tag_updates["albumartist"] = primary_artist
                if tr.tags.get("compilation") != "0":
                    tag_updates["compilation"] = "0"
            else:
                if consensus_album and consensus_album != (tr.tags.get("album") or ""):
                    tag_updates["album"] = consensus_album
                if consensus_year and consensus_year != (tr.tags.get("year") or ""):
                    tag_updates["year"] = consensus_year
                if unit.kind in {"compilation", "soundtrack_va"}:
                    tag_updates["albumartist"] = "Various Artists"
                    tag_updates["compilation"] = "1"
                elif primary_artist:
                    canon_aa, _, _ = snap_artist_name(
                        conn,
                        primary_artist,
                        music_root=music_root,
                        mb_artistid=tags.get("mb_albumartistid") or tags.get("mb_artistid") or None,
                        letter_bucketing=settings.letter_bucketing,
                    )
                    if canon_aa != (tr.tags.get("albumartist") or ""):
                        tag_updates["albumartist"] = canon_aa

            # Copy resolved MBIDs when present
            for mb_field in ("mb_trackid", "mb_albumid", "mb_artistid", "mb_albumartistid", "mb_releasegroupid"):
                if tags.get(mb_field) and not tr.tags.get(mb_field):
                    tag_updates[mb_field] = tags[mb_field]

            if tag_updates:
                add_action("write_tags", source_path=tr.path, ah=tr.audio_hash, payload={"updates": tag_updates})

            # 3. Artwork handling for dissolved small releases (embed before moving)
            if unit.kind == "small_release_singles" and settings.embed_single_covers:
                add_action(
                    "embed_artwork",
                    source_path=tr.path,
                    ah=tr.audio_hash,
                    payload={
                        "folder_cover": str(unit.cover_path) if unit.cover_path else None,
                        "mb_albumid": tags.get("mb_albumid") or None,
                        "max_dim": settings.cover_max_dim,
                    },
                )
                dissolved_dirs.add(unit.folder)

            # 4. Target path & transcode/move
            ext = ".mp3" if tr.needs_transcode else tr.path.suffix.lower()
            if is_single_route:
                canon_artist, _, artist_dir = snap_artist_name(
                    conn,
                    primary_artist,
                    music_root=music_root,
                    mb_artistid=tags.get("mb_albumartistid") or tags.get("mb_artistid") or None,
                    letter_bucketing=settings.letter_bucketing,
                )
                fname = build_filename(None, canon_artist, sanitise_name(raw_title), ext)
                target = artist_dir / "0singles" / fname
            else:
                assert unit_dest_dir is not None
                disc_dir = unit_dest_dir / tr.path.parent.name if unit.folder != tr.path.parent and unit.folder in tr.path.parents else unit_dest_dir
                fname = build_filename(
                    tags.get("track"),
                    raw_artist,
                    sanitise_name(raw_title),
                    ext,
                    disc=tags.get("disc"),
                )
                target = disc_dir / fname

            existing_on_disk: Path | None = target if target.exists() else None
            if existing_on_disk is None and target.parent.is_dir():
                for sib in target.parent.iterdir():
                    if sib.is_file() and sib.name.casefold() == target.name.casefold():
                        existing_on_disk = sib
                        break

            target_key = str(target).casefold()
            conflict = False
            if existing_on_disk is not None and existing_on_disk.resolve() != tr.path.resolve():
                existing_ah = audio_hash(str(existing_on_disk))
                if existing_ah == tr.audio_hash:
                    add_action(
                        "quarantine_duplicate",
                        source_path=tr.path,
                        target_path=existing_on_disk,
                        ah=tr.audio_hash,
                        payload={"keep": str(existing_on_disk), "drop": str(tr.path)},
                    )
                    continue
                conflict = True
            elif target_key in planned_targets and planned_targets[target_key] != tr.audio_hash:
                conflict = True

            if conflict:
                _insert_review_item(
                    conn,
                    item_key=f"rename_conflict:{tr.path}->{target}",
                    queue_type="actionable",
                    reason="rename_conflict",
                    path=tr.path,
                    audio_hash_val=tr.audio_hash,
                    payload={"reason": "rename_conflict", "target": str(target)},
                )
                review_created += 1
                continue

            planned_targets[target_key] = tr.audio_hash

            if tr.needs_transcode:
                add_action(
                    "transcode",
                    source_path=tr.path,
                    target_path=target,
                    ah=tr.audio_hash,
                    payload={
                        "bitrate": settings.transcode_bitrate,
                        "max_cover_dim": settings.cover_max_dim,
                        "is_single": is_single_route,
                    },
                )
            elif tr.path.resolve() != target.resolve():
                add_action(
                    "move",
                    source_path=tr.path,
                    target_path=target,
                    ah=tr.audio_hash,
                    payload={"kind": unit.kind},
                )

            if is_single_route:
                planned_singles_for_playlists.append(
                    {
                        "path": str(target),
                        "root_genre": resolve_root_genre(eff_genre or tr.tags.get("genre")),
                    }
                )

        # Album cover transfer when folder relocates
        if (
            unit.kind in {"album", "compilation", "soundtrack_score", "soundtrack_va"}
            and unit.cover_path
            and unit_dest_dir
            and unit.folder.resolve() != unit_dest_dir.resolve()
        ):
            add_action(
                "copy_cover",
                source_path=unit.cover_path,
                target_path=unit_dest_dir / "cover.jpg",
                payload={"max_dim": settings.cover_max_dim},
            )

    if planned_singles_for_playlists and (settings.auto_update_singles_playlists or settings.auto_update_intake_playlist):
        add_action(
            "update_singles_playlists",
            target_path=settings.playlists_path() if settings.playlists_path() != Path("Playlists") else music_root / "Playlists",
            payload={
                "singles": planned_singles_for_playlists,
                "run_date": run_date,
                "auto_intake": settings.auto_update_intake_playlist,
                "auto_genre": settings.auto_update_singles_playlists,
                "intake_prefix": settings.intake_playlist_prefix,
            },
        )

    for d in sorted(dissolved_dirs):
        add_action("cleanup_dir", source_path=d, payload={})

    by_type = dict(Counter(a["action_type"] for a in actions))
    summary = {
        "scope": str(scope),
        "units": len(scan_result.units),
        "total_tracks": scan_result.total_tracks,
        "actions_count": len(actions),
        "actions_by_type": by_type,
        "review_items_created": review_created,
    }
    ts = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO plans (scope, status, summary_json, created_at) VALUES (?, 'draft', ?, ?)",
        (str(scope), json.dumps(summary, ensure_ascii=False), ts),
    )
    plan_id = int(cur.lastrowid)
    for a in actions:
        conn.execute(
            """
            INSERT INTO plan_actions (plan_id, action_type, source_path, target_path, audio_hash, payload_json, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                plan_id,
                a["action_type"],
                a["source_path"],
                a["target_path"],
                a["audio_hash"],
                json.dumps(a["payload"], ensure_ascii=False),
            ),
        )
    conn.commit()
    return {"plan_id": plan_id, "summary": summary, "actions": actions}

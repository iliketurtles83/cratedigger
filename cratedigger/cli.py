"""Unified CLI entry point for cratedigger."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from cratedigger.db import DEFAULT_DB_PATH, open_db, review_counts, sync_features_into_db
from cratedigger.review_migration import migrate_review_json

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("cratedigger.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cratedigger", description="Cratedigger library CLI")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite DB path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("db-init", help="Initialize cratedigger.db schema")

    sync_p = sub.add_parser("sync-features", help="Import tracks/paths from features.pkl into cratedigger.db")
    sync_p.add_argument("--features", type=Path, default=Path("features.pkl"))

    mig_p = sub.add_parser("migrate-review", help="Import and auto-triage review.json into cratedigger.db")
    mig_p.add_argument("--review", type=Path, default=Path("review.json"))
    mig_p.add_argument("--dry-run", action="store_true", default=True, help="Preview without writing (default)")
    mig_p.add_argument("--no-dry-run", action="store_true", help="Write migrated items to cratedigger.db")
    mig_p.add_argument("--fix-consensus", action="store_true", help="Apply >=75%% consensus tags to disk")

    sub.add_parser("status", help="Show SQLite store counts")

    ui_p = sub.add_parser("ui", help="Launch local review & quarantine web UI")
    ui_p.add_argument("--host", choices=["127.0.0.1"], default="127.0.0.1", help="Bind to localhost only")
    ui_p.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    ui_p.add_argument("--music-root", type=Path, default=None, help="Override MUSIC_ROOT directory")

    idx_p = sub.add_parser("index-artists", help="Build canonical Tier 1 artist folder index in SQLite")
    idx_p.add_argument("--music-root", type=Path, default=None, help="Override MUSIC_ROOT directory")

    scan_p = sub.add_parser("scan", help="Read-only scan & classification summary for a folder")
    scan_p.add_argument("scope", type=Path, help="Folder or file to scan")
    scan_p.add_argument("--music-root", type=Path, default=None, help="Override MUSIC_ROOT directory")

    plan_p = sub.add_parser("plan", help="Scan folder and persist an execution plan in cratedigger.db")
    plan_p.add_argument("scope", type=Path, help="Folder or file to plan")
    plan_p.add_argument("--music-root", type=Path, default=None, help="Override MUSIC_ROOT directory")
    plan_p.add_argument("--online", action="store_true", help="Enable MusicBrainz / Beets no-write lookups")
    plan_p.add_argument("--effnet", action="store_true", help="Enable Discogs-EffNet genre prediction")
    plan_p.add_argument("--apply", action="store_true", help="Immediately apply plan after building (--no-dry-run)")
    plan_p.add_argument("--no-dry-run", action="store_true", help="Required alongside --apply to execute mutations")

    apply_p = sub.add_parser("apply", help="Execute a persisted plan from cratedigger.db")
    apply_p.add_argument("plan_id", type=int, help="Plan ID to execute")
    apply_p.add_argument("--music-root", type=Path, default=None, help="Override MUSIC_ROOT directory")
    apply_p.add_argument("--dry-run", action="store_true", default=True, help="Preview without mutating files (default)")
    apply_p.add_argument("--no-dry-run", action="store_true", help="Execute file and tag mutations")

    undo_p = sub.add_parser("undo", help="Revert a journaled run_id from undo_journal")
    undo_p.add_argument("run_id", type=str, help="Run ID to revert")
    undo_p.add_argument("--music-root", type=Path, default=None, help="Override MUSIC_ROOT directory")

    args = parser.parse_args(argv)

    if args.command == "db-init":
        with open_db(args.db):
            pass
        log.info("Initialized SQLite schema at %s", args.db)
        return 0

    if args.command == "sync-features":
        with open_db(args.db) as conn:
            res = sync_features_into_db(conn, args.features)
        print(json.dumps(res, indent=2))
        return 0

    if args.command == "migrate-review":
        dry_run = not args.no_dry_run
        if args.fix_consensus and dry_run:
            log.warning("--fix-consensus requested in dry-run mode; tags will not be modified")
        res = migrate_review_json(
            args.review,
            args.db,
            fix_consensus=args.fix_consensus,
            dry_run=dry_run,
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.command == "status":
        with open_db(args.db) as conn:
            tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            paths = conn.execute("SELECT COUNT(*) FROM track_paths").fetchone()[0]
            counts = review_counts(conn)
        print(json.dumps({"tracks": tracks, "track_paths": paths, "review_items": counts}, indent=2))
        return 0

    if args.command == "ui":
        import uvicorn
        from cratedigger.web.app import create_app

        app = create_app(db_path=args.db, music_root=args.music_root)
        log.info("Starting Cratedigger web UI at http://%s:%d/review", args.host, args.port)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    if args.command == "index-artists":
        from cratedigger.config_schema import load_settings
        from cratedigger.core.artists import build_artist_index

        with open_db(args.db) as conn:
            settings = load_settings(conn, music_root=args.music_root)
            recs = build_artist_index(conn, settings.music_root_path())
        print(json.dumps({"indexed_artists": len(recs)}, indent=2))
        return 0

    if args.command == "scan":
        from cratedigger.config_schema import load_settings
        from cratedigger.engine.scanner import scan_scope

        with open_db(args.db) as conn:
            settings = load_settings(conn, music_root=args.music_root)
            res = scan_scope(args.scope, settings=settings, conn=conn)
        out = {
            "scope": str(res.scope),
            "total_tracks": res.total_tracks,
            "units": [
                {
                    "folder": str(u.folder),
                    "kind": u.kind,
                    "tracks": len(u.tracks),
                    "ambiguous_covers": u.ambiguous_covers,
                    "notes": u.notes,
                }
                for u in res.units
            ],
        }
        print(json.dumps(out, indent=2))
        return 0

    if args.command == "plan":
        from cratedigger.config_schema import load_settings
        from cratedigger.engine.executor import execute_plan
        from cratedigger.engine.planner import build_plan

        with open_db(args.db) as conn:
            settings = load_settings(conn, music_root=args.music_root)
            plan_res = build_plan(
                args.scope,
                conn,
                settings=settings,
                online=args.online,
                use_effnet=args.effnet,
            )
            result: dict = {"plan_id": plan_res["plan_id"], "summary": plan_res["summary"]}
            if args.apply:
                dry = not args.no_dry_run
                result["execution"] = execute_plan(conn, plan_res["plan_id"], settings=settings, dry_run=dry)
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "apply":
        from cratedigger.config_schema import load_settings
        from cratedigger.engine.executor import execute_plan

        dry = not args.no_dry_run
        with open_db(args.db) as conn:
            settings = load_settings(conn, music_root=args.music_root)
            res = execute_plan(conn, args.plan_id, settings=settings, dry_run=dry)
        print(json.dumps(res, indent=2))
        return 0

    if args.command == "undo":
        from cratedigger.config_schema import load_settings
        from cratedigger.engine.executor import undo_plan_run

        with open_db(args.db) as conn:
            settings = load_settings(conn, music_root=args.music_root)
            n = undo_plan_run(conn, args.run_id, music_root=settings.music_root_path())
        print(json.dumps({"run_id": args.run_id, "reverted": n}, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

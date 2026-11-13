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

    return 1


if __name__ == "__main__":
    sys.exit(main())

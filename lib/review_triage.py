"""Review queue triage helper.

Cleans obsolete items, resolves consensus metadata discrepancies (majority rule >= 75%),
and removes items that have already been resolved on disk.
"""

from collections import Counter
import json
import logging
from pathlib import Path

import config
from lib.tags import read_tags, write_tags

log = logging.getLogger("review_triage")


def triage_review_queue(review_path: Path = Path("review.json"), *, dry_run: bool = True) -> dict:
    if not review_path.exists():
        return {"total": 0, "pruned": 0, "resolved": 0, "remaining": 0}

    try:
        items = json.loads(review_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Failed to load %s: %s", review_path, exc)
        return {"error": str(exc)}

    original_count = len(items)
    pruned_missing = 0
    resolved_consensus = 0
    resolved_other = 0
    remaining_items = []

    for item in items:
        raw_path = item.get("path")
        if not raw_path:
            continue
        p = Path(raw_path)

        # 1. Prune missing paths
        if not p.exists():
            pruned_missing += 1
            continue

        reason = item.get("reason", "")

        # 2. Check no_genre items: does the file now have a genre or parent genre?
        if reason == "no_genre" and p.is_file():
            tags = read_tags(p)
            if tags and tags.get("genre"):
                resolved_other += 1
                continue

        # 3. Check mixed_tags or inconsistent_tags on folders
        if reason in ("mixed_tags", "inconsistent_tags") and p.is_dir():
            audio_files = [
                f for f in p.rglob("*")
                if f.is_file() and f.suffix.lower() in config.AUDIO_EXTENSIONS
            ]
            if not audio_files:
                resolved_other += 1
                continue

            field = item.get("field") or (item.get("fields", [None])[0])
            if field in ("year", "album"):
                values = Counter()
                for af in audio_files:
                    tags = read_tags(af) or {}
                    val = (tags.get(field) or "").strip()
                    if field == "year" and val:
                        val = val[:4]
                    if val:
                        values[val] += 1

                if values:
                    top_val, count = values.most_common(1)[0]
                    # If >= 75% consensus and more than 1 distinct value existed
                    if count / len(audio_files) >= 0.75:
                        for af in audio_files:
                            tags = read_tags(af) or {}
                            curr_val = (tags.get(field) or "").strip()
                            if field == "year" and curr_val:
                                curr_val = curr_val[:4]
                            if curr_val != top_val:
                                if not dry_run:
                                    write_tags(af, {field: top_val}, dry_run=False)
                        resolved_consensus += 1
                        continue

        remaining_items.append(item)

    if not dry_run:
        review_path.write_text(
            json.dumps(remaining_items, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    summary = {
        "original_count": original_count,
        "pruned_missing": pruned_missing,
        "resolved_consensus": resolved_consensus,
        "resolved_other": resolved_other,
        "remaining": len(remaining_items),
        "dry_run": dry_run,
    }
    return summary


if __name__ == "__main__":
    import sys
    dry = "--live" not in sys.argv
    logging.basicConfig(level=logging.INFO)
    res = triage_review_queue(dry_run=dry)
    print("Triage Summary:", json.dumps(res, indent=2))

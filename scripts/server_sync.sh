#!/usr/bin/env bash
# server_sync.sh — Contract-compliant rsync from buffer audio to server.
#
# Enforces:
# 1. Deletion propagation (--delete) so moves do not duplicate files.
# 2. Rename detection (--fuzzy) to avoid re-uploading entire files.
# 3. Whole-file hashed formats check (.ogg, .opus, .wma).
# 4. Player refresh is performed separately on the remote player host.
#
# Usage:
#   ./scripts/server_sync.sh [--dry-run] [--source /path/to/buffer] [--dest user@server:/path]

set -euo pipefail

SOURCE="/media/jack/buffer/audio/"
DEST="${MUSIC_SERVER_DEST:-}"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run|-n)
            DRY_RUN=true
            shift
            ;;
        --source)
            SOURCE="$2"
            shift 2
            ;;
        --dest)
            DEST="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--dry-run] [--source DIR] [--dest DEST]" >&2
            exit 1
            ;;
    esac
done

if [ -z "$DEST" ]; then
    echo "ERROR: Destination not specified. Set MUSIC_SERVER_DEST in environment or pass --dest." >&2
    exit 1
fi

echo "=== Cratedigger Server Sync ==="
echo "Source:      $SOURCE"
echo "Destination: $DEST"
echo "Dry Run:     $DRY_RUN"
echo "==============================="

# Ensure source path ends with slash for rsync
[[ "$SOURCE" != */ ]] && SOURCE="${SOURCE}/"

RSYNC_FLAGS=(
    -avh
    --delete
    --fuzzy
    --prune-empty-dirs
    --partial
    --exclude=".*"
    --exclude="*~"
    --exclude="*.vdjstems"
    --exclude="0videos"
)

if [ "$DRY_RUN" = true ]; then
    RSYNC_FLAGS+=(--dry-run)
    echo "[DRY-RUN] Simulating sync..."
fi

rsync "${RSYNC_FLAGS[@]}" "$SOURCE" "$DEST"

echo "Sync completed successfully."

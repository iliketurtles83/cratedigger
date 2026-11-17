#!/usr/bin/env bash
# server_sync.sh — Contract-compliant rsync from buffer audio to server.
#
# Enforces:
# 1. Deletion propagation before transfer so stale case variants do not alias
#    canonical Windows-safe paths.
# 2. Perishable junk exclusions so hidden files cannot preserve stale folders.
# 3. The excluded 0videos tree is protected from deletion.
# 4. Player refresh is performed separately on the remote player host.
#
# The source must be a complete mirror. This script does not check the special
# retag/sync/refresh/move sequence required for Ogg, Opus, and WMA files.
#
# Usage:
#   ./scripts/server_sync.sh [--dry-run] [--source /path/to/buffer] [--dest user@server:/path]

set -euo pipefail

SOURCE="/media/jack/buffer/audio/"
DEST="${MUSIC_SERVER_DEST:-}"
DRY_RUN=false
ITEMIZE_PLAN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            echo "Usage: $0 [--dry-run] [--source DIR] [--dest DEST]"
            echo "The source must be a complete mirror; destination paths missing from it are deleted."
            echo "--itemize-plan emits a machine-readable plan and requires --dry-run."
            exit 0
            ;;
        --dry-run|-n)
            DRY_RUN=true
            shift
            ;;
        --itemize-plan)
            ITEMIZE_PLAN=true
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

if [ "$ITEMIZE_PLAN" = true ] && [ "$DRY_RUN" = false ]; then
    echo "ERROR: --itemize-plan requires --dry-run." >&2
    exit 1
fi

if [ -z "$DEST" ]; then
    echo "ERROR: Destination not specified. Set MUSIC_SERVER_DEST in environment or pass --dest." >&2
    exit 1
fi

echo "=== Cratedigger Server Sync ==="
echo "Source:      $SOURCE"
echo "Destination: $DEST"
echo "Dry Run:     $DRY_RUN"
echo "Delete mode: --delete-before (requires a complete source mirror)"
echo "==============================="

# Ensure source path ends with slash for rsync
[[ "$SOURCE" != */ ]] && SOURCE="${SOURCE}/"

RSYNC_FLAGS=(
    -avh
    --delete-before
    --prune-empty-dirs
    --partial
    --filter="-p .*"
    --filter="-p *~"
    --filter="-p *.vdjstems"
    --exclude="0videos"
)

if [ "$DRY_RUN" = true ]; then
    RSYNC_FLAGS+=(--dry-run)
    echo "[DRY-RUN] Simulating sync..."
fi
if [ "$ITEMIZE_PLAN" = true ]; then
    RSYNC_FLAGS+=(--itemize-changes "--out-format=@@sync-change %i|%l|%M|%n%L")
fi

rsync "${RSYNC_FLAGS[@]}" -- "$SOURCE" "$DEST"

echo "Sync completed successfully."
if [ "$DRY_RUN" = false ]; then
    echo "Refresh the player on the remote host after the sync; no local refresh was run."
fi

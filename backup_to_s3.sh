#!/bin/bash
# Incremental SQLite backup to S3
# Uses WAL checkpointing for safe copies, then aws s3 sync for incremental uploads.
# Only re-uploads files whose size has changed (--size-only), saving bandwidth.
#
# Note: A VPC Gateway Endpoint for S3 is configured (vpce-00bfa16b831a19c15)
# so all EC2-to-S3 traffic routes privately at zero data transfer cost.

set -e

BUCKET="${S3_BUCKET:-bhulekh-backup}"
DATA_DIR="${DATA_DIR:-bhulekh_data}"
QUEUE_DB="${QUEUE_DB:-work_queue.db}"
INSTANCE_TAG="${INSTANCE_TAG:-default}"
BACKUP_DIR="/tmp/bhulekh_backup_$$"
S3_PREFIX="s3://$BUCKET/fleet/$INSTANCE_TAG"

echo "=== Bhulekh S3 Incremental Backup ==="
echo "Bucket: $S3_PREFIX"
echo "Time: $(date)"

mkdir -p "$BACKUP_DIR/data"

# Backup work_queue.db with proper SQLite handling
if [ -f "$QUEUE_DB" ]; then
    echo "Backing up $QUEUE_DB..."
    sqlite3 "$QUEUE_DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
    sqlite3 "$QUEUE_DB" ".backup '$BACKUP_DIR/work_queue.db'"
    aws s3 cp "$BACKUP_DIR/work_queue.db" "$S3_PREFIX/work_queue.db" --only-show-errors
    echo "  Done."
fi

# Backup district databases incrementally
if [ -d "$DATA_DIR" ]; then
    echo "Backing up district databases (incremental)..."
    for db in "$DATA_DIR"/*.db; do
        [ -f "$db" ] || continue
        name=$(basename "$db")
        echo "  Checkpointing $name..."
        sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
        sqlite3 "$db" ".backup '$BACKUP_DIR/data/$name'"
    done

    # Sync only changed files (by size) to S3
    echo "  Syncing to S3 (size-only, skips unchanged)..."
    aws s3 sync "$BACKUP_DIR/data/" "$S3_PREFIX/bhulekh_data/" \
        --size-only --only-show-errors
    echo "  Done."
fi

# Cleanup temp files
rm -rf "$BACKUP_DIR"

echo "=== Backup complete ==="
echo "Time: $(date)"

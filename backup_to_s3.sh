#!/bin/bash
# Proper SQLite backup to S3
# SQLite WAL mode requires checkpointing before backup

set -e

BUCKET="${S3_BUCKET:-bhulekh-backup}"
DATA_DIR="${DATA_DIR:-bhulekh_data}"
QUEUE_DB="${QUEUE_DB:-work_queue.db}"
BACKUP_DIR="/tmp/bhulekh_backup_$$"

echo "=== Bhulekh S3 Backup ==="
echo "Bucket: s3://$BUCKET"
echo "Time: $(date)"

mkdir -p "$BACKUP_DIR"

# Backup work_queue.db with proper SQLite handling
if [ -f "$QUEUE_DB" ]; then
    echo "Backing up $QUEUE_DB..."
    # Checkpoint WAL to ensure all data is in main file
    sqlite3 "$QUEUE_DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
    # Use SQLite's backup command for safe copy
    sqlite3 "$QUEUE_DB" ".backup '$BACKUP_DIR/work_queue.db'"
    aws s3 cp "$BACKUP_DIR/work_queue.db" "s3://$BUCKET/work_queue.db"
    echo "  Uploaded work_queue.db"
fi

# Backup each district database
if [ -d "$DATA_DIR" ]; then
    echo "Backing up district databases..."
    for db in "$DATA_DIR"/*.db; do
        if [ -f "$db" ]; then
            name=$(basename "$db")
            echo "  Processing $name..."
            # Checkpoint and backup
            sqlite3 "$db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
            sqlite3 "$db" ".backup '$BACKUP_DIR/$name'"
            aws s3 cp "$BACKUP_DIR/$name" "s3://$BUCKET/bhulekh_data/$name"
        fi
    done
fi

# Cleanup
rm -rf "$BACKUP_DIR"

echo "=== Backup complete ==="
echo "Time: $(date)"

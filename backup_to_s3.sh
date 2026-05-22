#!/bin/bash
# Safe SQLite backup to S3
# This script properly checkpoints WAL before copying to avoid corruption

set -e

S3_BUCKET="${S3_BUCKET:-bhulekh-backup}"
BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
LOCAL_BACKUP="/tmp/$BACKUP_DIR"

echo "=== Bhulekh Safe Backup to S3 ==="
echo "Timestamp: $(date)"
echo "S3 Bucket: $S3_BUCKET"

mkdir -p "$LOCAL_BACKUP"

# Function to safely backup a SQLite database
backup_sqlite_db() {
    local db_path="$1"
    local db_name=$(basename "$db_path")
    
    if [ ! -f "$db_path" ]; then
        echo "  Skipping $db_name (not found)"
        return
    fi
    
    echo "  Backing up $db_name..."
    
    # Method 1: Use SQLite's built-in backup (safest)
    sqlite3 "$db_path" ".backup '$LOCAL_BACKUP/$db_name'"
    
    # Verify the backup
    if sqlite3 "$LOCAL_BACKUP/$db_name" "PRAGMA integrity_check;" | grep -q "ok"; then
        echo "    ✓ Backup verified: $db_name"
    else
        echo "    ✗ WARNING: Backup may be corrupted: $db_name"
    fi
}

# Backup work_queue.db
echo ""
echo "Backing up work_queue.db..."
backup_sqlite_db "work_queue.db"

# Backup all district databases
echo ""
echo "Backing up bhulekh_data/*.db..."
for db in bhulekh_data/district_*.db; do
    backup_sqlite_db "$db"
done

# Upload to S3
echo ""
echo "Uploading to S3..."
aws s3 sync "$LOCAL_BACKUP" "s3://$S3_BUCKET/$BACKUP_DIR/" --quiet
echo "  Uploaded to s3://$S3_BUCKET/$BACKUP_DIR/"

# Also keep a "latest" copy
aws s3 sync "$LOCAL_BACKUP" "s3://$S3_BUCKET/latest/" --quiet
echo "  Updated s3://$S3_BUCKET/latest/"

# Cleanup local backup
rm -rf "$LOCAL_BACKUP"

echo ""
echo "=== Backup Complete ==="
echo "To restore: aws s3 sync s3://$S3_BUCKET/latest/ ./"

# List recent backups
echo ""
echo "Recent backups in S3:"
aws s3 ls "s3://$S3_BUCKET/" | grep backup_ | tail -5

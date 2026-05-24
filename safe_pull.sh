#!/bin/bash
# Safe git pull that never fails
# Preserves local data files (work_queue.db, bhulekh_data/), updates code only

set -e

echo "=== Safe Git Pull ==="

# Get original owner of work_queue.db (for restoring permissions)
ORIG_OWNER=""
if [ -f work_queue.db ]; then
    ORIG_OWNER=$(stat -c '%U:%G' work_queue.db 2>/dev/null || stat -f '%Su:%Sg' work_queue.db 2>/dev/null || echo "")
    echo "Protecting work_queue.db (owner: $ORIG_OWNER)..."
    cp -p work_queue.db /tmp/work_queue_backup_$$.db
fi

# Stash any tracked file changes
if ! git diff --quiet 2>/dev/null; then
    echo "Stashing local changes..."
    git stash push -m "auto-stash before pull $(date +%Y%m%d-%H%M%S)"
fi

# Fetch latest
echo "Fetching from origin..."
git fetch origin

# Reset to origin/master (updates code only)
echo "Updating to latest code..."
git reset --hard origin/master

# Restore work_queue.db if it was backed up
if [ -f /tmp/work_queue_backup_$$.db ]; then
    echo "Restoring work_queue.db..."
    mv /tmp/work_queue_backup_$$.db work_queue.db
    
    # Restore ownership if we have it and running as root
    if [ -n "$ORIG_OWNER" ] && [ "$(id -u)" = "0" ]; then
        echo "Restoring ownership to $ORIG_OWNER..."
        chown "$ORIG_OWNER" work_queue.db
    fi
    
    # Ensure writable
    chmod 666 work_queue.db 2>/dev/null || true
fi

# Fix permissions on all .db files in bhulekh_data
if [ -d bhulekh_data ]; then
    echo "Fixing permissions on data files..."
    chmod 666 bhulekh_data/*.db 2>/dev/null || true
fi

echo "=== Pull complete ==="
git log --oneline -3

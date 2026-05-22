#!/bin/bash
# Safe git pull that never fails
# Preserves local data files (work_queue.db, bhulekh_data/), updates code only

set -e

echo "=== Safe Git Pull ==="

# Backup work_queue.db if it exists (critical protection)
if [ -f work_queue.db ]; then
    echo "Protecting work_queue.db..."
    cp work_queue.db /tmp/work_queue_backup_$$.db
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
fi

echo "=== Pull complete ==="
git log --oneline -3

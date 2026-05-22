#!/bin/bash
# Safe git pull that never fails
# Preserves local data files, updates code

set -e

echo "=== Safe Git Pull ==="

# Stash any tracked file changes (shouldn't happen if .gitignore is correct)
if ! git diff --quiet 2>/dev/null; then
    echo "Stashing local changes..."
    git stash push -m "auto-stash before pull $(date +%Y%m%d-%H%M%S)"
fi

# Fetch latest
echo "Fetching from origin..."
git fetch origin

# Reset to origin/master (safe because data files are untracked)
echo "Updating to latest code..."
git reset --hard origin/master

echo "=== Pull complete ==="
git log --oneline -3

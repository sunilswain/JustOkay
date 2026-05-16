#!/usr/bin/env bash
# =============================================================================
# boot.sh — runs on every EC2 boot (managed by bhulekh-boot.service)
#
# What it does:
#   1. git pull  — picks up any code / service file changes you pushed
#   2. uv sync   — installs any new packages from requirements.txt
#   3. copies bhulekh.service → systemd if it changed
#   4. reloads + starts the scraper
# =============================================================================

set -euo pipefail

PROJECT_DIR="/home/ubuntu/justokay"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

log() { echo "[boot] $*"; }

cd "$PROJECT_DIR"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
log "Pulling latest code..."
git pull --ff-only 2>/dev/null || log "git pull skipped (no network or already up to date)"

# ── 2. Sync dependencies ──────────────────────────────────────────────────────
log "Syncing Python dependencies..."
if [ -f ".venv/bin/uv" ]; then
    .venv/bin/uv pip install -q -r requirements.txt
elif command -v uv &>/dev/null; then
    uv pip install --python .venv/bin/python -q -r requirements.txt
else
    .venv/bin/pip install -q -r requirements.txt
fi

# ── 3. Update service file if changed ────────────────────────────────────────
if ! diff -q "$PROJECT_DIR/bhulekh.service" /etc/systemd/system/bhulekh.service &>/dev/null; then
    log "Service file changed — updating..."
    cp "$PROJECT_DIR/bhulekh.service" /etc/systemd/system/bhulekh.service
    systemctl daemon-reload
fi

log "Boot complete. Scraper will start via systemd."

#!/usr/bin/env bash
# =============================================================================
# boot.sh — runs on every EC2 boot via bhulekh-boot.service
#
#   1. git pull          — latest code + service file + pyproject.toml
#   2. uv sync           — install any new/changed packages
#   3. copy service file — if bhulekh.service changed, update systemd
# =============================================================================

set -euo pipefail

PROJECT_DIR="/home/ubuntu/justokay"
UV="/home/ubuntu/.local/bin/uv"

log() { echo "[boot] $*"; }

cd "$PROJECT_DIR"

# ── 1. Pull latest code ───────────────────────────────────────────────────────
log "git pull..."
git pull --ff-only 2>/dev/null || log "git pull skipped (offline or already up to date)"

# ── 2. uv sync — picks up any new packages added via uv add ──────────────────
log "uv sync..."
$UV sync --quiet

# ── 3. Update service file if changed ────────────────────────────────────────
if ! diff -q "$PROJECT_DIR/bhulekh.service" /etc/systemd/system/bhulekh.service &>/dev/null; then
    log "Service file changed — updating systemd..."
    cp "$PROJECT_DIR/bhulekh.service" /etc/systemd/system/bhulekh.service
    systemctl daemon-reload
fi

log "Done."

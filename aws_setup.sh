#!/usr/bin/env bash
# =============================================================================
# Bhulekh Scraper — AWS EC2 Setup Script
# OS      : Ubuntu 22.04 / 24.04 LTS
# Instance: m5.4xlarge (16 vCPU / 64 GB) → 40 workers
# Run     : git clone https://github.com/sunilswain/JustOkay.git justokay
#           cd justokay && sudo bash aws_setup.sh
# =============================================================================

set -euo pipefail

PROJECT_DIR="/home/ubuntu/justokay"
DATA_DIR="$PROJECT_DIR/bhulekh_data"

log() { echo -e "\n\033[1;32m>>> $*\033[0m"; }

# ── 1. System packages ────────────────────────────────────────────────────────
log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    curl git unzip \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libatspi2.0-0 libx11-6 libxext6 libxcb1 \
    fonts-noto fonts-noto-cjk

# libasound2 renamed in Ubuntu 24+
apt-get install -y -qq libasound2 2>/dev/null || \
apt-get install -y -qq libasound2t64 2>/dev/null || true

# ── 2. Install uv ─────────────────────────────────────────────────────────────
log "Installing uv..."
curl -Lsf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
uv --version

# ── 3. Python venv + dependencies via uv ─────────────────────────────────────
log "Setting up Python environment with uv..."
cd "$PROJECT_DIR"
uv venv .venv
uv pip install --quiet -r requirements.txt

# ── 4. Playwright Chromium ────────────────────────────────────────────────────
log "Installing Playwright Chromium..."
if .venv/bin/playwright install chromium; then
    log "Playwright Chromium installed"
else
    log "Falling back to system Chromium..."
    snap install chromium 2>/dev/null || apt-get install -y chromium-browser 2>/dev/null || true
    CHROMIUM_PATH=$(which chromium || which chromium-browser || echo "")
    if [ -n "$CHROMIUM_PATH" ]; then
        echo "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$CHROMIUM_PATH" >> /etc/environment
        echo "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"                 >> /etc/environment
        log "System Chromium set at: $CHROMIUM_PATH"
    else
        log "WARNING: No Chromium found — install manually: sudo snap install chromium"
    fi
fi

# ── 5. Data directory ─────────────────────────────────────────────────────────
log "Creating data directory..."
mkdir -p "$DATA_DIR"
chown -R ubuntu:ubuntu "$PROJECT_DIR"

# ── 6. Systemd service ────────────────────────────────────────────────────────
log "Installing systemd service..."
cp "$PROJECT_DIR/bhulekh.service" /etc/systemd/system/bhulekh.service
systemctl daemon-reload
systemctl enable bhulekh.service

# ── 7. Log rotation ───────────────────────────────────────────────────────────
cat > /etc/logrotate.d/bhulekh <<EOF
$PROJECT_DIR/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF

# ── Done ──────────────────────────────────────────────────────────────────────
log "Setup complete!"
echo ""
echo "  Start   : sudo systemctl start bhulekh"
echo "  Logs    : sudo journalctl -u bhulekh -f"
echo "  Status  : sudo systemctl status bhulekh"
echo "  Queue   : cd $PROJECT_DIR && .venv/bin/python work_queue.py stats"
echo "  Export  : cd $PROJECT_DIR && .venv/bin/python export_csv.py --data-dir $DATA_DIR --out /tmp/out.csv"
echo ""

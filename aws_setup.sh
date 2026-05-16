#!/usr/bin/env bash
# =============================================================================
# Bhulekh Scraper — One-time EC2 setup script
#
# Run ONCE after first clone:
#   git clone https://github.com/sunilswain/JustOkay.git justokay
#   cd justokay && sudo bash aws_setup.sh
#
# After this, every reboot auto-runs boot.sh (git pull + uv sync + start).
# To apply changes without reboot:
#   sudo systemctl restart bhulekh-boot && sudo systemctl restart bhulekh
# =============================================================================

set -euo pipefail

PROJECT_DIR="/home/ubuntu/justokay"

log() { echo -e "\n\033[1;32m>>> $*\033[0m"; }

# ── 1. System packages ────────────────────────────────────────────────────────
log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    curl git \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libatspi2.0-0 libx11-6 libxext6 libxcb1 \
    fonts-noto fonts-noto-cjk

apt-get install -y -qq libasound2 2>/dev/null || \
apt-get install -y -qq libasound2t64 2>/dev/null || true

# ── 2. uv ─────────────────────────────────────────────────────────────────────
log "Installing uv..."
sudo -u ubuntu bash -c 'curl -Lsf https://astral.sh/uv/install.sh | sh'
UV="/home/ubuntu/.local/bin/uv"

# ── 3. Python deps via uv sync ────────────────────────────────────────────────
log "Syncing dependencies (uv sync)..."
sudo -u ubuntu bash -c "cd $PROJECT_DIR && $UV sync"

# ── 4. Playwright Chromium ────────────────────────────────────────────────────
log "Installing Playwright Chromium..."
if sudo -u ubuntu bash -c "cd $PROJECT_DIR && $UV run playwright install chromium"; then
    log "Playwright Chromium installed"
else
    log "Falling back to system Chromium..."
    snap install chromium 2>/dev/null || apt-get install -y chromium-browser 2>/dev/null || true
    CHROMIUM_PATH=$(which chromium || which chromium-browser || echo "")
    if [ -n "$CHROMIUM_PATH" ]; then
        echo "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$CHROMIUM_PATH" >> /etc/environment
        echo "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"                 >> /etc/environment
        log "System Chromium: $CHROMIUM_PATH"
    else
        log "WARNING: No Chromium found — sudo snap install chromium"
    fi
fi

# ── 5. Data directory ─────────────────────────────────────────────────────────
log "Creating data directory..."
mkdir -p "$PROJECT_DIR/bhulekh_data"
chown -R ubuntu:ubuntu "$PROJECT_DIR"

# ── 6. Boot service (runs on every boot) ──────────────────────────────────────
log "Installing boot service..."
chmod +x "$PROJECT_DIR/boot.sh"

cat > /etc/systemd/system/bhulekh-boot.service <<EOF
[Unit]
Description=Bhulekh Boot (git pull + uv sync)
After=network-online.target
Wants=network-online.target
Before=bhulekh.service

[Service]
Type=oneshot
User=ubuntu
Environment=PATH=/home/ubuntu/.local/bin:/home/ubuntu/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/bin/bash $PROJECT_DIR/boot.sh
RemainAfterExit=yes
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bhulekh-boot

[Install]
WantedBy=multi-user.target
EOF

# ── 7. Scraper service ────────────────────────────────────────────────────────
log "Installing scraper service..."
cp "$PROJECT_DIR/bhulekh.service" /etc/systemd/system/bhulekh.service

systemctl daemon-reload
systemctl enable bhulekh-boot.service
systemctl enable bhulekh.service

# ── 8. Log rotation ───────────────────────────────────────────────────────────
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
echo "  Start    : sudo systemctl start bhulekh-boot bhulekh"
echo "  Logs     : sudo journalctl -u bhulekh -f"
echo "  Queue    : cd $PROJECT_DIR && uv run python work_queue.py stats"
echo "  Export   : cd $PROJECT_DIR && uv run python export_csv.py --data-dir bhulekh_data --out /tmp/out.csv"
echo ""

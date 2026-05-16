#!/usr/bin/env bash
# =============================================================================
# Bhulekh Scraper — One-time EC2 setup script
#
# Run ONCE after cloning the repo:
#   git clone https://github.com/sunilswain/JustOkay.git justokay
#   cd justokay && sudo bash aws_setup.sh
#
# After this, everything is automatic on every boot.
# To update: push changes to GitHub → reboot OR sudo systemctl restart bhulekh-boot
# =============================================================================

set -euo pipefail

PROJECT_DIR="/home/ubuntu/justokay"
DATA_DIR="$PROJECT_DIR/bhulekh_data"

log() { echo -e "\n\033[1;32m>>> $*\033[0m"; }

# ── 1. System packages (apt) ──────────────────────────────────────────────────
log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    curl git \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libatspi2.0-0 libx11-6 libxext6 libxcb1 \
    fonts-noto fonts-noto-cjk

# libasound2 renamed in Ubuntu 24+
apt-get install -y -qq libasound2 2>/dev/null || \
apt-get install -y -qq libasound2t64 2>/dev/null || true

# ── 2. uv ─────────────────────────────────────────────────────────────────────
log "Installing uv..."
sudo -u ubuntu bash -c 'curl -Lsf https://astral.sh/uv/install.sh | sh'
export PATH="/home/ubuntu/.local/bin:/home/ubuntu/.cargo/bin:$PATH"

# ── 3. Python venv + all dependencies ────────────────────────────────────────
log "Installing Python dependencies via uv..."
cd "$PROJECT_DIR"
sudo -u ubuntu bash -c "
    export PATH=/home/ubuntu/.local/bin:/home/ubuntu/.cargo/bin:\$PATH
    cd $PROJECT_DIR
    uv venv .venv
    uv pip install -q -r requirements.txt
"

# ── 4. Playwright Chromium ────────────────────────────────────────────────────
log "Installing Playwright Chromium..."
if sudo -u ubuntu "$PROJECT_DIR/.venv/bin/playwright" install chromium; then
    log "Playwright Chromium installed"
else
    log "Falling back to system Chromium..."
    snap install chromium 2>/dev/null || apt-get install -y chromium-browser 2>/dev/null || true
    CHROMIUM_PATH=$(which chromium || which chromium-browser || echo "")
    if [ -n "$CHROMIUM_PATH" ]; then
        echo "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$CHROMIUM_PATH" >> /etc/environment
        echo "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1"                 >> /etc/environment
        log "System Chromium set: $CHROMIUM_PATH"
    else
        log "WARNING: No Chromium found — install manually: sudo snap install chromium"
    fi
fi

# ── 5. Data directory ─────────────────────────────────────────────────────────
log "Creating data directory..."
mkdir -p "$DATA_DIR"
chown -R ubuntu:ubuntu "$PROJECT_DIR"

# ── 6. boot.sh service — runs on every boot ───────────────────────────────────
log "Installing boot service..."
chmod +x "$PROJECT_DIR/boot.sh"

cat > /etc/systemd/system/bhulekh-boot.service <<EOF
[Unit]
Description=Bhulekh Boot (git pull + dep sync)
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
log "Setup complete! Run:"
echo ""
echo "  sudo systemctl start bhulekh-boot bhulekh"
echo "  sudo journalctl -u bhulekh -f"
echo ""
echo "On every future reboot:"
echo "  1. bhulekh-boot runs → git pull + uv sync + service file update"
echo "  2. bhulekh starts    → scraper resumes from queue"
echo ""
echo "To apply changes without rebooting:"
echo "  sudo systemctl restart bhulekh-boot && sudo systemctl restart bhulekh"

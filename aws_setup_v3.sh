#!/bin/bash
# Quick setup for Bhulekh scraper v3 on an existing Ubuntu instance.
#
# Usage (SSH into instance, then):
#   curl -sSL https://raw.githubusercontent.com/.../aws_setup_v3.sh | bash
#   OR: copy this file and run: bash aws_setup_v3.sh
#
# After setup, start scraping:
#   cd ~/bhulekh
#   uv run python scraper_v3.py scrape --districts 3 8 2 15 --workers 20

set -euo pipefail

INSTANCE_NAME="${INSTANCE_NAME:-$(hostname)}"
S3_BUCKET="${S3_BUCKET:-bhulekh-backup}"

echo "=== Bhulekh Scraper v3 Setup ==="
echo "Instance: $INSTANCE_NAME"
echo ""

# System deps
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq git curl unzip

# Install uv if not present
if ! command -v uv &>/dev/null; then
    echo "[2/5] Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
else
    echo "[2/5] uv already installed"
fi
export PATH="$HOME/.local/bin:$PATH"

# Clone or update repo
echo "[3/5] Getting code..."
cd ~
if [ -d "bhulekh" ]; then
    cd bhulekh && git pull
else
    git clone https://github.com/sunilswain/JustOkay.git bhulekh
    cd bhulekh
fi

# Install Python deps + Playwright browser
echo "[4/5] Installing Python deps + Chromium..."
uv sync
uv run playwright install chromium
uv run playwright install-deps 2>/dev/null || true

# Restore progress from S3 (if any previous data exists)
echo "[5/5] Restoring progress from S3..."
aws s3 sync "s3://$S3_BUCKET/$INSTANCE_NAME/data/" bhulekh_data/ --quiet 2>/dev/null || true
aws s3 sync "s3://$S3_BUCKET/$INSTANCE_NAME/progress/" progress/ --quiet 2>/dev/null || true

# Setup S3 backup cron (every 5 min)
CRON_CMD="*/5 * * * * cd $HOME/bhulekh && aws s3 sync bhulekh_data/ s3://$S3_BUCKET/$INSTANCE_NAME/data/ --quiet 2>/dev/null && aws s3 sync progress/ s3://$S3_BUCKET/$INSTANCE_NAME/progress/ --quiet 2>/dev/null"
(crontab -l 2>/dev/null | grep -v "bhulekh" ; echo "$CRON_CMD") | crontab -

# Setup spot interruption monitor
sudo tee /usr/local/bin/spot-interrupt.sh > /dev/null << SCRIPT
#!/bin/bash
cd $HOME/bhulekh
aws s3 sync bhulekh_data/ s3://$S3_BUCKET/$INSTANCE_NAME/data/ --only-show-errors
aws s3 sync progress/ s3://$S3_BUCKET/$INSTANCE_NAME/progress/ --only-show-errors
echo "\$(date): Spot backup done" >> /var/log/spot-interrupt.log
SCRIPT
sudo chmod +x /usr/local/bin/spot-interrupt.sh

sudo tee /etc/systemd/system/spot-monitor.service > /dev/null << 'SERVICE'
[Unit]
Description=Spot Termination Monitor
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStart=/bin/bash -c 'while true; do if curl -sf http://169.254.169.254/latest/meta-data/spot/instance-action > /dev/null 2>&1; then /usr/local/bin/spot-interrupt.sh; sleep 120; fi; sleep 5; done'
User=ubuntu

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable spot-monitor.service
sudo systemctl start spot-monitor.service 2>/dev/null || true

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Start scraping:"
echo "  cd ~/bhulekh"
echo "  uv run python scraper_v3.py scrape --districts 3 8 2 15 --workers 20"
echo ""
echo "Check progress:"
echo "  uv run python scraper_v3.py status"
echo ""
echo "S3 backup: every 5 min to s3://$S3_BUCKET/$INSTANCE_NAME/"
echo "Spot interrupt handler: active"

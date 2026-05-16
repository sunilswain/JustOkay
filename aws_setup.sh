#!/usr/bin/env bash
# =============================================================================
# Bhulekh Scraper — AWS EC2 Setup Script
# Target OS : Ubuntu 22.04 LTS (ami-0c7217cdde317cfec in us-east-1, or pick
#             the latest Ubuntu 22.04 AMI for your region)
# Instance  : c5.2xlarge  (8 vCPU / 16 GB RAM)  →  20 workers
#             c5.4xlarge  (16 vCPU / 32 GB RAM)  →  40 workers  (recommended)
#
# Usage:
#   1. Launch EC2 (see bottom of this file for exact AWS CLI command)
#   2. scp this script + work_queue.db to the instance
#      scp -i key.pem aws_setup.sh work_queue.db ubuntu@<IP>:~
#   3. SSH in and run:
#      chmod +x aws_setup.sh && sudo bash aws_setup.sh
# =============================================================================

set -euo pipefail

# ── Config — edit before running ─────────────────────────────────────────────
WORKERS=40                        # workers per machine (see sizing guide below)
DISTRICTS="1 2 3 4 5 6 7 8 9 10" # which districts this machine handles
DATA_DIR="/home/ubuntu/JustOkay/bhulekh_data"
PROJECT_DIR="/home/ubuntu/JustOkay"
# ─────────────────────────────────────────────────────────────────────────────

log() { echo -e "\n\033[1;32m>>> $*\033[0m"; }

# Detect Python — Ubuntu 26.04 ships 3.13, 22.04 ships 3.10/3.11
detect_python() {
    for v in python3.13 python3.12 python3.11 python3; do
        if command -v "$v" &>/dev/null; then echo "$v"; return; fi
    done
    echo "python3"
}
PYTHON=$(detect_python)
log "Using Python: $PYTHON ($(${PYTHON} --version))"

# ── 1. System packages ────────────────────────────────────────────────────────
log "Installing system packages..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip git curl wget unzip \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \
    libatspi2.0-0 libx11-6 libxext6 libxcb1 \
    fonts-noto fonts-noto-cjk                # Odia Unicode font support

# libasound2 was renamed in Ubuntu 24+ — install whichever exists
apt-get install -y -qq libasound2 2>/dev/null || \
apt-get install -y -qq libasound2t64 2>/dev/null || true

# ── 2. Project files ──────────────────────────────────────────────────────────
log "Setting up project directory at $PROJECT_DIR..."
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# Files are uploaded by scp directly into PROJECT_DIR — nothing to copy
# (scp target should be ubuntu@IP:~/bhulekh/)
log "Project files in $PROJECT_DIR:"
ls -lh "$PROJECT_DIR/"

# ── 3. Python virtualenv ──────────────────────────────────────────────────────
log "Creating Python virtualenv..."
$PYTHON -m venv venv
source venv/bin/activate

log "Installing Python packages..."
pip install --quiet --upgrade pip
pip install --quiet \
    playwright \
    httpx \
    fastapi \
    uvicorn \
    "beautifulsoup4>=4.12" \
    lxml

log "Installing Playwright browsers (Chromium only)..."
# Try Playwright's bundled Chromium first (works on Ubuntu 22.04 / 24.04)
if playwright install chromium 2>/dev/null; then
    log "Playwright Chromium installed successfully"
else
    # Ubuntu 26.04+ not yet supported by Playwright — use system Chromium
    log "Playwright bundled Chromium not available for this OS, using system Chromium..."
    snap install chromium 2>/dev/null || apt-get install -y chromium-browser 2>/dev/null || true
    CHROMIUM_PATH=$(which chromium || which chromium-browser || echo "")
    if [ -n "$CHROMIUM_PATH" ]; then
        log "System Chromium found at: $CHROMIUM_PATH"
        echo "export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$CHROMIUM_PATH" >> /etc/environment
        echo "export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" >> /etc/environment
    else
        log "WARNING: No Chromium found. Install manually: sudo snap install chromium"
    fi
fi

# ── 4. Data directory ─────────────────────────────────────────────────────────
log "Creating data directory..."
mkdir -p "$DATA_DIR"
chown ubuntu:ubuntu "$DATA_DIR"

# ── 5. Systemd service — auto-restart on crash ────────────────────────────────
log "Creating systemd service..."

cat > /etc/systemd/system/bhulekh.service <<EOF
[Unit]
Description=Bhulekh Village Scraper
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python run_village_workers.py \\
    --workers $WORKERS \\
    --db $PROJECT_DIR/work_queue.db \\
    --data-dir $DATA_DIR \\
    --headless \\
    --fast \\
    --districts 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 24 25 26 27 28 29 30
Restart=on-failure
RestartSec=15
StandardOutput=journal
StandardError=journal
SyslogIdentifier=bhulekh

# Memory guard — restart if a worker leaks memory
MemoryMax=14G

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable bhulekh.service

# ── 6. Log rotation ───────────────────────────────────────────────────────────
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

# ── 7. S3 sync cron (optional — uncomment if you want live backup to S3) ──────
# Replace YOUR-BUCKET with your actual S3 bucket name
# crontab -l 2>/dev/null | { cat; echo "*/30 * * * * aws s3 sync $DATA_DIR s3://YOUR-BUCKET/bhulekh_data/ --quiet"; } | crontab -

# ── 8. Done ───────────────────────────────────────────────────────────────────
log "Setup complete!"
echo ""
echo "  Start now   : sudo systemctl start bhulekh"
echo "  Live logs   : sudo journalctl -u bhulekh -f"
echo "  Status      : sudo systemctl status bhulekh"
echo "  Check queue : cd $PROJECT_DIR && venv/bin/python work_queue.py stats"
echo "  Export CSV  : cd $PROJECT_DIR && venv/bin/python export_csv.py --data-dir $DATA_DIR --out /tmp/bhulekh_all.csv"
echo ""
echo "  To start manually (without systemd):"
echo "    cd $PROJECT_DIR && source venv/bin/activate"
echo "    python run_village_workers.py --workers $WORKERS --db work_queue.db --data-dir $DATA_DIR --headless --fast --districts $DISTRICTS"
echo ""

# =============================================================================
# AWS INSTANCE SIZING GUIDE
# =============================================================================
# Instance       vCPU  RAM    Workers  Cost/hr   Full run estimate
# ─────────────────────────────────────────────────────────────────────────────
# t3.large          2   8 GB      8    $0.083   ~12 days for 15 districts
# c5.xlarge         4   8 GB     12    $0.170    ~8 days for 15 districts
# c5.2xlarge        8  16 GB     20    $0.340    ~5 days for 15 districts  ← sweet spot
# c5.4xlarge       16  32 GB     40    $0.680    ~2.5 days for 15 districts
# c5.9xlarge       36  72 GB     80    $1.530    ~1.5 days for all 30 districts
# ─────────────────────────────────────────────────────────────────────────────
# Recommended setup for full 19M records in 7 days:
#   2 × c5.2xlarge  →  20 workers each, split 15 districts each → ~5 days
#   1 × c5.4xlarge  →  40 workers,      all 30 districts        → ~4 days
#
# With your local PCs ALSO running (e.g. 15+15 workers):
#   1 × c5.2xlarge  →  20 workers, districts 21-30
#   Local PC 1      →  15 workers, districts  1-10
#   Local PC 2      →  15 workers, districts 11-20
#   → Everything done in ~5 days, AWS cost ~$40 total

# =============================================================================
# LAUNCH COMMANDS (run from your LOCAL machine)
# =============================================================================
#
# 1. Create a key pair (once):
#    aws ec2 create-key-pair --key-name bhulekh-key --query 'KeyMaterial' \
#        --output text > bhulekh-key.pem
#    chmod 400 bhulekh-key.pem
#
# 2. Launch instance (c5.2xlarge, Ubuntu 22.04, 50 GB disk):
#    aws ec2 run-instances \
#        --image-id ami-0c7217cdde317cfec \
#        --instance-type c5.2xlarge \
#        --key-name bhulekh-key \
#        --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3"}}]' \
#        --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=bhulekh-worker-1}]' \
#        --count 1 \
#        --query 'Instances[0].PublicIpAddress' \
#        --output text
#
# 3. Upload project files to instance:
#    IP=<the IP from step 2>
#    scp -i bhulekh-key.pem \
#        bhulekh_scraper.py storage.py work_queue.py soap_enumerator.py \
#        run_village_workers.py export_csv.py verify_db.py queue_server.py \
#        requirements.txt aws_setup.sh work_queue.db \
#        ubuntu@$IP:~
#
# 4. SSH and run setup (takes ~5 minutes):
#    ssh -i bhulekh-key.pem ubuntu@$IP
#    sudo bash aws_setup.sh
#
# 5. Start scraping:
#    sudo systemctl start bhulekh
#    sudo journalctl -u bhulekh -f
#
# 6. When done, download data:
#    scp -i bhulekh-key.pem -r ubuntu@$IP:/home/ubuntu/bhulekh_data ./bhulekh_data_aws
#    python export_csv.py --data-dir bhulekh_data_aws --out aws_results.csv
#
# 7. Terminate instance when done (to stop billing):
#    aws ec2 terminate-instances --instance-ids <instance-id>

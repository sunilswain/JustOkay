#!/bin/bash
# =============================================================================
# Launch 2x r5.2xlarge spot instances from Windows PowerShell:
#
#   $Region     = "ap-south-1"
#   $Ami        = "ami-0f58b397bc5c1f2e8"
#   $Key        = "eziterms-admin"
#   $Sg         = "sg-037df14673f6a20a0"
#   $Subnet     = "subnet-0cfc6616475bedb42"
#   $SpotOpts   = '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"persistent","InstanceInterruptionBehavior":"stop"}}'
#   $EbsMapping = '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":80,"VolumeType":"gp3","DeleteOnTermination":false}}]'
#
#   foreach ($Name in @("scraper-r5-1", "scraper-r5-2")) {
#     aws ec2 run-instances `
#       --region $Region `
#       --image-id $Ami `
#       --instance-type r5.2xlarge `
#       --key-name $Key `
#       --security-group-ids $Sg `
#       --subnet-id $Subnet `
#       --associate-public-ip-address `
#       --instance-market-options $SpotOpts `
#       --block-device-mappings $EbsMapping `
#       --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$Name},{Key=Project,Value=bhulekh}]" `
#       --query 'Instances[0].InstanceId' `
#       --output text
#   }
#
# After instances are running, SSH in and run:
#   bash launch_r5_fleet.sh scraper-r5-1
#   bash launch_r5_fleet.sh scraper-r5-2
# =============================================================================
#
# Setup script for r5.2xlarge Bhulekh scraper fleet (64GB RAM, 40 workers).
# Usage: bash launch_r5_fleet.sh <instance-name>
#   scraper-r5-1 -> districts 10 23 3 9 21 16 26 20 27 30 24 19 29 25
#   scraper-r5-2 -> districts 15 28 4 17 22 11 13 2 18 14 8 7 6 1 5

set -euo pipefail

INSTANCE_NAME="${1:-}"
S3_BUCKET="${S3_BUCKET:-bhulekh-backup}"
REPO_URL="https://github.com/sunilswain/JustOkay.git"
APP_DIR="/home/ubuntu/bhulekh"
WORKERS=40

if [ -z "$INSTANCE_NAME" ]; then
    echo "Usage: bash launch_r5_fleet.sh <instance-name>"
    echo "  scraper-r5-1  (near-complete + first half of remaining)"
    echo "  scraper-r5-2  (second half of remaining districts)"
    exit 1
fi

case "$INSTANCE_NAME" in
    scraper-r5-1)
        DISTRICTS="10 23 3 9 21 16 26 20 27 30 24 19 29 25"
        ;;
    scraper-r5-2)
        DISTRICTS="15 28 4 17 22 11 13 2 18 14 8 7 6 1 5"
        ;;
    *)
        echo "ERROR: Unknown instance name '$INSTANCE_NAME'"
        echo "Expected: scraper-r5-1 or scraper-r5-2"
        exit 1
        ;;
esac

echo "=== Bhulekh r5 Fleet Setup: $INSTANCE_NAME ==="
echo "Districts: $DISTRICTS"
echo "Workers:   $WORKERS"
echo ""

# System packages
echo "[1/9] Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq git curl unzip sqlite3

# Hostname
sudo hostnamectl set-hostname "$INSTANCE_NAME"

# uv
echo "[2/9] Installing uv..."
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="/home/ubuntu/.local/bin:$PATH"
grep -q '.local/bin' ~/.bashrc 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

# Clone repo
echo "[3/9] Cloning repository..."
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR" && git pull
else
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

# Python deps + Playwright
echo "[4/9] Installing Python deps and Playwright Chromium..."
uv sync
uv run playwright install chromium
uv run playwright install-deps 2>/dev/null || true

# Download work_queue.db (one-time seed source, not used at runtime)
echo "[5/9] Downloading work_queue.db from S3..."
aws s3 cp "s3://$S3_BUCKET/work_queue.db" "$APP_DIR/work_queue.db" --only-show-errors

# Generate villages.json
echo "[6/9] Generating villages.json..."
uv run python scraper_v3.py enumerate --db work_queue.db

# Seed .done files from work_queue.db
echo "[7/9] Seeding progress markers from work_queue.db..."
uv run python seed_progress.py --db work_queue.db --progress-dir progress

# Sync existing scraped data from fleet backups
echo "[8/9] Syncing fleet backup data from S3..."
mkdir -p bhulekh_data progress
aws s3 sync "s3://$S3_BUCKET/fleet-backup/" /tmp/fleet-backup/ --only-show-errors 2>/dev/null || true
if [ -d /tmp/fleet-backup ]; then
    for inst_dir in /tmp/fleet-backup/*/; do
        [ -d "$inst_dir" ] || continue
        echo "  Merging $(basename "$inst_dir")..."
        cp -n "$inst_dir"/*.db bhulekh_data/ 2>/dev/null || true
    done
    rm -rf /tmp/fleet-backup
fi

# S3 backup cron (every 5 min)
echo "[9/9] Installing systemd service and backup cron..."
CRON_CMD="*/5 * * * * cd $APP_DIR && aws s3 sync bhulekh_data/ s3://$S3_BUCKET/$INSTANCE_NAME/data/ --quiet 2>/dev/null; aws s3 sync progress/ s3://$S3_BUCKET/$INSTANCE_NAME/progress/ --quiet 2>/dev/null"
(crontab -l 2>/dev/null | grep -v "bhulekh-backup/$INSTANCE_NAME" || true; echo "$CRON_CMD") | crontab -

# Spot interruption backup
sudo tee /usr/local/bin/spot-interrupt.sh > /dev/null << SPOTSCRIPT
#!/bin/bash
cd $APP_DIR
aws s3 sync bhulekh_data/ s3://$S3_BUCKET/$INSTANCE_NAME/data/ --only-show-errors
aws s3 sync progress/ s3://$S3_BUCKET/$INSTANCE_NAME/progress/ --only-show-errors
echo "\$(date): Spot backup done" >> /var/log/spot-interrupt.log
SPOTSCRIPT
sudo chmod +x /usr/local/bin/spot-interrupt.sh

sudo tee /etc/systemd/system/spot-monitor.service > /dev/null << 'SPOTSVC'
[Unit]
Description=Spot Termination Monitor
After=network-online.target

[Service]
Type=simple
User=ubuntu
Restart=always
RestartSec=5
ExecStart=/bin/bash -c 'while true; do if curl -sf http://169.254.169.254/latest/meta-data/spot/instance-action > /dev/null 2>&1; then /usr/local/bin/spot-interrupt.sh; sleep 120; fi; sleep 5; done'

[Install]
WantedBy=multi-user.target
SPOTSVC

# Scraper service (districts passed as parameter)
sudo tee /etc/systemd/system/bhulekh-scraper.service > /dev/null << SVCEOF
[Unit]
Description=Bhulekh Scraper v3 ($INSTANCE_NAME)
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=ubuntu
WorkingDirectory=$APP_DIR
Environment="PATH=/home/ubuntu/.local/bin:/usr/bin:/bin:/usr/local/bin"
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/ubuntu/.local/bin/uv run python scraper_v3.py scrape --districts $DISTRICTS --workers $WORKERS --fast
Restart=always
RestartSec=30
StandardOutput=append:$APP_DIR/scraper.log
StandardError=append:$APP_DIR/scraper.log
SyslogIdentifier=bhulekh-scraper

# Prefer killing child Chromium workers over the supervisor during memory pressure
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
SVCEOF

sudo systemctl daemon-reload
sudo systemctl enable spot-monitor.service bhulekh-scraper.service
sudo systemctl start spot-monitor.service
sudo systemctl start bhulekh-scraper.service

echo ""
echo "=== Setup complete: $INSTANCE_NAME ==="
echo "  Districts: $DISTRICTS"
echo "  Workers:   $WORKERS"
echo "  S3 backup: every 5 min -> s3://$S3_BUCKET/$INSTANCE_NAME/"
echo ""
echo "  sudo systemctl status bhulekh-scraper"
echo "  tail -f $APP_DIR/scraper.log"
echo "  uv run python scraper_v3.py status --districts $DISTRICTS"

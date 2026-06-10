#!/bin/bash
# Deploy Bhulekh scraper v3 to a t3.xlarge spot instance.
#
# Usage:
#   ./deploy_spot.sh <instance-name> <district-codes>
#   ./deploy_spot.sh spot-1 "3 8 2 15"
#   ./deploy_spot.sh spot-2 "10 24 20 14"
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - eziterms-admin.pem key pair in ~/.ssh/ or current directory
#   - S3 bucket exists: s3://bhulekh-scraper-backup

set -euo pipefail

INSTANCE_NAME="${1:-spot-1}"
DISTRICTS="${2:-3 8 2 15}"
WORKERS="${3:-20}"

AMI="ami-0f58b397bc5c1f2e8"  # Ubuntu 22.04 LTS ap-south-1
INSTANCE_TYPE="t3.xlarge"
KEY_NAME="eziterms-admin"
SECURITY_GROUP="sg-04f32850d3e792dbe"
SUBNET="subnet-0a1b2c3d"  # Update with your subnet
S3_BUCKET="bhulekh-backup"
REPO_URL="https://github.com/sunilswain/JustOkay.git"

echo "=== Deploying Bhulekh Scraper v3 ==="
echo "Instance: $INSTANCE_NAME"
echo "Type: $INSTANCE_TYPE (spot)"
echo "Districts: $DISTRICTS"
echo "Workers: $WORKERS"
echo ""

# Launch spot instance with persistent EBS
INSTANCE_ID=$(aws ec2 run-instances \
    --image-id "$AMI" \
    --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" \
    --security-group-ids "$SECURITY_GROUP" \
    --instance-market-options '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"persistent","InstanceInterruptionBehavior":"stop"}}' \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":50,"VolumeType":"gp3","DeleteOnTermination":false}}]' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]" \
    --query 'Instances[0].InstanceId' \
    --output text)

echo "Launched instance: $INSTANCE_ID"
echo "Waiting for instance to be running..."
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"

# Get public IP
PUBLIC_IP=$(aws ec2 describe-instances \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' \
    --output text)

echo "Instance IP: $PUBLIC_IP"
echo "Waiting for SSH..."
sleep 30

# Setup script to run on the instance
SSH_CMD="ssh -i eziterms-admin.pem -o StrictHostKeyChecking=no ubuntu@$PUBLIC_IP"

$SSH_CMD << 'SETUP_EOF'
set -euo pipefail

echo "=== Installing dependencies ==="
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip git awscli

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

# Clone repo
cd ~
git clone $REPO_URL bhulekh 2>/dev/null || (cd bhulekh && git pull)
cd bhulekh

# Install Python deps + Playwright
uv sync
uv run playwright install chromium
uv run playwright install-deps

echo "=== Setting up S3 backup cron ==="
# Backup every 5 minutes
(crontab -l 2>/dev/null; echo "*/5 * * * * cd ~/bhulekh && aws s3 sync bhulekh_data/ s3://$S3_BUCKET/$INSTANCE_NAME/data/ --quiet && aws s3 sync progress/ s3://$S3_BUCKET/$INSTANCE_NAME/progress/ --quiet") | crontab -

echo "=== Setting up spot interruption handler ==="
sudo tee /usr/local/bin/spot-interrupt.sh > /dev/null << 'SCRIPT'
#!/bin/bash
# Triggered on spot termination 2-minute warning
cd /home/ubuntu/bhulekh
aws s3 sync bhulekh_data/ s3://$S3_BUCKET/$INSTANCE_NAME/data/ --only-show-errors
aws s3 sync progress/ s3://$S3_BUCKET/$INSTANCE_NAME/progress/ --only-show-errors
echo "$(date): Spot interruption backup complete" >> /var/log/spot-interrupt.log
SCRIPT
sudo chmod +x /usr/local/bin/spot-interrupt.sh

# Systemd service for spot interruption detection
sudo tee /etc/systemd/system/spot-interrupt.service > /dev/null << 'SERVICE'
[Unit]
Description=Spot Instance Interruption Handler
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/spot-interrupt.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SERVICE

# Metadata polling service (checks every 5s for termination notice)
sudo tee /etc/systemd/system/spot-monitor.service > /dev/null << 'MONITOR'
[Unit]
Description=Spot Termination Monitor
After=network.target

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStart=/bin/bash -c 'while true; do if curl -sf http://169.254.169.254/latest/meta-data/spot/instance-action > /dev/null 2>&1; then systemctl start spot-interrupt.service; sleep 120; fi; sleep 5; done'

[Install]
WantedBy=multi-user.target
MONITOR

sudo systemctl daemon-reload
sudo systemctl enable spot-monitor.service
sudo systemctl start spot-monitor.service

echo "=== Setup complete ==="
SETUP_EOF

echo ""
echo "=== Starting scraper ==="
$SSH_CMD "cd ~/bhulekh && nohup uv run python scraper_v3.py scrape --districts $DISTRICTS --workers $WORKERS > scraper.log 2>&1 &"

echo ""
echo "=========================================="
echo "  Deployment complete!"
echo "  Instance: $INSTANCE_ID"
echo "  IP: $PUBLIC_IP"
echo "  Districts: $DISTRICTS"
echo "  Workers: $WORKERS"
echo ""
echo "  Monitor: $SSH_CMD 'tail -f ~/bhulekh/scraper_v3.log'"
echo "  Status:  $SSH_CMD 'cd ~/bhulekh && uv run python scraper_v3.py status'"
echo "=========================================="

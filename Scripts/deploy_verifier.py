"""Deploy per-instance auto-verifier to all fleet instances (parallel)."""
import subprocess
import sys

KEY = r"C:\Users\sushi\Downloads\eziterms-admin.pem"
FLEET = {
    "fleet-01": "13.232.243.239",
    "fleet-02": "3.110.104.124",
    "fleet-03": "3.7.70.196",
    "fleet-04": "13.126.129.216",
    "fleet-05": "13.233.38.169",
    "fleet-06": "3.111.55.72",
    "fleet-07": "13.126.196.140",
    "fleet-08": "13.203.157.75",
    "fleet-09": "3.109.1.126",
    "fleet-10": "3.108.237.160",
    "fleet-11": "13.127.183.188",
    "fleet-12": "52.66.249.212",
    "fleet-13": "13.201.137.36",
}

REMOTE = r"""
set -e
cd /home/ubuntu/justokay
aws s3 cp s3://bhulekh-backup/code/verify_district.py verify_district.py
aws s3 cp s3://bhulekh-backup/code/ror_parser.py ror_parser.py
aws s3 cp s3://bhulekh-backup/code/auto_verify.py auto_verify.py
aws s3 cp s3://bhulekh-backup/code/work_queue.py work_queue.py
aws s3 cp s3://bhulekh-backup/code/http_scraper.py http_scraper.py
aws s3 cp s3://bhulekh-backup/code/playwright_district_scraper.py playwright_district_scraper.py
aws s3 cp s3://bhulekh-backup/code/export_csv.py export_csv.py

# Install playwright + system deps
if ! .venv/bin/python -c "import playwright" 2>/dev/null; then
  echo "Installing playwright..."
  .venv/bin/pip install -q playwright
fi
.venv/bin/playwright install chromium 2>/dev/null || true
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2t64 \
  libpango-1.0-0 libcairo2 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libdrm2 libxkbcommon0 libxfixes3 2>/dev/null || true
echo "Playwright ready"

sudo tee /etc/systemd/system/bhulekh-verify.service > /dev/null << 'UNIT'
[Unit]
Description=Bhulekh Per-Instance Auto-Verifier
After=network.target bhulekh-http.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/justokay
ExecStart=/home/ubuntu/justokay/.venv/bin/python -u auto_verify.py --db /home/ubuntu/justokay/work_queue.db --data-dir /home/ubuntu/justokay/bhulekh_data --interval 300 --fetch-missing
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
UNIT

rm -f verifier_active_district.json
rm -f verify_attempts.json

sudo systemctl daemon-reload
sudo systemctl enable bhulekh-verify
sudo systemctl restart bhulekh-verify
echo DEPLOY_OK
"""


def deploy_one(name, ip):
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        "-i", KEY, f"ubuntu@{ip}", REMOTE,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = "DEPLOY_OK" in r.stdout
        return name, ok, r.stdout.strip().splitlines()[-3:] if r.stdout else [], r.stderr[-200:] if r.stderr else ""
    except subprocess.TimeoutExpired:
        return name, False, [], "TIMEOUT"
    except Exception as e:
        return name, False, [], str(e)


def main():
    # Upload latest to S3 first
    for f in ["auto_verify.py", "verify_district.py", "ror_parser.py", "work_queue.py", "http_scraper.py", "playwright_district_scraper.py", "export_csv.py"]:
        subprocess.run(
            ["aws", "s3", "cp",
             rf"C:\Users\sushi\Programming\Sunil\BhulekhAutomation\{f}",
             f"s3://bhulekh-backup/code/{f}"],
            check=True,
        )
    print("Uploaded to S3\n")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=13) as pool:
        futures = {pool.submit(deploy_one, n, ip): n for n, ip in FLEET.items()}
        for fut in as_completed(futures):
            results.append(fut.result())

    print("=" * 60)
    for name, ok, lines, err in sorted(results):
        status = "OK" if ok else "FAILED"
        print(f"  {name}: {status}")
        for line in lines:
            print(f"    {line}")
        if err and not ok:
            print(f"    ERR: {err}")
    print("=" * 60)


if __name__ == "__main__":
    main()

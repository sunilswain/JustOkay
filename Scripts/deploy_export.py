"""Deploy export_csv.py to all fleet instances (parallel)."""
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

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

CMD = "cd /home/ubuntu/justokay && aws s3 cp s3://bhulekh-backup/code/export_csv.py export_csv.py && echo DEPLOY_OK"


def deploy_one(name, ip):
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15",
        "-i", KEY, f"ubuntu@{ip}", CMD,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        ok = "DEPLOY_OK" in r.stdout
        return name, ok, r.stderr[-100:] if r.stderr and not ok else ""
    except subprocess.TimeoutExpired:
        return name, False, "TIMEOUT"
    except Exception as e:
        return name, False, str(e)


def main():
    results = []
    with ThreadPoolExecutor(max_workers=13) as pool:
        futures = {pool.submit(deploy_one, n, ip): n for n, ip in FLEET.items()}
        for fut in as_completed(futures):
            results.append(fut.result())

    print("=" * 40)
    for name, ok, err in sorted(results):
        status = "OK" if ok else "FAILED"
        print(f"  {name}: {status}" + (f"  ({err})" if err else ""))
    print("=" * 40)


if __name__ == "__main__":
    main()

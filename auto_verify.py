#!/usr/bin/env python3
"""
Auto-Verification Daemon (per-instance).

When the HTTP scraper finishes or gets stuck on a district, this daemon:
  1. Stops the HTTP scraper (bhulekh-http)
  2. Requeues all villages in that ONE district
  3. Runs the multi-worker Playwright scraper (type12-fast-scraper flow) with
     ALL instance workers until every village is 100% complete
  4. Confirms via SOAP comparison (verify_district.py)
  5. Resumes HTTP scraper for remaining assigned districts

Only ONE district is processed at a time per instance.

Usage:
    python auto_verify.py --fetch-missing
    python auto_verify.py --once --fetch-missing
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from work_queue import requeue_district_for_verifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("auto_verify.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

VERIFIED_FILE = "verified_districts.json"
ATTEMPTS_FILE = "verify_attempts.json"
ACTIVE_DISTRICT_FILE = "verifier_active_district.json"
SERVICE_FILE = "/etc/systemd/system/bhulekh-http.service"
COMPLETED_DISTRICTS = {4, 17, 23, 25, 28, 29, 30}
MIN_PCT_FOR_HANDOFF = 95.0
RETRY_HOURS = 6
DEFAULT_WORKERS = 15


def load_json_set(path: str) -> Set[int]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_json_set(path: str, values: Set[int]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(values), f)


def load_attempts() -> Dict[str, float]:
    if os.path.exists(ATTEMPTS_FILE):
        try:
            with open(ATTEMPTS_FILE, "r", encoding="utf-8") as f:
                return {str(k): float(v) for k, v in json.load(f).items()}
        except Exception:
            pass
    return {}


def save_attempt(district_code: int) -> None:
    attempts = load_attempts()
    attempts[str(district_code)] = time.time()
    with open(ATTEMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(attempts, f, indent=0)


def recently_attempted(district_code: int) -> bool:
    attempts = load_attempts()
    ts = attempts.get(str(district_code))
    if not ts:
        return False
    return (time.time() - ts) < RETRY_HOURS * 3600


def save_active_district(district_code: int) -> None:
    with open(ACTIVE_DISTRICT_FILE, "w", encoding="utf-8") as f:
        json.dump({"district_code": district_code, "started_at": time.time()}, f)


def clear_active_district() -> None:
    if os.path.exists(ACTIVE_DISTRICT_FILE):
        os.remove(ACTIVE_DISTRICT_FILE)


def get_active_district() -> Optional[int]:
    if not os.path.exists(ACTIVE_DISTRICT_FILE):
        return None
    try:
        with open(ACTIVE_DISTRICT_FILE, encoding="utf-8") as f:
            return int(json.load(f).get("district_code", 0)) or None
    except Exception:
        return None


def get_assigned_districts(service_file: str = SERVICE_FILE) -> List[int]:
    try:
        text = Path(service_file).read_text(encoding="utf-8")
    except Exception:
        return []
    m = re.search(r"--districts\s+([\d\s]+)", text)
    if not m:
        return []
    return [int(x) for x in m.group(1).split() if x.strip().isdigit()]


def get_worker_count(service_file: str = SERVICE_FILE) -> int:
    try:
        text = Path(service_file).read_text(encoding="utf-8")
        m = re.search(r"--workers\s+(\d+)", text)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return DEFAULT_WORKERS


def is_http_takeover_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "http_scraper.py.*--completion-min-fraction 1"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def kill_playwright_scraper() -> None:
    subprocess.run(["pkill", "-f", "playwright_district_scraper.py"], check=False)


def stop_http_scraper() -> None:
    log.info("Stopping HTTP scraper (bhulekh-http) — Playwright verifier taking over")
    subprocess.run(["sudo", "systemctl", "stop", "bhulekh-http"], check=False)


def is_http_service_active() -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "bhulekh-http"],
            capture_output=True, text=True,
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def is_http_scraper_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "http_scraper.py"],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def start_http_scraper() -> None:
    if get_active_district() is not None:
        log.warning("Not starting HTTP scraper — verifier still active")
        return
    log.info("Resuming HTTP scraper (bhulekh-http)")
    subprocess.run(["sudo", "systemctl", "start", "bhulekh-http"], check=False)


def ensure_http_scraper_running(db_path: str, assigned: List[int]) -> None:
    """
    Watchdog: restart bhulekh-http if it died but assigned districts still have work.
    """
    if not assigned:
        return
    if get_active_district() or is_http_takeover_running() or is_http_scraper_running():
        return
    if is_http_service_active():
        return

    completion = get_district_completion(db_path, assigned)
    needs_work = any(
        info["pending"] > 0 or info["in_progress"] > 0
        for info in completion.values()
    )
    if not needs_work:
        return

    pending_total = sum(info["pending"] for info in completion.values())
    in_prog = sum(info["in_progress"] for info in completion.values())
    log.warning(
        "Watchdog: HTTP scraper down but %d pending + %d in_progress on assigned "
        "districts — restarting bhulekh-http",
        pending_total, in_prog,
    )
    start_http_scraper()


def kill_stray_verifiers() -> None:
    subprocess.run(["pkill", "-f", "verify_district.py"], check=False)


def get_district_completion(db_path: str, district_codes: Optional[List[int]] = None) -> Dict[int, Dict]:
    conn = sqlite3.connect(db_path)
    if district_codes:
        placeholders = ",".join("?" * len(district_codes))
        rows = conn.execute(f"""
            SELECT district_code, district_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                   SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errors
            FROM villages
            WHERE district_code IN ({placeholders})
            GROUP BY district_code
        """, district_codes).fetchall()
    else:
        rows = conn.execute("""
            SELECT district_code, district_name,
                   COUNT(*) as total,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                   SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errors
            FROM villages
            GROUP BY district_code
        """).fetchall()
    conn.close()

    result = {}
    for d_code, d_name, total, done, pending, in_progress, errors in rows:
        pct = (done / total * 100) if total else 0
        scraper_idle = pending == 0 and in_progress == 0
        result[d_code] = {
            "district_code": d_code,
            "district_name": d_name,
            "total": total,
            "done": done,
            "pending": pending,
            "in_progress": in_progress,
            "errors": errors,
            "pct": pct,
            "is_complete": done == total and total > 0,
            "scraper_idle": scraper_idle,
        }
    return result


def ready_for_verification(info: Dict) -> Tuple[bool, str]:
    if info["is_complete"]:
        return True, "complete"

    if not info["scraper_idle"]:
        return False, ""

    if info["errors"] > 0:
        return True, f"stuck ({info['errors']} errors, 0 pending)"

    if info["pct"] >= MIN_PCT_FOR_HANDOFF:
        return True, f"exhausted ({info['pct']:.1f}% done, 0 pending)"

    return False, ""


def run_http_district_completion(
    district_code: int,
    data_dir: str,
    db_path: str,
    workers: int,
) -> bool:
    """Run HTTP scraper at 100%% completion until district queue is drained."""
    script = Path(__file__).parent / "http_scraper.py"
    cmd = [
        sys.executable, "-u", str(script),
        "--workers", str(workers),
        "--db", db_path,
        "--data-dir", data_dir,
        "--districts", str(district_code),
        "--completion-min-fraction", "1.0",
        "--request-delay", "0.2",
        "--max-inflight", "30",
        "--reset-errors",
    ]
    log.info("HTTP takeover D%d with %d workers (100%% completion)", district_code, workers)
    try:
        result = subprocess.run(cmd, timeout=None)
        return result.returncode == 0
    except Exception as e:
        log.error("HTTP scraper error for D%d: %s", district_code, e)
        return False


def run_verification_check(district_code: int, data_dir: str) -> Tuple[bool, int]:
    """SOAP comparison only — no single-threaded Playwright fetch."""
    script = Path(__file__).parent / "verify_district.py"
    report_file = f"verify_report_d{district_code}.json"
    cmd = [
        sys.executable, "-u", str(script),
        "--district", str(district_code),
        "--data-dir", data_dir,
        "--report-file", report_file,
        "--headless",
    ]
    log.info("Verification check for D%d", district_code)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if result.stdout:
            for line in result.stdout.strip().splitlines()[-10:]:
                log.info("  %s", line)
        if result.returncode != 0:
            log.error("Verification check failed D%d: %s", district_code, (result.stderr or "")[-500:])
            return False, -1

        if os.path.exists(report_file):
            with open(report_file, encoding="utf-8") as f:
                report = json.load(f)
            missing = report.get("total_missing_khatiyans", 0)
            if missing == 0:
                log.info("D%d FULLY VERIFIED — 0 missing khatiyans", district_code)
                return True, 0
            log.warning("D%d still has %d missing khatiyans", district_code, missing)
            return False, missing
        return False, -1
    except subprocess.TimeoutExpired:
        log.error("Verification check timed out D%d", district_code)
        return False, -1
    except Exception as e:
        log.error("Verification check error D%d: %s", district_code, e)
        return False, -1


def takeover_district(
    district_code: int,
    data_dir: str,
    db_path: str,
    reason: str,
) -> bool:
    """
    Full district handoff: stop HTTP scraper, requeue villages, run Playwright
    with all workers, verify 100%, resume HTTP scraper.
    """
    workers = get_worker_count()
    log.info("=" * 60)
    log.info("DISTRICT TAKEOVER: D%d (%s) — %d HTTP workers", district_code, reason, workers)
    log.info("=" * 60)

    kill_stray_verifiers()
    kill_playwright_scraper()
    stop_http_scraper()
    time.sleep(3)

    requeued = requeue_district_for_verifier(db_path, district_code)
    log.info("Requeued %d villages in D%d for 100%% HTTP completion", requeued, district_code)

    save_active_district(district_code)

    http_ok = run_http_district_completion(district_code, data_dir, db_path, workers)
    if not http_ok:
        log.error("HTTP scraper exited with error for D%d", district_code)

    # If villages remain queued, keep draining before verification check
    completion = get_district_completion(db_path, [district_code])
    info = completion.get(district_code, {})
    if info.get("pending", 0) > 0 or info.get("in_progress", 0) > 0:
        log.warning(
            "D%d still has pend=%d wip=%d after HTTP pass — running another pass",
            district_code, info.get("pending", 0), info.get("in_progress", 0),
        )
        run_http_district_completion(district_code, data_dir, db_path, workers)

    verified, missing = run_verification_check(district_code, data_dir)

    clear_active_district()
    start_http_scraper()

    if verified:
        return True

    if missing > 0:
        log.warning(
            "D%d takeover incomplete — %d khatiyans still missing after Playwright pass",
            district_code, missing,
        )
    return False


def find_districts_to_verify(
    db_path: str,
    data_dir: str,
    assigned: Optional[List[int]],
    skip: Set[int],
) -> List[Tuple[int, str]]:
    completion = get_district_completion(db_path, assigned or None)
    watch = assigned if assigned else list(completion.keys())
    targets = []

    for d_code in watch:
        if d_code in skip:
            continue
        info = completion.get(d_code)
        if not info:
            continue
        should, reason = ready_for_verification(info)
        if not should:
            continue
        if recently_attempted(d_code):
            log.debug("D%d skipped — attempted recently", d_code)
            continue
        targets.append((d_code, reason))

    # One district at a time — highest completion % first
    targets.sort(key=lambda x: completion.get(x[0], {}).get("pct", 0), reverse=True)
    return targets


async def daemon_loop(
    db_path: str,
    data_dir: str,
    interval: int,
    fetch_missing: bool,
    assigned: List[int],
):
    verified = load_json_set(VERIFIED_FILE)
    skip = COMPLETED_DISTRICTS | verified

    log.info("Auto-Verification Daemon started (per-instance)")
    log.info("  Assigned districts: %s", assigned)
    log.info("  DB: %s | Data: %s | Interval: %ds", db_path, data_dir, interval)
    log.info("  Handoff: ONE district at a time, HTTP workers at 100%% completion")
    log.info("  Already verified: %s", sorted(verified))

    while True:
        try:
            active = get_active_district()
            if active or is_http_takeover_running():
                log.info("HTTP verifier active for D%s — waiting", active or "?")
                await asyncio.sleep(interval)
                continue

            targets = find_districts_to_verify(db_path, data_dir, assigned, skip)

            if targets:
                d_code, reason = targets[0]
                log.info("Next handoff: D%d only (%d candidates queued)", d_code, len(targets))
                if fetch_missing:
                    success = takeover_district(d_code, data_dir, db_path, reason)
                else:
                    success, _ = run_verification_check(d_code, data_dir)
                save_attempt(d_code)
                if success:
                    verified.add(d_code)
                    save_json_set(VERIFIED_FILE, verified)
                    skip.add(d_code)
                    log.info("D%d verified and marked complete.", d_code)
                else:
                    log.warning("D%d takeover incomplete — retry in %dh", d_code, RETRY_HOURS)
            else:
                ensure_http_scraper_running(db_path, assigned)
                completion = get_district_completion(db_path, assigned)
                active_districts = [info for d, info in completion.items() if d not in skip]
                if active_districts:
                    best = max(active_districts, key=lambda x: x["pct"])
                    log.info(
                        "No handoffs. Closest: D%d %s at %.1f%% "
                        "(pend=%d wip=%d err=%d)",
                        best["district_code"], best["district_name"], best["pct"],
                        best["pending"], best["in_progress"], best["errors"],
                    )
        except Exception as e:
            log.error("Daemon loop error: %s", e)

        await asyncio.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Per-instance auto-verification daemon")
    parser.add_argument("--db", default="/home/ubuntu/justokay/work_queue.db")
    parser.add_argument("--data-dir", default="/home/ubuntu/justokay/bhulekh_data")
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--fetch-missing", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--districts", type=int, nargs="*", help="Override assigned districts")
    args = parser.parse_args()

    assigned = args.districts if args.districts else get_assigned_districts()
    if not assigned:
        log.warning("No --districts in service config — will watch all districts on this instance")

    verified = load_json_set(VERIFIED_FILE)
    skip = COMPLETED_DISTRICTS | verified

    if args.once:
        targets = find_districts_to_verify(args.db, args.data_dir, assigned, skip)
        if not targets:
            log.info("No districts ready for verification handoff.")
            return
        d_code, reason = targets[0]
        log.info("Handing off D%d (%s)", d_code, reason)
        if args.fetch_missing:
            success = takeover_district(d_code, args.data_dir, args.db, reason)
        else:
            success, _ = run_verification_check(d_code, args.data_dir)
        save_attempt(d_code)
        if success:
            verified.add(d_code)
            save_json_set(VERIFIED_FILE, verified)
    else:
        asyncio.run(daemon_loop(
            args.db, args.data_dir, args.interval, args.fetch_missing, assigned
        ))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
watchdog.py — Supervisor for http_scraper_v3 with auto-restart and district sequencing.

Features:
- Runs districts in order, completing each before moving to next
- Launches N parallel processes per district
- Detects stalls (no new DB rows for STALL_TIMEOUT seconds) and restarts
- Detects dead processes and respawns
- Periodic progress reporting to log
- Exits cleanly when all districts are done

Usage:
    python watchdog.py --districts 14 18 3 --procs 3 --workers 40
    python watchdog.py --districts 14 18 3 --procs 3 --workers 40 --stall-timeout 600
"""
import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(message)s",
    handlers=[
        logging.FileHandler("watchdog.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")

STALL_TIMEOUT = 600  # seconds with no new rows → restart all procs for current district
CHECK_INTERVAL = 30  # seconds between health checks
PROGRESS_REPORT_INTERVAL = 300  # seconds between progress reports
DONE_THRESHOLD = 1.0  # district is "done" when ALL villages have .done files

DATA_DIR = "bhulekh_data"
PROGRESS_DIR = "progress"
SCRAPER_CMD = [sys.executable, "http_scraper_v3.py"]


def get_db_count(district_code: int) -> int:
    """Get total khatiyans in DB for this district (across all matching files)."""
    total = 0
    base = Path(DATA_DIR)
    for db_path in base.glob("*.db"):
        name = db_path.name
        if f"District-{district_code}" in name:
            try:
                c = sqlite3.connect(str(db_path), timeout=10)
                n = c.execute("SELECT COUNT(*) FROM khatiyans").fetchone()[0]
                c.close()
                total += n
            except Exception:
                pass
        elif name.startswith("district_") and "District-" not in name:
            try:
                c = sqlite3.connect(str(db_path), timeout=10)
                n = c.execute("SELECT COUNT(*) FROM khatiyans").fetchone()[0]
                c.close()
                if n > 0:
                    total += n
            except Exception:
                pass
    # If nothing found with generic approach, try by max(rowid) on the biggest recent file
    return total


def get_village_progress(district_code: int) -> tuple:
    """Returns (done_count, total_villages) from progress directory."""
    prog_dir = Path(PROGRESS_DIR) / str(district_code)
    if not prog_dir.exists():
        return 0, 0
    done = 0
    total = 0
    for tahasil_dir in prog_dir.iterdir():
        if not tahasil_dir.is_dir():
            continue
        for f in tahasil_dir.iterdir():
            if f.suffix == ".done":
                done += 1
                total += 1
            elif f.suffix == ".lock" or f.suffix == ".failed":
                total += 1
    return done, total


def count_done_villages(district_code: int) -> int:
    """Count .done files for this district."""
    prog_dir = Path(PROGRESS_DIR) / str(district_code)
    if not prog_dir.exists():
        return 0
    count = 0
    for root, dirs, files in os.walk(str(prog_dir)):
        for f in files:
            if f.endswith(".done"):
                count += 1
    return count


def get_total_villages(district_code: int, villages_file: str) -> int:
    """Total villages for this district from villages.json."""
    with open(villages_file, encoding="utf-8") as f:
        data = json.load(f)
    return sum(1 for v in data if v.get("district_code") == district_code)


def district_is_complete(district_code: int, villages_file: str) -> bool:
    """Check if district is effectively complete."""
    total = get_total_villages(district_code, villages_file)
    if total == 0:
        return True
    done = count_done_villages(district_code)
    return done / total >= DONE_THRESHOLD


def get_max_rowid(district_code: int) -> int:
    """Fast progress indicator: max rowid in the primary HTTP DB."""
    db_path = Path(DATA_DIR) / f"district_District-{district_code}.db"
    if not db_path.exists():
        return 0
    try:
        c = sqlite3.connect(str(db_path), timeout=10)
        n = c.execute("SELECT MAX(rowid) FROM khatiyans").fetchone()[0] or 0
        c.close()
        return n
    except Exception:
        return 0


def launch_processes(district_code: int, num_procs: int, workers: int, log_prefix: str) -> list:
    """Launch N scraper processes for a district. Returns list of Popen objects."""
    procs = []
    for i in range(num_procs):
        log_file = f"{log_prefix}_p{i+1}.log"
        cmd = SCRAPER_CMD + [
            "--districts", str(district_code),
            "--workers", str(workers),
            "--log-file", log_file,
        ]
        p = subprocess.Popen(
            cmd,
            stdout=open(f"/tmp/watchdog_p{i+1}.out", "w"),
            stderr=subprocess.STDOUT,
        )
        procs.append(p)
        log.info("Launched process %d (PID %d) for D%d: %s", i + 1, p.pid, district_code, " ".join(cmd))
    return procs


def kill_processes(procs: list):
    """Kill all processes in list."""
    for p in procs:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass
    log.info("Killed %d processes", len(procs))


def run_district(district_code: int, num_procs: int, workers: int,
                 villages_file: str, stall_timeout: int) -> bool:
    """
    Supervise scraping of one district until complete.
    Returns True if district completed, False if interrupted.
    """
    log_prefix = f"http_d{district_code}"
    log.info("=" * 60)
    log.info("Starting district %d with %d processes x %d workers", district_code, num_procs, workers)

    total_villages = get_total_villages(district_code, villages_file)
    done_at_start = count_done_villages(district_code)
    log.info("District %d: %d/%d villages done at start (%.1f%%)",
             district_code, done_at_start, total_villages,
             done_at_start / total_villages * 100 if total_villages else 0)

    # Clear stale locks before starting
    prog_dir = Path(PROGRESS_DIR) / str(district_code)
    if prog_dir.exists():
        lock_count = 0
        for root, dirs, files in os.walk(str(prog_dir)):
            for f in files:
                if f.endswith(".lock"):
                    os.remove(os.path.join(root, f))
                    lock_count += 1
        if lock_count:
            log.info("Cleared %d stale .lock files", lock_count)

    procs = launch_processes(district_code, num_procs, workers, log_prefix)
    last_progress_rowid = get_max_rowid(district_code)
    last_progress_time = time.time()
    last_report_time = time.time()

    try:
        while True:
            time.sleep(CHECK_INTERVAL)

            # Check if district is complete
            if district_is_complete(district_code, villages_file):
                done_now = count_done_villages(district_code)
                log.info("District %d COMPLETE! %d/%d villages done.",
                         district_code, done_now, total_villages)
                kill_processes(procs)
                return True

            # Check for dead processes and respawn
            alive = []
            for p in procs:
                if p.poll() is None:
                    alive.append(p)
                else:
                    log.warning("Process PID %d died (exit=%s) — respawning", p.pid, p.returncode)
            if len(alive) < num_procs:
                new_procs = launch_processes(district_code, num_procs - len(alive), workers, log_prefix)
                alive.extend(new_procs)
            procs = alive

            # Progress-based stall detection
            current_rowid = get_max_rowid(district_code)
            done_now = count_done_villages(district_code)
            if current_rowid > last_progress_rowid or done_now > done_at_start:
                last_progress_rowid = current_rowid
                last_progress_time = time.time()
                done_at_start = done_now

            stall_duration = time.time() - last_progress_time
            if stall_duration > stall_timeout:
                log.warning(
                    "District %d STALLED for %ds (no new rows/villages) — restarting all processes",
                    district_code, int(stall_duration),
                )
                kill_processes(procs)
                # Clear locks and relaunch
                if prog_dir.exists():
                    for root, dirs, files in os.walk(str(prog_dir)):
                        for f in files:
                            if f.endswith(".lock"):
                                os.remove(os.path.join(root, f))
                procs = launch_processes(district_code, num_procs, workers, log_prefix)
                last_progress_time = time.time()
                last_progress_rowid = get_max_rowid(district_code)

            # Periodic progress report
            if time.time() - last_report_time > PROGRESS_REPORT_INTERVAL:
                pct = done_now / total_villages * 100 if total_villages else 0
                log.info(
                    "District %d progress: %d/%d villages (%.1f%%), rowid=%d, %d alive procs, load=%.1f",
                    district_code, done_now, total_villages, pct,
                    current_rowid, len(procs),
                    os.getloadavg()[0],
                )
                last_report_time = time.time()

    except KeyboardInterrupt:
        log.info("Interrupted — killing processes")
        kill_processes(procs)
        return False


def main():
    parser = argparse.ArgumentParser(description="Watchdog supervisor for HTTP scraper fleet")
    parser.add_argument("--districts", nargs="+", type=int, required=True,
                        help="Districts to scrape in order (first completes before second starts)")
    parser.add_argument("--procs", type=int, default=3,
                        help="Number of parallel scraper processes per district (default 3)")
    parser.add_argument("--workers", type=int, default=40,
                        help="Workers per process (default 40)")
    parser.add_argument("--stall-timeout", type=int, default=STALL_TIMEOUT,
                        help=f"Seconds without progress before restart (default {STALL_TIMEOUT})")
    parser.add_argument("--villages-file", default="villages.json")
    args = parser.parse_args()

    log.info("Watchdog starting: districts=%s procs=%d workers=%d stall=%ds",
             args.districts, args.procs, args.workers, args.stall_timeout)

    for district_code in args.districts:
        if district_is_complete(district_code, args.villages_file):
            log.info("District %d already complete — skipping", district_code)
            continue

        completed = run_district(
            district_code, args.procs, args.workers,
            args.villages_file, args.stall_timeout,
        )
        if not completed:
            log.info("Stopping (interrupted during district %d)", district_code)
            break

    log.info("Watchdog finished all districts.")


if __name__ == "__main__":
    main()

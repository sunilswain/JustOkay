"""
Bhulekh Scraper v3 — File-based tracking, no work_queue.db dependency.

Progress is tracked via filesystem markers:
  progress/{district_code}/{tahasil_code}/{village_code}.done

Workers claim villages by atomically creating .lock files.
Data is stored in per-district SQLite DBs (same as before).

Usage:
    # Generate village list (one-time, from existing work_queue.db or SOAP)
    uv run python scraper_v3.py enumerate --db work_queue.db

    # Scrape districts with 20 headless workers
    uv run python scraper_v3.py scrape --districts 3 8 --workers 20

    # Check progress
    uv run python scraper_v3.py status --districts 3 8

    # Export to CSV (per village files)
    uv run python scraper_v3.py export --district 3
"""

import argparse
import asyncio
import json
import logging
import multiprocessing
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bhulekh_scraper import BhulekhScraper
from storage import DEFAULT_DATA_DIR

PROGRESS_DIR = "progress"
VILLAGES_FILE = "villages.json"

# District processing order: near-complete first, then smallest remaining.
# Sambalpur (12) is 100% done and excluded from scraping.
DISTRICT_PRIORITY = [
    10, 23, 3, 9, 21, 16, 26, 20, 27, 30, 24, 19, 29, 25,
    15, 28, 4, 17, 22, 11, 13, 2, 18, 14, 8, 7, 6, 1, 5,
]
_DISTRICT_PRIORITY_INDEX = {code: i for i, code in enumerate(DISTRICT_PRIORITY)}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(processName)s] %(message)s",
    handlers=[
        logging.FileHandler("scraper_v3.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ── Village List Management ───────────────────────────────────────────────────

def load_villages(villages_file: str = VILLAGES_FILE) -> List[Dict[str, Any]]:
    """Load village list from JSON file."""
    with open(villages_file, "r", encoding="utf-8") as f:
        return json.load(f)


def enumerate_from_queue_db(db_path: str, out_file: str = VILLAGES_FILE) -> int:
    """Extract village list from existing work_queue.db into villages.json."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT district_code, district_name, tahasil_code, tahasil_name, "
        "village_code, village_name, khatiyan_count FROM villages ORDER BY district_code, tahasil_code, village_code"
    ).fetchall()
    conn.close()

    villages = [dict(r) for r in rows]
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(villages, f, ensure_ascii=False, indent=1)

    logger.info("Exported %d villages to %s", len(villages), out_file)
    return len(villages)


# ── File-Based Progress Tracking ──────────────────────────────────────────────

def _progress_path(progress_dir: str, district_code: int, tahasil_code: int, village_code: int, ext: str) -> str:
    return os.path.join(progress_dir, str(district_code), str(tahasil_code), f"{village_code}{ext}")


def is_village_done(progress_dir: str, district_code: int, tahasil_code: int, village_code: int) -> bool:
    return os.path.exists(_progress_path(progress_dir, district_code, tahasil_code, village_code, ".done"))


def try_claim_village(progress_dir: str, district_code: int, tahasil_code: int, village_code: int, worker_id: str) -> bool:
    """Atomically claim a village by creating a .lock file. Returns True if claimed."""
    lock_path = _progress_path(progress_dir, district_code, tahasil_code, village_code, ".lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{worker_id}\n{time.time()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        # Check if lock is stale (> 30 minutes old)
        try:
            age = time.time() - os.path.getmtime(lock_path)
            if age > 1800:
                os.remove(lock_path)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{worker_id}\n{time.time()}\n".encode())
                os.close(fd)
                return True
        except (OSError, FileExistsError):
            pass
        return False


def mark_village_done(progress_dir: str, district_code: int, tahasil_code: int, village_code: int, khatiyans: int):
    """Mark village as done by renaming .lock -> .done (or creating .done directly)."""
    lock_path = _progress_path(progress_dir, district_code, tahasil_code, village_code, ".lock")
    done_path = _progress_path(progress_dir, district_code, tahasil_code, village_code, ".done")
    os.makedirs(os.path.dirname(done_path), exist_ok=True)

    content = f"{khatiyans}\n{time.time()}\n"
    # Write .done file
    with open(done_path, "w") as f:
        f.write(content)
    # Remove .lock if it exists
    try:
        os.remove(lock_path)
    except OSError:
        pass


def mark_village_failed(progress_dir: str, district_code: int, tahasil_code: int, village_code: int, error: str):
    """Remove lock so village can be retried later."""
    lock_path = _progress_path(progress_dir, district_code, tahasil_code, village_code, ".lock")
    try:
        os.remove(lock_path)
    except OSError:
        pass


# ── Worker Process ────────────────────────────────────────────────────────────

def _worker_main(
    worker_id: str,
    villages_file: str,
    progress_dir: str,
    data_dir: str,
    district_codes: List[int],
    base_url: str,
    delay_scale: float,
) -> None:
    """Entry point for each worker process."""

    async def _run():
        villages = load_villages(villages_file)
        # Filter to assigned districts, then sort by district priority
        my_villages = [v for v in villages if v["district_code"] in district_codes]
        my_villages.sort(
            key=lambda v: _DISTRICT_PRIORITY_INDEX.get(v["district_code"], len(DISTRICT_PRIORITY))
        )

        scraper = BhulekhScraper(
            base_url=base_url,
            data_dir=data_dir,
            resume=True,
            storage_backend="sqlite",
            delay_scale=delay_scale,
        )

        # Init browser
        for attempt in range(5):
            try:
                await scraper.init_browser(headless=True)
                await scraper.navigate_to_ror_page()
                break
            except Exception as e:
                wait = min(30 * (attempt + 1), 180)
                logger.warning("Worker %s: browser init failed (attempt %d/5), waiting %ds: %s",
                               worker_id, attempt + 1, wait, e)
                try:
                    await scraper.cleanup()
                except Exception:
                    pass
                if attempt == 4:
                    logger.error("Worker %s: giving up after 5 attempts", worker_id)
                    return
                await asyncio.sleep(wait)

        logger.info("Worker %s: ready, %d villages in scope", worker_id, len(my_villages))

        consecutive_failures = 0
        villages_done = 0
        total_khatiyans = 0
        start_time = time.time()

        for village in my_villages:
            d_code = village["district_code"]
            t_code = village["tahasil_code"]
            v_code = village["village_code"]
            d_name = village["district_name"]
            t_name = village["tahasil_name"]
            v_name = village["village_name"]
            expected = village.get("khatiyan_count", 0)

            # Skip if already done
            if is_village_done(progress_dir, d_code, t_code, v_code):
                continue

            # Try to claim
            if not try_claim_village(progress_dir, d_code, t_code, v_code, worker_id):
                continue

            # Site-down detection
            if consecutive_failures >= 5:
                logger.warning("Worker %s: 5 consecutive failures, waiting 120s...", worker_id)
                await asyncio.sleep(120)
                consecutive_failures = 0
                # Restart browser
                try:
                    await scraper.cleanup()
                except Exception:
                    pass
                await scraper.init_browser(headless=True)
                await scraper.navigate_to_ror_page()

            logger.info("Worker %s: scraping %s (D%d/T%d) | expected: %d khatiyans",
                        worker_id, v_name, d_code, t_code, expected)

            try:
                # Set up storage for this district
                from storage import create_storage
                storage = create_storage(data_dir, d_name, backend="sqlite")
                scraper._current_storage = storage
                scraper._current_district_value = str(d_code)
                scraper._current_district_text = d_name
                scraper.khatiyans_processed = 0
                scraper._last_khatiyan_no = None

                # Navigate
                if scraper.page.url != scraper.start_url:
                    await scraper.navigate_to_ror_page()

                from bhulekh_scraper import SELECTOR_DISTRICT, SELECTOR_TAHASIL, SELECTOR_VILLAGE
                await scraper.select_dropdown(SELECTOR_DISTRICT, str(d_code))
                await scraper.select_search_type("Khatiyan")
                if not await scraper.wait_for_dropdown_populated(SELECTOR_TAHASIL, min_options=1, timeout_ms=25000):
                    raise Exception(f"Tahasil dropdown did not populate for D{d_code}")
                await scraper.human_delay(0.1, 0.3)

                await scraper.select_dropdown(SELECTOR_TAHASIL, str(t_code))
                if not await scraper.wait_for_dropdown_populated(SELECTOR_VILLAGE, min_options=1, timeout_ms=25000):
                    raise Exception(f"Village dropdown did not populate for T{t_code}")
                await scraper.human_delay(0.1, 0.3)

                # Process village with timeout
                await asyncio.wait_for(
                    scraper.process_village(
                        village_value=str(v_code),
                        village_text=v_name,
                        district=d_name,
                        tahasil=t_name,
                        tahasil_value=str(t_code),
                    ),
                    timeout=600,  # 10 min max per village
                )

                kh_done = scraper.khatiyans_processed
                if kh_done == 0 and expected > 5:
                    raise Exception(f"0 khatiyans scraped (expected {expected})")

                mark_village_done(progress_dir, d_code, t_code, v_code, kh_done)
                villages_done += 1
                total_khatiyans += kh_done
                consecutive_failures = 0

                elapsed = time.time() - start_time
                speed = total_khatiyans / (elapsed / 60) if elapsed > 60 else 0
                logger.info("Worker %s: done %s | %d khatiyans | speed: %.0f/min | total: %d",
                            worker_id, v_name, kh_done, speed, total_khatiyans)

                # Periodic browser restart to prevent memory leaks
                if villages_done % 40 == 0:
                    logger.info("Worker %s: restarting browser (memory cleanup)", worker_id)
                    try:
                        await scraper.cleanup()
                    except Exception:
                        pass
                    await scraper.init_browser(headless=True)
                    await scraper.navigate_to_ror_page()

            except asyncio.TimeoutError:
                logger.error("Worker %s: village %s timed out (600s)", worker_id, v_name)
                mark_village_failed(progress_dir, d_code, t_code, v_code, "timeout")
                consecutive_failures += 1
            except Exception as e:
                logger.error("Worker %s: village %s failed: %s", worker_id, v_name, str(e)[:200])
                mark_village_failed(progress_dir, d_code, t_code, v_code, str(e)[:200])
                consecutive_failures += 1

        logger.info("Worker %s: finished. %d villages, %d khatiyans in %.0f min",
                    worker_id, villages_done, total_khatiyans, (time.time() - start_time) / 60)
        try:
            await scraper.cleanup()
        except Exception:
            pass

    asyncio.run(_run())


# ── Supervisor / Main Commands ────────────────────────────────────────────────

def cmd_scrape(args):
    """Spawn N workers and supervise them."""
    if not os.path.exists(args.villages):
        logger.error("Village list not found: %s", args.villages)
        logger.error("Run first: uv run python scraper_v3.py enumerate --db work_queue.db")
        sys.exit(1)

    os.makedirs(args.progress_dir, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)

    # Disk space check
    free_gb = shutil.disk_usage(args.data_dir).free / (1024 ** 3)
    if free_gb < 2.0:
        logger.error("DISK SPACE LOW: %.1f GB free — aborting.", free_gb)
        sys.exit(1)
    logger.info("Disk space: %.1f GB free", free_gb)

    # Show initial status
    _print_status(args.villages, args.progress_dir, args.districts)

    n_workers = args.workers
    hostname = socket.gethostname()
    delay_scale = 0.15 if args.fast else 0.5

    def _make_worker(slot: int) -> multiprocessing.Process:
        wid = f"{hostname}-{os.getpid()}-w{slot}"
        p = multiprocessing.Process(
            target=_worker_main,
            name=f"Worker-{slot}",
            kwargs=dict(
                worker_id=wid,
                villages_file=args.villages,
                progress_dir=args.progress_dir,
                data_dir=args.data_dir,
                district_codes=args.districts,
                base_url=args.url,
                delay_scale=delay_scale,
            ),
        )
        p.start()
        logger.info("Started %s (pid=%d)", p.name, p.pid)
        return p

    processes = [_make_worker(i) for i in range(n_workers)]

    # Supervisor loop
    poll_interval = 5
    last_status = time.time()
    while True:
        time.sleep(poll_interval)

        # Replace dead workers
        alive = 0
        for i, p in enumerate(processes):
            if not p.is_alive():
                logger.warning("Worker-%d (pid=%d) exited (code=%s) — respawning",
                               i, p.pid, p.exitcode)
                p.join()
                processes[i] = _make_worker(i)
            else:
                alive += 1

        # Check if all work is done
        villages = load_villages(args.villages)
        my_villages = [v for v in villages if v["district_code"] in args.districts]
        done_count = sum(1 for v in my_villages
                         if is_village_done(args.progress_dir, v["district_code"], v["tahasil_code"], v["village_code"]))

        if done_count >= len(my_villages):
            logger.info("All %d villages complete!", done_count)
            break

        # Periodic status log
        if time.time() - last_status > 120:
            pct = 100 * done_count / len(my_villages) if my_villages else 0
            logger.info("Progress: %d/%d villages (%.1f%%) | %d/%d workers alive",
                        done_count, len(my_villages), pct, alive, n_workers)
            last_status = time.time()

    # Clean shutdown
    for p in processes:
        if p.is_alive():
            p.join(timeout=60)
            if p.is_alive():
                p.terminate()

    logger.info("All workers finished.")
    _print_status(args.villages, args.progress_dir, args.districts)


def cmd_status(args):
    """Show progress by district."""
    if not os.path.exists(args.villages):
        print(f"Village list not found: {args.villages}")
        print("Run: uv run python scraper_v3.py enumerate --db work_queue.db")
        return

    _print_status(args.villages, args.progress_dir, args.districts)


def _print_status(villages_file: str, progress_dir: str, district_filter: Optional[List[int]] = None):
    """Print progress status."""
    villages = load_villages(villages_file)
    if district_filter:
        villages = [v for v in villages if v["district_code"] in district_filter]

    # Group by district
    from collections import defaultdict
    by_district: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
        "name": "", "total": 0, "done": 0, "locked": 0, "est_khatiyans": 0
    })

    for v in villages:
        d = v["district_code"]
        by_district[d]["name"] = v["district_name"]
        by_district[d]["total"] += 1
        by_district[d]["est_khatiyans"] += v.get("khatiyan_count", 0)
        if is_village_done(progress_dir, d, v["tahasil_code"], v["village_code"]):
            by_district[d]["done"] += 1
        elif os.path.exists(_progress_path(progress_dir, d, v["tahasil_code"], v["village_code"], ".lock")):
            by_district[d]["locked"] += 1

    total_villages = sum(d["total"] for d in by_district.values())
    total_done = sum(d["done"] for d in by_district.values())
    total_locked = sum(d["locked"] for d in by_district.values())
    total_kh = sum(d["est_khatiyans"] for d in by_district.values())

    print(f"\n{'='*70}")
    print(f"  OVERALL: {total_done}/{total_villages} villages done ({100*total_done//total_villages if total_villages else 0}%) | "
          f"in_progress={total_locked} | est_khatiyans={total_kh:,}")
    print(f"{'='*70}")
    print(f"  {'District':<20} {'Done':<12} {'In Progress':<14} {'Pending':<12} {'%':<6}")
    print(f"  {'-'*20} {'-'*12} {'-'*14} {'-'*12} {'-'*6}")

    for d_code in sorted(by_district.keys()):
        d = by_district[d_code]
        pending = d["total"] - d["done"] - d["locked"]
        pct = f"{100*d['done']//d['total']}%" if d["total"] else "-"
        print(f"  {d['name']:<20} {d['done']:<12} {d['locked']:<14} {pending:<12} {pct}")

    print()


def cmd_enumerate(args):
    """Generate villages.json from work_queue.db."""
    if not os.path.exists(args.db):
        print(f"ERROR: Queue DB not found: {args.db}")
        sys.exit(1)
    n = enumerate_from_queue_db(args.db, args.out)
    print(f"Exported {n} villages to {args.out}")


def cmd_export(args):
    """Export scraped data to CSV files organized by district/tahasil/village."""
    from export_csv import HEADER, flatten_ror, iter_all_records
    import csv

    export_dir = args.export_dir
    district_filter = [args.district] if args.district else None

    records = iter_all_records(args.data_dir, district_filter=district_filter)

    files_written = 0
    rows_written = 0
    current_file = None
    current_writer = None
    current_key = None

    for record in records:
        flat_rows = flatten_ror(record)
        if not flat_rows:
            continue

        for row in flat_rows:
            district = row.get("district", "unknown").strip() or "unknown"
            tahasil = row.get("tahasil", "unknown").strip() or "unknown"
            village = row.get("village", "unknown").strip() or "unknown"

            # Sanitize names for filesystem
            district_safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in district).strip()
            tahasil_safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in tahasil).strip()
            village_safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in village).strip()

            key = (district_safe, tahasil_safe, village_safe)

            if key != current_key:
                if current_file:
                    current_file.close()

                out_dir = os.path.join(export_dir, district_safe, tahasil_safe)
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, f"{village_safe}.csv")

                is_new = not os.path.exists(out_path)
                current_file = open(out_path, "a", newline="", encoding="utf-8-sig")
                current_writer = csv.DictWriter(current_file, fieldnames=HEADER, extrasaction="ignore")
                if is_new:
                    current_writer.writeheader()
                    files_written += 1
                current_key = key

            current_writer.writerow(row)
            rows_written += 1

    if current_file:
        current_file.close()

    print(f"\nExport complete: {rows_written:,} rows -> {files_written} village CSV files")
    print(f"Output: {export_dir}/")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bhulekh Scraper v3 — file-based, no work_queue dependency")
    parser.add_argument("--villages", default=VILLAGES_FILE, help="Path to villages.json")
    parser.add_argument("--progress-dir", default=PROGRESS_DIR, help="Directory for .done/.lock markers")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="Directory for district_*.db files")

    sub = parser.add_subparsers(dest="command")

    # enumerate
    p_enum = sub.add_parser("enumerate", help="Generate villages.json from work_queue.db")
    p_enum.add_argument("--db", default="work_queue.db", help="Path to work_queue.db")
    p_enum.add_argument("--out", default=VILLAGES_FILE, help="Output file")

    # scrape
    p_scrape = sub.add_parser("scrape", help="Scrape with N headless workers")
    p_scrape.add_argument("--districts", nargs="+", type=int, required=True, help="District codes to scrape")
    p_scrape.add_argument("--workers", type=int, default=20, help="Number of workers (default: 20)")
    p_scrape.add_argument("--url", default="http://bhulekh.ori.nic.in", help="Bhulekh base URL")
    p_scrape.add_argument("--fast", action="store_true", help="Minimal delays")

    # status
    p_status = sub.add_parser("status", help="Show progress")
    p_status.add_argument("--districts", nargs="+", type=int, default=None, help="Filter to these districts")

    # export
    p_export = sub.add_parser("export", help="Export to CSV by village")
    p_export.add_argument("--district", type=int, default=None, help="Export specific district code")
    p_export.add_argument("--export-dir", default="exports", help="Output directory")

    args = parser.parse_args()

    if args.command == "enumerate":
        cmd_enumerate(args)
    elif args.command == "scrape":
        cmd_scrape(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "export":
        cmd_export(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

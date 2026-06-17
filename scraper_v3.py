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

from bhulekh_scraper import BhulekhScraper, _checkpoint_looks_corrupt
from storage import DEFAULT_DATA_DIR

PROGRESS_DIR = "progress"
VILLAGE_TIMEOUT = 1800  # 30 min max per village (resume reduces work on retries)
KHATIYAN_DONE_THRESHOLD = 0.98  # Only write .done when DB count meets this fraction of expected
SMALL_GAP_RETRY = 20  # Re-queue underscraped villages with <= this many khatiyans missing
VILLAGES_FILE = "villages.json"


def village_khatiyan_complete(got: int, expected: int, threshold: float = KHATIYAN_DONE_THRESHOLD) -> bool:
    if expected <= 0:
        return got > 0
    return got >= max(expected - 5, int(expected * threshold))

# Districts to SKIP (already scraped or being scraped locally):
# 12=Sambalpur(done), 28=Boudh, 29=Deogarh, 30=Jharsuguda, 25=Malkangiri,
# 17=Jagatsinghpur, 4=Dhenkanal, 23=Subarnapur, 21=Nuapada, 10=Kandhamal(local),
# 27=Rayagada, 22=Nayagarh
SKIP_DISTRICTS = {12, 28, 29, 30, 25, 17, 4, 23, 21, 27, 22}

# Also skip Cuttack tahasil (code=4) within Cuttack district (code=3)
SKIP_TAHASILS = {(3, 4)}  # (district_code, tahasil_code)

# Default priority when district_priority.json is absent.
DEFAULT_DISTRICT_PRIORITY = [
    10, 24, 14, 18, 3, 20, 9, 16, 26, 19,
    15, 11, 13, 2, 8, 7, 6, 1, 5,
]
PRIORITY_FILE = "district_priority.json"


def load_priority_config(base_dir: str = ".") -> tuple:
    """Load priority list and optional parallel district codes from district_priority.json."""
    path = os.path.join(base_dir, PRIORITY_FILE)
    priority = list(DEFAULT_DISTRICT_PRIORITY)
    parallel: List[int] = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [int(x) for x in data], []
            if isinstance(data, dict):
                if data.get("priority"):
                    priority = [int(x) for x in data["priority"]]
                if data.get("parallel"):
                    parallel = [int(x) for x in data["parallel"]]
        except Exception:
            pass
    return priority, parallel


def load_priority_list(base_dir: str = ".") -> List[int]:
    return load_priority_config(base_dir)[0]


def priority_index(base_dir: str = ".") -> Dict[int, int]:
    priority, _ = load_priority_config(base_dir)
    return {code: i for i, code in enumerate(priority)}


def sort_villages_for_worker(
    villages: List[Dict[str, Any]],
    progress_dir: str,
    base_dir: str = ".",
) -> List[Dict[str, Any]]:
    """Order villages for workers.

    When ``parallel`` is set in district_priority.json, pending villages from those
    districts are round-robin interleaved so workers split across them (e.g. Gajapati
    + Kandhamal simultaneously). Other pending villages sort by backlog then priority.
    """
    priority, parallel_dcs = load_priority_config(base_dir)
    pri = {code: i for i, code in enumerate(priority)}
    fallback = len(pri) + 100
    parallel_set = set(parallel_dcs)

    district_pending: Dict[int, int] = {}
    for v in villages:
        d = v["district_code"]
        if not is_village_done(progress_dir, d, v["tahasil_code"], v["village_code"]):
            district_pending[d] = district_pending.get(d, 0) + 1

    parallel_buckets: Dict[int, List[Dict[str, Any]]] = {d: [] for d in parallel_dcs}
    other_pending: List[Dict[str, Any]] = []
    done_villages: List[Dict[str, Any]] = []

    for v in villages:
        d = v["district_code"]
        if is_village_done(progress_dir, d, v["tahasil_code"], v["village_code"]):
            done_villages.append(v)
            continue
        if d in parallel_set:
            parallel_buckets[d].append(v)
        else:
            other_pending.append(v)

    stable = lambda v: (v["tahasil_code"], v["village_code"])
    for bucket in parallel_buckets.values():
        bucket.sort(key=stable)

    interleaved: List[Dict[str, Any]] = []
    buckets = [parallel_buckets[d] for d in parallel_dcs if d in parallel_buckets]
    while any(buckets):
        for bucket in buckets:
            if bucket:
                interleaved.append(bucket.pop(0))

    def _other_key(v: Dict[str, Any]):
        d = v["district_code"]
        return (
            district_pending.get(d, 0),
            pri.get(d, fallback),
            v["tahasil_code"],
            v["village_code"],
        )

    other_pending.sort(key=_other_key)
    return interleaved + other_pending + done_villages

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


def _get_village_resume_from_storage(
    storage,
    tahasil_name: str,
    village_name: str,
    village_code: int,
) -> tuple[int, Optional[str], set]:
    """
    Query DB for khatiyans already scraped in this village.
    Returns (already_done_count, resume_after_khatiyan_value, existing_khatiyan_values).
    On failure, returns (0, None, set()) so scraping proceeds from scratch.
    """
    try:
        if not hasattr(storage, "get_existing_khatiyans"):
            return 0, None, set()

        existing = storage.get_existing_khatiyans(tahasil_name, village_name)
        already_done = len(existing)
        if already_done == 0:
            return 0, None, set()

        resume_kh = None
        if hasattr(storage, "get_checkpoint"):
            cp = storage.get_checkpoint()
            if (
                cp
                and str(cp.get("village_value")) == str(village_code)
                and cp.get("tahasil_text") == tahasil_name
            ):
                resume_kh = cp.get("last_khatiyan_value")

        if not resume_kh and hasattr(storage, "_conn"):
            row = storage._conn().execute(
                "SELECT khatiyan_value FROM khatiyans "
                "WHERE tahasil = ? AND village = ? AND needs_review = 0 "
                "ORDER BY rowid DESC LIMIT 1",
                (tahasil_name, village_name),
            ).fetchone()
            if row:
                resume_kh = row[0]

        return already_done, resume_kh, existing
    except Exception as e:
        logger.warning(
            "Resume query failed for %s/%s, starting fresh: %s",
            tahasil_name, village_name, e,
        )
        return 0, None, set()


# ── Worker Process ────────────────────────────────────────────────────────────

def _worker_main(
    worker_id: str,
    villages_file: str,
    progress_dir: str,
    data_dir: str,
    district_codes: List[int],
    base_url: str,
    delay_scale: float,
    work_dir: str,
) -> None:
    """Entry point for each worker process."""

    async def _run():
        villages = load_villages(villages_file)
        # Filter to assigned districts, exclude skipped districts/tahasils
        my_villages = [
            v for v in villages
            if v["district_code"] in district_codes
            and v["district_code"] not in SKIP_DISTRICTS
            and (v["district_code"], v["tahasil_code"]) not in SKIP_TAHASILS
        ]

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
        retry_queue: List[Dict[str, Any]] = []

        while True:
            # Re-sort each pass so district_priority.json changes take effect without restart.
            ordered = sort_villages_for_worker(my_villages, progress_dir, work_dir)
            seen_retry = set()
            retry_slice = []
            for v in retry_queue:
                key = (v["district_code"], v["tahasil_code"], v["village_code"])
                if key not in seen_retry:
                    seen_retry.add(key)
                    retry_slice.append(v)
            retry_queue = retry_slice
            ordered = retry_slice + [
                v for v in ordered
                if (v["district_code"], v["tahasil_code"], v["village_code"]) not in seen_retry
            ]
            claimed_this_pass = False
            for village in ordered:
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

                claimed_this_pass = True

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
                    storage = create_storage(data_dir, d_name, backend="sqlite", district_code=d_code)
                    scraper._current_storage = storage
                    scraper._current_district_value = str(d_code)
                    scraper._current_district_text = d_name
                    scraper.khatiyans_processed = 0
                    scraper._last_khatiyan_no = None

                    already_done, resume_kh, existing_kh = _get_village_resume_from_storage(
                        storage, t_name, v_name, v_code,
                    )
                    if resume_kh and _checkpoint_looks_corrupt(resume_kh, already_done, expected):
                        logger.warning(
                            "Worker %s: clearing corrupt resume checkpoint for %s "
                            "(last_khatiyan=%r, in_db=%d, expected=%d)",
                            worker_id, v_name, resume_kh, already_done, expected,
                        )
                        resume_kh = None

                    # Position-based resume skips scattered gaps (e.g. last checkpoint
                    # at end of dropdown while 1–4 khatiyans failed earlier). When the
                    # village is incomplete but we have DB rows, scan from the start and
                    # rely on skip_khatiyan_values to fetch only missing khatiyans.
                    if existing_kh and not village_khatiyan_complete(already_done, expected):
                        if resume_kh:
                            logger.info(
                                "Worker %s: %s incomplete (%d/%d) — value-skip only, "
                                "ignoring position checkpoint %r",
                                worker_id, v_name, already_done, expected, resume_kh,
                            )
                        resume_kh = None

                    if already_done > 0:
                        suffix = f", after {resume_kh!r}" if resume_kh else ""
                        logger.info(
                            "Worker %s: resuming %s — %d/%d khatiyans already in DB%s",
                            worker_id, v_name, already_done, expected, suffix,
                        )

                    # Village already complete in DB — mark done without re-scraping
                    if village_khatiyan_complete(already_done, expected):
                        logger.info(
                            "Worker %s: %s already has %d/%d khatiyans in DB — marking done",
                            worker_id, v_name, already_done, expected,
                        )
                        mark_village_done(progress_dir, d_code, t_code, v_code, already_done)
                        villages_done += 1
                        total_khatiyans += 0
                        consecutive_failures = 0
                        continue

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

                    # Process village with timeout (resume skips khatiyans already in DB)
                    await asyncio.wait_for(
                        scraper.process_village(
                            village_value=str(v_code),
                            village_text=v_name,
                            district=d_name,
                            tahasil=t_name,
                            tahasil_value=str(t_code),
                            start_after_khatiyan_value=resume_kh,
                            expected_khatiyan_count=expected or None,
                            skip_khatiyan_values=existing_kh or None,
                        ),
                        timeout=VILLAGE_TIMEOUT,
                    )

                    if hasattr(storage, "get_existing_khatiyans"):
                        kh_done = len(storage.get_existing_khatiyans(t_name, v_name))
                    else:
                        kh_done = already_done + scraper.khatiyans_processed

                    new_this_run = scraper.khatiyans_processed
                    complete = village_khatiyan_complete(kh_done, expected)
                    if kh_done == 0 and expected > 5 and not complete:
                        raise Exception(f"0 khatiyans scraped (expected {expected})")
                    if expected > 0 and kh_done < expected * 0.5 and new_this_run == 0 and not complete:
                        raise Exception(
                            f"no progress: {kh_done}/{expected} khatiyans in DB after resume"
                        )

                    if complete:
                        mark_village_done(progress_dir, d_code, t_code, v_code, kh_done)
                        villages_done += 1
                        total_khatiyans += kh_done
                        consecutive_failures = 0
                        elapsed = time.time() - start_time
                        speed = total_khatiyans / (elapsed / 60) if elapsed > 60 else 0
                        logger.info(
                            "Worker %s: done %s | %d khatiyans | speed: %.0f/min | total: %d",
                            worker_id, v_name, kh_done, speed, total_khatiyans,
                        )
                    else:
                        pct = (kh_done / expected * 100) if expected else 0
                        gap = max(0, expected - kh_done)
                        logger.warning(
                            "Worker %s: %s only %d/%d khatiyans (%.0f%%) — not marking .done, will retry",
                            worker_id, v_name, kh_done, expected, pct,
                        )
                        mark_village_failed(progress_dir, d_code, t_code, v_code, "underscraped")
                        consecutive_failures += 1
                        if gap <= SMALL_GAP_RETRY:
                            retry_queue.append(village)
                            logger.info(
                                "Worker %s: %s has %d khatiyans left — queued for immediate retry",
                                worker_id, v_name, gap,
                            )
                            break

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
                    logger.error("Worker %s: village %s timed out (%ds)", worker_id, v_name, VILLAGE_TIMEOUT)
                    mark_village_failed(progress_dir, d_code, t_code, v_code, "timeout")
                    consecutive_failures += 1
                except Exception as e:
                    logger.error("Worker %s: village %s failed: %s", worker_id, v_name, str(e)[:200])
                    mark_village_failed(progress_dir, d_code, t_code, v_code, str(e)[:200])
                    consecutive_failures += 1

            if not claimed_this_pass:
                break

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

    work_dir = os.path.dirname(os.path.abspath(args.villages)) or os.getcwd()

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
                work_dir=work_dir,
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


def cmd_set_priority(args):
    """Write district_priority.json — workers pick this up on their next village pass."""
    work_dir = os.path.dirname(os.path.abspath(args.villages)) or os.getcwd()
    path = os.path.join(work_dir, PRIORITY_FILE)
    payload = {
        "priority": args.districts,
        "updated_at": time.time(),
    }
    if args.parallel:
        payload["parallel"] = args.parallel
    if args.note:
        payload["note"] = args.note
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(args.districts)} districts to {path}")
    print("Priority (first = highest):", " ".join(str(d) for d in args.districts))
    print("Workers reload this file automatically — no service restart needed.")


def cmd_priority(args):
    """Show current district priority."""
    work_dir = os.path.dirname(os.path.abspath(args.villages)) or os.getcwd()
    path = os.path.join(work_dir, PRIORITY_FILE)
    lst = load_priority_list(work_dir)
    print(f"Priority file: {path} ({'exists' if os.path.isfile(path) else 'using defaults'})")
    for i, dc in enumerate(lst):
        print(f"  {i + 1:>2}. D{dc}")


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

    # set-priority (live reprioritization)
    p_pri = sub.add_parser(
        "set-priority",
        help="Set district scrape priority (district_priority.json, no restart needed)",
    )
    p_pri.add_argument(
        "--districts", nargs="+", type=int, required=True,
        help="District codes in priority order (first = highest)",
    )
    p_pri.add_argument(
        "--parallel", nargs="*", type=int, default=None,
        help="District codes to scrape in parallel (round-robin interleave)",
    )
    p_pri.add_argument("--note", default="", help="Optional note stored in JSON")

    p_show_pri = sub.add_parser("priority", help="Show current district priority")

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
    elif args.command == "set-priority":
        cmd_set_priority(args)
    elif args.command == "priority":
        cmd_priority(args)
    elif args.command == "export":
        cmd_export(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

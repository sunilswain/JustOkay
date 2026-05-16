"""
Phase 2 multi-worker runner — village-level parallelism.

Each worker process claims one village at a time from work_queue.db,
scrapes all its khatiyans, saves full RoR data to per-district SQLite/NDJSON,
then claims the next village.  Workers never overlap.

Compared to the old run_workers.py (district-level):
  - Old: max 30 parallel workers (one per district)
  - New: up to ~51,727 parallel workers (one per village)

Usage examples:

  # Step 1: Build the work queue (run once, ~30-60 minutes)
  python soap_enumerator.py --db work_queue.db --concurrency 20

  # Step 2: Start N workers (can be on one machine or multiple)
  python run_village_workers.py --workers 8 --db work_queue.db --data-dir bhulekh_data --headless

  # Process specific districts first (e.g. Cuttack=3, Angul=14)
  python run_village_workers.py --workers 8 --db work_queue.db --data-dir bhulekh_data --headless --districts 3 14

  # Check progress at any time
  python work_queue.py stats --db work_queue.db
  python work_queue.py districts --db work_queue.db

Resume:
  Just re-run the same command.  Completed villages are skipped.
  Partially-done villages are resumed from last_khatiyan_no.
  Crashed workers' villages are reclaimed after 1 hour automatically.

Distributed (multiple machines):
  Copy work_queue.db to a shared path (NFS/EFS) and point every machine at it.
  Or copy and rsync periodically — SQLite WAL mode handles concurrent access.
"""

import argparse
import asyncio
import logging
import multiprocessing
import os
import socket
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bhulekh_scraper import BhulekhScraper, logger
from work_queue import create_queue, get_stats
from storage import DEFAULT_DATA_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(processName)s %(message)s",
    handlers=[
        logging.FileHandler("village_workers.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def _worker_main(
    worker_id: str,
    queue_path: str,
    data_dir: str,
    headless: bool,
    base_url: str,
    storage_backend: str,
    delay_scale: float,
    district_codes: Optional[List[int]],
    limit_khatiyans: Optional[int],
    api_key: Optional[str] = None,
) -> None:
    """Entry point for each worker process."""
    async def _run() -> None:
        scraper = BhulekhScraper(
            base_url=base_url,
            data_dir=data_dir,
            resume=True,                # always resume within a village
            storage_backend=storage_backend,
            delay_scale=delay_scale,
            limit_khatiyans=limit_khatiyans,
        )
        scraper._queue_api_key = api_key  # passed through to run_from_queue
        await scraper.run_from_queue(
            queue_path=queue_path,
            headless=headless,
            worker_id=worker_id,
            district_codes=district_codes,
        )

    asyncio.run(_run())


def _print_stats(queue_path: str) -> None:
    try:
        s = get_stats(queue_path)
        total_v  = s["total_villages"]
        total_kh = s["total_khatiyans_est"]
        done_kh  = s["total_khatiyans_fetched"]
        by_s     = s["by_status"]

        done_v   = by_s.get("done",        {}).get("villages", 0)
        pend_v   = by_s.get("pending",     {}).get("villages", 0)
        prog_v   = by_s.get("in_progress", {}).get("villages", 0)
        err_v    = by_s.get("error",       {}).get("villages", 0)

        pct_v  = f"{100*done_v//total_v}%" if total_v else "-"
        pct_kh = f"{100*done_kh//total_kh}%" if total_kh else "-"

        print(
            f"\nQueue: {total_v:,} villages | "
            f"done={done_v:,} ({pct_v}) | "
            f"in_progress={prog_v} | "
            f"pending={pend_v:,} | "
            f"error={err_v}"
        )
        print(
            f"Khatiyans: ~{total_kh:,} est | "
            f"{done_kh:,} fetched ({pct_kh})\n"
        )
    except Exception as e:
        print(f"(stats unavailable: {e})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 2 runner: N workers each claiming villages from work_queue.db. "
            "Run soap_enumerator.py first to build the queue."
        )
    )
    parser.add_argument("--workers",   type=int,  default=4,            metavar="N",
                        help="Number of parallel worker processes (default: 4)")
    parser.add_argument("--db",        type=str,  default="work_queue.db", metavar="PATH",
                        help="Local SQLite queue file OR remote server URL (http://host:8000)")
    parser.add_argument("--key",       type=str,  default=None,
                        help="API key for remote queue server (matches --key on queue_server.py)")
    parser.add_argument("--data-dir",  type=str,  default=DEFAULT_DATA_DIR, metavar="DIR",
                        help="Directory for per-district storage files")
    parser.add_argument("--headless",  action="store_true",
                        help="Run browsers headlessly (required for servers)")
    parser.add_argument("--url",       type=str,  default="http://bhulekh.ori.nic.in",
                        help="Bhulekh base URL")
    parser.add_argument("--storage",   choices=["sqlite", "ndjson"], default="sqlite",
                        help="Storage backend (default: sqlite)")
    parser.add_argument("--fast",      action="store_true",
                        help="Minimal artificial delays (faster; may cause more timeouts)")
    parser.add_argument("--districts", nargs="+", type=int, metavar="CODE",
                        help="Only process these district codes (e.g. --districts 3 14)")
    parser.add_argument("--priority-districts", nargs="+", type=int, default=[], metavar="CODE",
                        help="Boost these districts to the front of the queue before starting workers")
    parser.add_argument("--priority-level", type=int, default=10,
                        help="Priority value used with --priority-districts (default: 10)")
    parser.add_argument("--limit-khatiyans", type=int, default=None, metavar="N",
                        help="Stop each worker after N khatiyans total (for testing)")
    args = parser.parse_args()

    # Validate queue (local file check or remote health check)
    is_remote = args.db.startswith("http://") or args.db.startswith("https://")
    if is_remote:
        import httpx
        headers = {"X-Api-Key": args.key} if args.key else {}
        try:
            r = httpx.get(f"{args.db.rstrip('/')}/health", headers=headers, timeout=10)
            r.raise_for_status()
            print(f"Queue server reachable: {args.db}")
        except Exception as e:
            print(f"ERROR: Cannot reach queue server at {args.db}: {e}")
            sys.exit(1)
    elif not os.path.exists(args.db):
        print(f"ERROR: Queue file not found: {args.db}")
        print("Run first:  python soap_enumerator.py --db", args.db)
        sys.exit(1)

    # Apply priority boost before starting workers
    if args.priority_districts:
        from work_queue import make_queue, set_priority
        q = make_queue(args.db, api_key=args.key)
        if hasattr(q, 'set_priority'):
            n = q.set_priority(args.priority_districts, args.priority_level)
        else:
            n = set_priority(args.db, args.priority_districts, args.priority_level)
        print(f"Priority={args.priority_level} set for {n} villages in districts {args.priority_districts}")

    _print_stats(args.db)

    n_workers = args.workers
    delay_scale = 0.15 if args.fast else 1.0
    hostname = socket.gethostname()

    processes = []
    for i in range(n_workers):
        worker_id = f"{hostname}-{os.getpid()}-w{i}"
        p = multiprocessing.Process(
            target=_worker_main,
            name=f"VillageWorker-{i}",
            kwargs=dict(
                worker_id=worker_id,
                queue_path=args.db,
                data_dir=args.data_dir,
                headless=args.headless,
                base_url=args.url,
                storage_backend=args.storage,
                api_key=args.key,
                delay_scale=delay_scale,
                district_codes=args.districts,
                limit_khatiyans=args.limit_khatiyans,
            ),
        )
        p.start()
        processes.append(p)
        logger.info("Started %s (pid=%d)", p.name, p.pid)

    for p in processes:
        p.join()

    logger.info("All workers finished.")
    _print_stats(args.db)


if __name__ == "__main__":
    main()

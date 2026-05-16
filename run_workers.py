"""
Multi-worker runner for Bhulekh scraper.

- Fetches district list once, then partitions districts across N worker processes.
- Each worker runs its own browser and writes to per-district SQLite in --data-dir.
- No shared state: each process owns its district subset, so no locking.
- Run with --workers 4 --data-dir bhulekh_data --resume to go fast and resume on crash.
"""

import argparse
import asyncio
import logging
import multiprocessing
import os
import sys
from typing import Any, Dict, List

# Ensure project root is on path when run as script or subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bhulekh_scraper import BhulekhScraper, get_district_list, logger
from storage import DEFAULT_DATA_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(processName)s - %(message)s",
    handlers=[
        logging.FileHandler("bhulekh_workers.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


def _worker_main(
    worker_id: int,
    districts: List[Dict[str, str]],
    data_dir: str,
    resume: bool,
    headless: bool,
    base_url: str,
    limit_khatiyans: Any,
    storage_backend: str = "sqlite",
    delay_scale: float = 1.0,
) -> None:
    """Entry point for each worker process. Runs async scraper for its district subset."""
    if not districts:
        logger.info("Worker %s: no districts assigned, exiting", worker_id)
        return
    async def run_worker() -> None:
        scraper = BhulekhScraper(
            base_url=base_url,
            data_dir=data_dir,
            resume=resume,
            limit_khatiyans=limit_khatiyans,
            storage_backend=storage_backend,
            delay_scale=delay_scale,
        )
        # Run only these districts (value, text)
        scraper._districts_to_run = [(d["value"], d["text"]) for d in districts]
        try:
            await scraper.run(headless=headless)
        finally:
            await scraper.cleanup()
    asyncio.run(run_worker())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Bhulekh scraper with multiple workers (one browser per worker)."
    )
    parser.add_argument("--workers", type=int, default=4, metavar="N", help="Number of worker processes (default: 4)")
    parser.add_argument("--data-dir", type=str, default=DEFAULT_DATA_DIR, metavar="DIR", help="Storage directory (SQLite per district)")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint per district")
    parser.add_argument("--headless", action="store_true", help="Run browsers headless")
    parser.add_argument("--url", type=str, default="http://bhulekh.ori.nic.in", help="Base URL")
    parser.add_argument("--limit-khatiyans", type=int, default=None, metavar="N", help="Stop each worker after N khatiyans (for testing)")
    parser.add_argument("--storage", type=str, choices=["sqlite", "ndjson"], default="sqlite", help="Storage: sqlite or ndjson (JSON Lines, often faster)")
    parser.add_argument("--fast", action="store_true", help="Minimal delays (faster; may trigger site timeouts/blocks)")
    args = parser.parse_args()

    # Fetch district list in main process (one browser)
    logger.info("Fetching district list...")
    districts = asyncio.run(get_district_list(base_url=args.url, headless=args.headless))
    if not districts:
        logger.error("No districts found. Check URL and network.")
        sys.exit(1)
    logger.info("Found %d districts", len(districts))

    n = min(args.workers, len(districts))
    # Partition: worker i gets districts [i, i+n, i+2n, ...]
    worker_districts: List[List[Dict[str, str]]] = [[] for _ in range(n)]
    for i, d in enumerate(districts):
        worker_districts[i % n].append(d)

    for i, wd in enumerate(worker_districts):
        logger.info("Worker %s: %d districts %s", i, len(wd), [d.get("text", d.get("value")) for d in wd[:3]] + (["..."] if len(wd) > 3 else []))

    processes = []
    for i in range(n):
        p = multiprocessing.Process(
            target=_worker_main,
            name=f"Worker-{i}",
            kwargs=dict(
                worker_id=i,
                districts=worker_districts[i],
                data_dir=args.data_dir,
                resume=args.resume,
                headless=args.headless,
                base_url=args.url,
                limit_khatiyans=args.limit_khatiyans,
                storage_backend=args.storage,
                delay_scale=0.15 if args.fast else 1.0,
            ),
        )
        p.start()
        processes.append(p)

    for p in processes:
        p.join()
    logger.info("All workers finished.")


if __name__ == "__main__":
    main()

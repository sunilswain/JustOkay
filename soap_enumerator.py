"""
Phase 1: Enumerate all Odisha villages + khatiyan counts via SOAP HTTP GET.

Calls BhulekhService.asmx — no auth, no browser, no VIEWSTATE.
Writes every village as a work unit into work_queue.db.

Usage:
  # Enumerate all 30 districts (takes ~30-60 minutes with --concurrency 20)
  python soap_enumerator.py --db work_queue.db

  # Only specific districts, max concurrency
  python soap_enumerator.py --districts 14 3 10 --concurrency 30

  # Skip khatiyan count fetch (faster, just builds village list)
  python soap_enumerator.py --no-khatiyan-count

  # Boost priority for districts you want scraped first
  python soap_enumerator.py --priority-districts 14 3 --priority-level 10

Resume: safe to re-run at any time. Already-known villages are skipped (UPSERT).
"""

import asyncio
import argparse
import logging
import time
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

from work_queue import create_queue, upsert_village, set_priority, get_stats

SOAP_BASE = "http://bhulekh.ori.nic.in/BhulekhService.asmx"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# All 30 Odisha district codes (from web UI option values)
ALL_DISTRICT_CODES = list(range(1, 31))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("soap_enumerator.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── SOAP helpers ─────────────────────────────────────────────────────────────

def _parse_rows(xml_text: str) -> list[dict]:
    """Parse ASP.NET DataSet XML into list of dicts."""
    try:
        root = ET.fromstring(xml_text)
        rows = []
        for table in root.iter("Table"):
            row = {child.tag: (child.text or "").strip() for child in table}
            if row:
                rows.append(row)
        return rows
    except ET.ParseError as e:
        log.warning("XML parse error: %s | text: %s", e, xml_text[:200])
        return []


async def soap_get(
    client: httpx.AsyncClient,
    op: str,
    params: dict,
    retries: int = 4,
) -> list[dict]:
    """HTTP GET call to BhulekhService with exponential backoff retry."""
    url = f"{SOAP_BASE}/{op}"
    for attempt in range(retries):
        try:
            r = await client.get(url, params=params, timeout=60)
            if r.status_code == 500 and "ConnectionString" in r.text:
                # Permanent server-side error for this combo — return empty
                return []
            r.raise_for_status()
            return _parse_rows(r.text)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            wait = 2 ** attempt
            log.warning("%s %s attempt %d/%d failed: %s — retrying in %ds",
                        op, params, attempt + 1, retries, e, wait)
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as e:
            log.warning("%s %s HTTP %d — skipping", op, params, e.response.status_code)
            return []
    log.error("%s %s failed after %d retries", op, params, retries)
    return []


# ── Enumeration logic ─────────────────────────────────────────────────────────

async def fetch_districts(client: httpx.AsyncClient) -> list[dict]:
    """
    Districts don't have a 'get all' call — dCode=0 returns a server error.
    We try codes 1–30 in parallel and keep the ones that respond.
    """
    tasks = [
        soap_get(client, "DistrictsUnicode", {"dCode": code})
        for code in ALL_DISTRICT_CODES
    ]
    results = await asyncio.gather(*tasks)
    districts = []
    for code, rows in zip(ALL_DISTRICT_CODES, results):
        if rows:
            districts.append({"code": str(code), "oname": rows[0].get("oname", str(code))})
    log.info("Districts found: %d", len(districts))
    return districts


async def fetch_tahasils(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    district_code: int,
) -> list[dict]:
    async with sem:
        rows = await soap_get(client, "TahasilsUnicode", {"dCode": district_code})
    return rows


async def fetch_villages(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    district_code: int,
    tahasil_code: int,
) -> list[dict]:
    async with sem:
        return await soap_get(
            client, "VillagesUnicode", {"dCode": district_code, "tCode": tahasil_code}
        )


async def fetch_khatiyan_count(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    district_code: int,
    tahasil_code: int,
    village_code: int,
) -> int:
    async with sem:
        rows = await soap_get(
            client,
            "KhatiyanUnicode",
            {"dCode": district_code, "tCode": tahasil_code, "vCode": village_code},
        )
    return len(rows)


async def enumerate_district(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    db_path: str,
    district_code: int,
    district_name: str,
    fetch_khatiyans: bool,
    priority_districts: set,
    priority_level: int,
) -> tuple[int, int]:
    """Enumerate all tahasils + villages for one district. Returns (villages, khatiyans_est)."""
    tahasils = await fetch_tahasils(client, sem, district_code)
    if not tahasils:
        log.warning("District %d (%s): no tahasils found", district_code, district_name)
        return 0, 0

    priority = priority_level if district_code in priority_districts else 0
    total_villages = 0
    total_khatiyans = 0

    for tah in tahasils:
        tah_code = int(tah["code"])
        tah_name = tah.get("oname", str(tah_code))

        villages = await fetch_villages(client, sem, district_code, tah_code)
        if not villages:
            log.warning("D%d T%d: no villages", district_code, tah_code)
            continue

        # Optionally fetch khatiyan counts in parallel for this tahasil
        khatiyan_counts: list[int] = []
        if fetch_khatiyans:
            count_tasks = [
                fetch_khatiyan_count(client, sem, district_code, tah_code, int(v["code"]))
                for v in villages
            ]
            khatiyan_counts = list(await asyncio.gather(*count_tasks))
        else:
            khatiyan_counts = [0] * len(villages)

        for vil, kh_count in zip(villages, khatiyan_counts):
            vil_code = int(vil["code"])
            vil_name = vil.get("oname", str(vil_code))
            upsert_village(
                db_path=db_path,
                district_code=district_code,
                district_name=district_name,
                tahasil_code=tah_code,
                tahasil_name=tah_name,
                village_code=vil_code,
                village_name=vil_name,
                khatiyan_count=kh_count,
                priority=priority,
            )
            total_villages += 1
            total_khatiyans += kh_count

        log.info(
            "D%d %s | T%d %s | %d villages | ~%d khatiyans",
            district_code, district_name, tah_code, tah_name,
            len(villages), sum(khatiyan_counts),
        )

    return total_villages, total_khatiyans


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(
    db_path: str,
    district_codes: Optional[list[int]],
    concurrency: int,
    fetch_khatiyans: bool,
    priority_districts: list[int],
    priority_level: int,
) -> None:
    create_queue(db_path)

    priority_set = set(priority_districts)
    target_codes = district_codes if district_codes else ALL_DISTRICT_CODES

    limits = httpx.Limits(max_connections=concurrency + 5, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        headers={"User-Agent": UA},
        limits=limits,
        follow_redirects=True,
        timeout=60,
    ) as client:
        # First resolve district names (fast parallel call)
        log.info("Resolving district names...")
        all_districts = await fetch_districts(client)
        name_map = {int(d["code"]): d["oname"] for d in all_districts}

        sem = asyncio.Semaphore(concurrency)

        log.info("Enumerating %d districts with concurrency=%d, fetch_khatiyans=%s",
                 len(target_codes), concurrency, fetch_khatiyans)
        t_start = time.time()

        tasks = [
            enumerate_district(
                client=client,
                sem=sem,
                db_path=db_path,
                district_code=code,
                district_name=name_map.get(code, str(code)),
                fetch_khatiyans=fetch_khatiyans,
                priority_districts=priority_set,
                priority_level=priority_level,
            )
            for code in target_codes
            if code in name_map  # skip invalid codes
        ]

        results = await asyncio.gather(*tasks)

    total_villages = sum(r[0] for r in results)
    total_khatiyans = sum(r[1] for r in results)
    elapsed = time.time() - t_start

    log.info(
        "Enumeration complete in %.1fs — %d villages, ~%d khatiyans estimated",
        elapsed, total_villages, total_khatiyans,
    )

    # Apply priority boosts
    if priority_districts:
        n = set_priority(db_path, priority_districts, priority_level)
        log.info("Priority=%d set for %d villages in districts %s",
                 priority_level, n, priority_districts)

    # Final stats
    stats = get_stats(db_path)
    log.info("Queue stats: %s", stats)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 1: Enumerate all Bhulekh villages into work_queue.db via SOAP."
    )
    parser.add_argument(
        "--db", default="work_queue.db", metavar="PATH",
        help="Work queue SQLite file (default: work_queue.db)",
    )
    parser.add_argument(
        "--districts", nargs="+", type=int, metavar="CODE",
        help="Only enumerate these district codes (default: all 1-30)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=20, metavar="N",
        help="Max concurrent SOAP HTTP calls (default: 20)",
    )
    parser.add_argument(
        "--no-khatiyan-count", action="store_true",
        help="Skip fetching khatiyan counts per village (faster, ~5 min vs ~35 min)",
    )
    parser.add_argument(
        "--priority-districts", nargs="+", type=int, default=[], metavar="CODE",
        help="District codes to process first in Phase 2",
    )
    parser.add_argument(
        "--priority-level", type=int, default=10,
        help="Priority value for priority districts (default: 10)",
    )
    args = parser.parse_args()

    asyncio.run(run(
        db_path=args.db,
        district_codes=args.districts,
        concurrency=args.concurrency,
        fetch_khatiyans=not args.no_khatiyan_count,
        priority_districts=args.priority_districts,
        priority_level=args.priority_level,
    ))


if __name__ == "__main__":
    main()

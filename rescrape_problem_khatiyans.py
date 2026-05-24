#!/usr/bin/env python3
"""
Re-scrape specific khatiyans that have extraction problems without re-running entire villages.

Usage:
    # Preview what would be re-scraped
    python rescrape_problem_khatiyans.py --data-dir bhulekh_data --preview

    # Re-scrape all problem khatiyans in a district
    python rescrape_problem_khatiyans.py --data-dir bhulekh_data --district ଅନୁଗୋଳ

    # Re-scrape specific village only
    python rescrape_problem_khatiyans.py --data-dir bhulekh_data --district ଅନୁଗୋଳ --tahasil ଅନୁଗୋଳ --village ଆଙ୍କୁଲା

    # Limit to N khatiyans (for testing)
    python rescrape_problem_khatiyans.py --data-dir bhulekh_data --district ଅନୁଗୋଳ --limit 10
"""

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bhulekh_scraper import (
    BhulekhScraper,
    SELECTOR_DISTRICT,
    SELECTOR_KHATIYAN,
    SELECTOR_TAHASIL,
    SELECTOR_VILLAGE,
)
from storage import get_storage_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def normalize_odia(text: str) -> str:
    if not text:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    return normalized.replace("\u0b3c", "")


def find_match(options: List[Dict[str, str]], target: str) -> Optional[Dict[str, str]]:
    target_norm = normalize_odia(target)
    for option in options:
        if option["text"] == target:
            return option
    for option in options:
        if normalize_odia(option["text"]) == target_norm:
            return option
    for option in options:
        opt_norm = normalize_odia(option["text"])
        if target_norm in opt_norm or opt_norm in target_norm:
            return option
    return None


def khatiyan_has_problems(data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Return whether a khatiyan record has extraction problems and why."""
    reasons: List[str] = []
    plots = data.get("plots") or []

    if not plots:
        return True, ["zero_plots"]

    for plot in plots:
        plot_no = str(plot.get("plot_no", "") or "").strip()
        if not plot_no:
            reasons.append("missing_plot_no")
            break

        acre = str(plot.get("acre", "") or "").strip()
        decimil = str(plot.get("decimil", "") or "").strip()
        hector = str(plot.get("hector", "") or "").strip()
        kisam = str(plot.get("kisam", "") or "")
        land_type = str(plot.get("land_type", "") or "")

        if "ଉପଲବ୍ଧ ନାହିଁ" in kisam or "ଉପଲବ୍ଧ ନାହିଁ" in land_type:
            continue

        if not acre and not decimil and not hector:
            reasons.append("empty_area")
            break

    return bool(reasons), reasons


def scan_problem_khatiyans(
    db_path: Path,
    *,
    district_filter: Optional[str] = None,
    tahasil_filter: Optional[str] = None,
    village_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Find khatiyans with extraction problems in a district database."""
    problems: List[Dict[str, Any]] = []

    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            """
            SELECT id, district, tahasil, village, khatiyan_value, khatiyan_text, data_json
            FROM khatiyans
            """
        )
        for row in cursor:
            kh_id, district, tahasil, village, kh_value, kh_text, data_json = row

            if district_filter and district != district_filter:
                continue
            if tahasil_filter and tahasil != tahasil_filter:
                continue
            if village_filter and village != village_filter:
                continue

            try:
                data = json.loads(data_json)
            except json.JSONDecodeError:
                problems.append(
                    {
                        "id": kh_id,
                        "district": district,
                        "tahasil": tahasil,
                        "village": village,
                        "khatiyan_value": kh_value,
                        "khatiyan_text": kh_text,
                        "reasons": ["invalid_json"],
                    }
                )
                continue

            has_problem, reasons = khatiyan_has_problems(data)
            if has_problem:
                problems.append(
                    {
                        "id": kh_id,
                        "district": district,
                        "tahasil": tahasil,
                        "village": village,
                        "khatiyan_value": kh_value,
                        "khatiyan_text": kh_text,
                        "reasons": reasons,
                    }
                )

        conn.close()
    except Exception as exc:
        logger.error("Error reading %s: %s", db_path, exc)

    return problems


def get_district_name_from_db(db_path: Path) -> str:
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        return ""


async def navigate_to_village(
    scraper: BhulekhScraper,
    district: str,
    tahasil: str,
    village: str,
) -> Dict[str, str]:
    """Select district, tahasil, and village; return resolved dropdown values."""
    district_opts = await scraper.get_dropdown_options(SELECTOR_DISTRICT)
    district_match = find_match(district_opts, district)
    if not district_match:
        raise ValueError(f"District {district!r} not found")

    scraper._current_district_value = district_match["value"]
    scraper._current_district_text = district_match["text"]
    await scraper.select_dropdown(SELECTOR_DISTRICT, district_match["value"], wait_for_update=True)
    await scraper.select_search_type("Khatiyan")

    if not await scraper.wait_for_dropdown_populated(SELECTOR_TAHASIL, min_options=1):
        raise ValueError("Tahasil dropdown did not populate")

    tahasil_opts = await scraper.get_dropdown_options(SELECTOR_TAHASIL)
    tahasil_match = find_match(tahasil_opts, tahasil)
    if not tahasil_match:
        raise ValueError(f"Tahasil {tahasil!r} not found")

    await scraper.select_dropdown(SELECTOR_TAHASIL, tahasil_match["value"], wait_for_update=True)

    if not await scraper.wait_for_dropdown_populated(SELECTOR_VILLAGE, min_options=1):
        raise ValueError("Village dropdown did not populate")

    village_opts = await scraper.get_dropdown_options(SELECTOR_VILLAGE)
    village_match = find_match(village_opts, village)
    if not village_match:
        raise ValueError(f"Village {village!r} not found")

    await scraper.select_dropdown(SELECTOR_VILLAGE, village_match["value"], wait_for_update=True)

    if not await scraper.wait_for_dropdown_populated(SELECTOR_KHATIYAN, min_options=1):
        raise ValueError("Khatiyan dropdown did not populate")

    return {
        "district_text": district_match["text"],
        "district_value": district_match["value"],
        "tahasil_text": tahasil_match["text"],
        "tahasil_value": tahasil_match["value"],
        "village_text": village_match["text"],
        "village_value": village_match["value"],
    }


async def rescrape_khatiyan(
    scraper: BhulekhScraper,
    kh: Dict[str, Any],
    location: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """Re-scrape one khatiyan and return extracted RoR data."""
    start_count = len(scraper.data_list)
    ok = await scraper.process_khatiyan(
        khatiyan_value=kh["khatiyan_value"],
        khatiyan_text=kh["khatiyan_text"],
        district=location["district_text"],
        tahasil=location["tahasil_text"],
        village=location["village_text"],
        tahasil_value=location["tahasil_value"],
        village_value=location["village_value"],
    )
    if not ok or len(scraper.data_list) <= start_count:
        return None
    return scraper.data_list[-1]


def preview_problems(problems: List[Dict[str, Any]], limit: int = 50) -> None:
    reason_counts: Dict[str, int] = defaultdict(int)
    for kh in problems:
        for reason in kh["reasons"]:
            reason_counts[reason] += 1

    print(f"\nFound {len(problems)} problem khatiyan(s)")
    print("Reason counts:")
    for reason, count in sorted(reason_counts.items()):
        print(f"  {reason}: {count}")

    print("\nSample records:")
    for kh in problems[:limit]:
        reasons = ", ".join(kh["reasons"])
        print(
            f"  [{kh['id']}] {kh['district']}/{kh['tahasil']}/{kh['village']}/"
            f"Khatiyan {kh['khatiyan_text']} ({reasons})"
        )
    if len(problems) > limit:
        print(f"  ... and {len(problems) - limit} more")


async def rescrape_problems(
    data_dir: Path,
    *,
    district_filter: Optional[str] = None,
    tahasil_filter: Optional[str] = None,
    village_filter: Optional[str] = None,
    limit: Optional[int] = None,
    preview: bool = False,
    headless: bool = True,
) -> int:
    db_files = sorted(data_dir.glob("district_*.db"))
    if not db_files:
        logger.error("No district databases found in %s", data_dir)
        return 1

    all_problems: List[Dict[str, Any]] = []
    district_db_map: Dict[str, Path] = {}

    for db_path in db_files:
        district = get_district_name_from_db(db_path)
        if not district:
            continue
        if district_filter and district != district_filter:
            continue

        district_db_map[district] = db_path
        found = scan_problem_khatiyans(
            db_path,
            district_filter=district_filter,
            tahasil_filter=tahasil_filter,
            village_filter=village_filter,
        )
        if found:
            logger.info("%s: %s problem khatiyan(s)", district, len(found))
            all_problems.extend(found)

    if not all_problems:
        logger.info("No problem khatiyans found")
        return 0

    if limit:
        all_problems = all_problems[:limit]
        logger.info("Limited to %s khatiyan(s)", limit)

    if preview:
        preview_problems(all_problems)
        return 0

    grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for kh in all_problems:
        key = (kh["district"], kh["tahasil"], kh["village"])
        grouped[key].append(kh)

    scraper = BhulekhScraper()
    success_count = 0
    still_problem = 0
    fail_count = 0

    try:
        await scraper.init_browser(headless=headless)
        await scraper.navigate_to_ror_page()

        processed = 0
        total = len(all_problems)

        for (district, tahasil, village), khatiyans in sorted(grouped.items()):
            logger.info("Village %s/%s/%s (%s khatiyan(s))", district, tahasil, village, len(khatiyans))
            try:
                location = await navigate_to_village(scraper, district, tahasil, village)
            except Exception as exc:
                logger.error("Failed to navigate to %s/%s/%s: %s", district, tahasil, village, exc)
                fail_count += len(khatiyans)
                processed += len(khatiyans)
                continue

            storage = get_storage_manager(str(data_dir), district_name=district)

            for kh in khatiyans:
                processed += 1
                logger.info(
                    "[%s/%s] Re-scraping %s/%s/%s/Khatiyan %s (%s)",
                    processed,
                    total,
                    district,
                    tahasil,
                    village,
                    kh["khatiyan_text"],
                    ", ".join(kh["reasons"]),
                )

                try:
                    ror_data = await rescrape_khatiyan(scraper, kh, location)
                    if not ror_data:
                        logger.error("  Failed to extract data")
                        fail_count += 1
                        continue

                    has_problem, reasons = khatiyan_has_problems(ror_data)
                    needs_review = 1 if has_problem else 0
                    updated = storage.update_khatiyan(
                        kh["id"],
                        ror_data,
                        needs_review=needs_review,
                    )
                    if not updated:
                        logger.error("  Failed to update database record %s", kh["id"])
                        fail_count += 1
                        continue

                    plots_count = len(ror_data.get("plots") or [])
                    if has_problem:
                        logger.warning(
                            "  Updated but still has problems (%s): %s plot(s)",
                            ", ".join(reasons),
                            plots_count,
                        )
                        still_problem += 1
                    else:
                        logger.info("  Updated successfully with %s plot(s)", plots_count)
                        success_count += 1

                except Exception as exc:
                    logger.error("  Error: %s", exc)
                    fail_count += 1
                    try:
                        await scraper.cleanup()
                        await scraper.init_browser(headless=headless)
                        await scraper.navigate_to_ror_page()
                        location = await navigate_to_village(scraper, district, tahasil, village)
                    except Exception:
                        logger.exception("Failed to recover browser session")
                        return 1

            storage.close()

    finally:
        await scraper.cleanup()

    logger.info("\n%s", "=" * 60)
    logger.info("RE-SCRAPE COMPLETE")
    logger.info("  Fixed: %s", success_count)
    logger.info("  Still problematic: %s", still_problem)
    logger.info("  Failed: %s", fail_count)
    logger.info("%s", "=" * 60)

    return 0 if fail_count == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-scrape khatiyans with extraction problems without touching the work queue"
    )
    parser.add_argument("--data-dir", default="bhulekh_data", help="Directory containing district databases")
    parser.add_argument("--district", help="Only process this district (Odia name)")
    parser.add_argument("--tahasil", help="Only process this tahasil (Odia name)")
    parser.add_argument("--village", help="Only process this village (Odia name)")
    parser.add_argument("--limit", type=int, help="Limit number of khatiyans to process")
    parser.add_argument("--preview", action="store_true", help="Preview problem khatiyans without re-scraping")
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser headless (default: True)")
    parser.add_argument("--no-headless", action="store_false", dest="headless", help="Show browser window")
    args = parser.parse_args()

    data_path = Path(args.data_dir)
    if not data_path.exists():
        print(f"ERROR: Data directory not found: {args.data_dir}", file=sys.stderr)
        return 1

    return asyncio.run(
        rescrape_problems(
            data_path,
            district_filter=args.district,
            tahasil_filter=args.tahasil,
            village_filter=args.village,
            limit=args.limit,
            preview=args.preview,
            headless=args.headless,
        )
    )


if __name__ == "__main__":
    sys.exit(main())

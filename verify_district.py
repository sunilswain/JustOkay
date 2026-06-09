#!/usr/bin/env python3
"""
District Verification Pipeline.

Independently verifies that ALL villages and khatiyans for a district have been scraped
by comparing against the live Bhulekh website. Does NOT depend on work_queue.db.

If missing data is found, uses Playwright to fetch it.

Usage:
    python verify_district.py --district 5 --data-dir bhulekh_data
    python verify_district.py --district 5 --data-dir bhulekh_data --fetch-missing
    python verify_district.py --district 5 --data-dir bhulekh_data --fetch-missing --headless

Runs on dedicated verification instances with Playwright installed.
"""
import asyncio
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from ror_parser import parse_ror_html

SOAP_BASE = "http://bhulekh.ori.nic.in/BhulekhService.asmx"
ROR_URL = "http://bhulekh.ori.nic.in/RoRView.aspx"
NAV_TIMEOUT_MS = 120_000
KHATIYAN_RETRIES = 4
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("verify_district.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── SOAP Helpers ──────────────────────────────────────────────────────────────

def _parse_rows(xml_text: str) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
        rows = []
        for table in root.iter("Table"):
            row = {child.tag: (child.text or "").strip() for child in table}
            if row:
                rows.append(row)
        return rows
    except ET.ParseError:
        return []


async def soap_get(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    op: str,
    params: dict,
    retries: int = 4,
) -> List[Dict[str, str]]:
    url = f"{SOAP_BASE}/{op}"
    async with sem:
        for attempt in range(retries):
            try:
                r = await client.get(url, params=params, timeout=60)
                if r.status_code == 500 and "ConnectionString" in r.text:
                    return []
                r.raise_for_status()
                return _parse_rows(r.text)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
            except httpx.HTTPStatusError:
                return []
    return []


# ── Website Enumeration ───────────────────────────────────────────────────────

async def enumerate_from_website(district_code: int, concurrency: int = 20) -> Dict:
    """Get authoritative village+khatiyan list from the website."""
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency + 5, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(
        headers={"User-Agent": UA},
        limits=limits,
        follow_redirects=True,
        timeout=60,
    ) as client:
        dist_rows = await soap_get(client, sem, "DistrictsUnicode", {"dCode": district_code})
        district_name = dist_rows[0].get("oname", str(district_code)) if dist_rows else str(district_code)
        log.info("Enumerating district %d: %s", district_code, district_name)

        tahasils = await soap_get(client, sem, "TahasilsUnicode", {"dCode": district_code})
        log.info("  Found %d tahasils", len(tahasils))

        result = {
            "district_code": district_code,
            "district_name": district_name,
            "villages": [],
        }

        for tah in tahasils:
            tah_code = int(tah["code"])
            tah_name = tah.get("oname", str(tah_code))

            villages = await soap_get(
                client, sem, "VillagesUnicode",
                {"dCode": district_code, "tCode": tah_code},
            )

            async def _get_khatiyans(v_code: int):
                rows = await soap_get(
                    client, sem, "KhatiyanUnicode",
                    {"dCode": district_code, "tCode": tah_code, "vCode": v_code},
                )
                return v_code, rows

            tasks = [_get_khatiyans(int(v["code"])) for v in villages]
            kh_results = await asyncio.gather(*tasks)
            kh_map = {vc: rows for vc, rows in kh_results}

            for v in villages:
                v_code = int(v["code"])
                v_name = v.get("oname", str(v_code))
                kh_rows = kh_map.get(v_code, [])
                khatiyan_ids = []
                for row in kh_rows:
                    kh_id = row.get("okhata_no", row.get("khatiyannumber", row.get("code", "")))
                    if kh_id:
                        khatiyan_ids.append(kh_id.strip())

                result["villages"].append({
                    "district_code": district_code,
                    "district_name": district_name,
                    "tahasil_code": tah_code,
                    "tahasil_name": tah_name,
                    "village_code": v_code,
                    "village_name": v_name,
                    "khatiyan_ids": khatiyan_ids,
                    "khatiyan_count": len(khatiyan_ids),
                })

            log.info("  T%d %s: %d villages", tah_code, tah_name, len(villages))

    return result


# ── District DB Comparison ────────────────────────────────────────────────────

def find_district_db(data_dir: str, district_code: int, district_name: str) -> Optional[Path]:
    """Find the district DB file (tries multiple name patterns)."""
    data_path = Path(data_dir)
    patterns = [
        f"district_District-{district_code}.db",
        f"district_{district_name}.db",
    ]
    # Also try sanitized Odia name
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in district_name).strip()
    patterns.append(f"district_{safe}.db")

    for pat in patterns:
        candidate = data_path / pat
        if candidate.exists():
            return candidate

    # Fallback: scan all district DBs
    for db_file in data_path.glob("district_*.db"):
        try:
            conn = sqlite3.connect(str(db_file))
            row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
            conn.close()
            if row and (row[0] == district_name or str(district_code) in db_file.name):
                return db_file
        except Exception:
            pass
    return None


def get_scraped_khatiyans(db_path: Path) -> Dict[Tuple[str, str], Set[str]]:
    """
    Get all scraped khatiyans from the district DB.
    Returns: {(tahasil_name, village_name): set(khatiyan_values)}
    """
    result = {}
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT tahasil, village, khatiyan_value FROM khatiyans"
        ).fetchall()
        conn.close()

        for tahasil, village, kh_val in rows:
            key = (tahasil, village)
            if key not in result:
                result[key] = set()
            result[key].add(kh_val.strip() if kh_val else "")

    except Exception as e:
        log.error("Error reading district DB %s: %s", db_path, e)

    return result


def compare_data(website_data: Dict, scraped_data: Dict) -> Dict:
    """Compare website enumeration against scraped data."""
    report = {
        "district_code": website_data["district_code"],
        "district_name": website_data["district_name"],
        "total_villages_on_website": len(website_data["villages"]),
        "total_khatiyans_on_website": sum(v["khatiyan_count"] for v in website_data["villages"]),
        "missing_villages": [],
        "incomplete_villages": [],
        "complete_villages": 0,
        "total_missing_khatiyans": 0,
    }

    for village_info in website_data["villages"]:
        v_name = village_info["village_name"]
        t_name = village_info["tahasil_name"]
        expected_khatiyans = set(village_info["khatiyan_ids"])

        # Try to find this village in scraped data (fuzzy match on name)
        scraped_kh = scraped_data.get((t_name, v_name), set())

        if not scraped_kh and expected_khatiyans:
            report["missing_villages"].append(village_info)
            report["total_missing_khatiyans"] += len(expected_khatiyans)
        elif expected_khatiyans:
            missing = expected_khatiyans - scraped_kh
            if missing:
                village_info["missing_khatiyans"] = list(missing)
                village_info["scraped_count"] = len(scraped_kh)
                report["incomplete_villages"].append(village_info)
                report["total_missing_khatiyans"] += len(missing)
            else:
                report["complete_villages"] += 1

    return report


# ── Playwright Fetcher ────────────────────────────────────────────────────────

async def fetch_missing_with_playwright(
    report: Dict,
    data_dir: str,
    headless: bool = True,
    max_workers: int = 3,
):
    """Use Playwright to fetch missing khatiyans."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return

    missing_work = []
    for village in report["missing_villages"]:
        for kh_id in village["khatiyan_ids"]:
            missing_work.append((village, kh_id))
    for village in report["incomplete_villages"]:
        for kh_id in village.get("missing_khatiyans", []):
            missing_work.append((village, kh_id))

    if not missing_work:
        log.info("No missing khatiyans to fetch.")
        return

    log.info("Fetching %d missing khatiyans via Playwright (headless=%s)", len(missing_work), headless)

    # Find/create district DB for storing fetched data
    district_code = report["district_code"]
    district_name = report["district_name"]
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in district_name).strip() or "unknown"
    db_path = Path(data_dir) / f"district_{safe_name}.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS khatiyans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            district TEXT NOT NULL,
            tahasil TEXT NOT NULL,
            village TEXT NOT NULL,
            khatiyan_value TEXT NOT NULL,
            khatiyan_text TEXT NOT NULL,
            data_json TEXT NOT NULL,
            html_content TEXT,
            needs_review INTEGER DEFAULT 0,
            fetched_at TEXT DEFAULT (datetime('now')),
            UNIQUE(district, tahasil, village, khatiyan_value)
        )
    """)
    conn.commit()
    conn.close()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        # Group by village for efficient navigation
        village_khatiyans = {}
        for village_info, kh_id in missing_work:
            key = (village_info["district_code"], village_info["tahasil_code"], village_info["village_code"])
            if key not in village_khatiyans:
                village_khatiyans[key] = {
                    "info": village_info,
                    "khatiyans": []
                }
            village_khatiyans[key]["khatiyans"].append(kh_id)

        fetched = 0
        failed = 0

        for (d_code, t_code, v_code), data in village_khatiyans.items():
            info = data["info"]
            khatiyans_to_fetch = data["khatiyans"]

            log.info("Village %s (%d khatiyans to fetch)", info["village_name"], len(khatiyans_to_fetch))

            page = await browser.new_page()
            try:
                await _navigate_to_village(page, d_code, t_code, v_code)

                for kh_id in khatiyans_to_fetch:
                    saved = False
                    for attempt in range(1, KHATIYAN_RETRIES + 1):
                        try:
                            await page.wait_for_selector('select[id$="_ddlBindData"]', timeout=NAV_TIMEOUT_MS)
                            await page.locator('select[id$="_ddlBindData"]').select_option(value=kh_id)
                            await asyncio.sleep(0.15)

                            btn = page.locator('input[id$="_btnRORFront"]')
                            await btn.click()
                            await _wait_for_ajax(page, NAV_TIMEOUT_MS)
                            await page.wait_for_selector(
                                '[id$="_lblKhatiyanslNo"], [id$="_lblPlotNo"]',
                                timeout=NAV_TIMEOUT_MS,
                            )

                            html_content = await page.content()
                            ror_data = parse_ror_html(html_content, village_info=info)
                            ror_data["khatiyan_value"] = kh_id
                            ror_data["khatiyan_text"] = kh_id

                            _save_khatiyan(db_path, ror_data, html_content)
                            fetched += 1
                            saved = True

                            await _return_to_khatiyan_page(page)
                            break

                        except Exception as e:
                            log.warning(
                                "Khatiyan %s in %s attempt %d/%d: %s",
                                kh_id, info["village_name"], attempt, KHATIYAN_RETRIES, e,
                            )
                            try:
                                await _return_to_khatiyan_page(page)
                            except Exception:
                                await _navigate_to_village(page, d_code, t_code, v_code)
                            await asyncio.sleep(2 * attempt)

                    if not saved:
                        failed += 1

            except Exception as e:
                log.error("Village %s navigation failed: %s", info["village_name"], e)
                failed += len(khatiyans_to_fetch)
            finally:
                await page.close()

        await browser.close()

    log.info("Playwright fetch complete: %d fetched, %d failed", fetched, failed)


async def _wait_for_ajax(page, timeout=NAV_TIMEOUT_MS):
    await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    await page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))
    await page.wait_for_function("""
        () => {
            const prm = window.Sys && window.Sys.WebForms && window.Sys.WebForms.PageRequestManager;
            if (!prm) return true;
            try { return !prm.getInstance().get_isInAsyncPostBack(); } catch { return true; }
        }
    """, timeout=timeout)


async def _goto_start(page):
    for attempt in range(1, 6):
        try:
            await page.goto(ROR_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await _wait_for_ajax(page)
            await page.wait_for_selector('select[id$="_ddlDistrict"]', timeout=NAV_TIMEOUT_MS)
            return
        except Exception as e:
            log.warning("RoRView load retry %d/5: %s", attempt, e)
            await asyncio.sleep(2 * attempt)
    raise RuntimeError("Failed to load RoRView.aspx after 5 attempts")


async def _navigate_to_village(page, d_code: int, t_code: int, v_code: int):
    """Navigate district → tahasil → village with retries (matches type12-fast-scraper flow)."""
    last_err = None
    for attempt in range(1, 6):
        try:
            await _goto_start(page)
            await page.locator('select[id$="_ddlDistrict"]').select_option(value=str(d_code))
            await _wait_for_ajax(page)

            await page.wait_for_function(
                """(tah) => {
                    const sel = document.querySelector('select[id$="_ddlTahsil"]');
                    return sel && [...sel.options].some(o => o.value === tah);
                }""",
                str(t_code),
                timeout=NAV_TIMEOUT_MS,
            )
            await page.locator('select[id$="_ddlTahsil"]').select_option(value=str(t_code))
            await _wait_for_ajax(page)

            await page.wait_for_function(
                """(vil) => {
                    const sel = document.querySelector('select[id$="_ddlVillage"]');
                    return sel && [...sel.options].some(o => o.value === vil);
                }""",
                str(v_code),
                timeout=NAV_TIMEOUT_MS,
            )
            await page.locator('select[id$="_ddlVillage"]').select_option(value=str(v_code))
            await _wait_for_ajax(page)
            await page.wait_for_selector('select[id$="_ddlBindData"]', timeout=NAV_TIMEOUT_MS)
            return
        except Exception as e:
            last_err = e
            log.warning("Village navigation retry %d/5 (D%d T%d V%d): %s", attempt, d_code, t_code, v_code, e)
            await asyncio.sleep(3 * attempt)
    raise RuntimeError(f"Village navigation failed: {last_err}")


async def _return_to_khatiyan_page(page):
    """Click the 'Khatiyan Page' button to go back."""
    clicked = await page.evaluate("""
        () => {
            const labels = ["Khatiyan Page", "Khatiyan", "ଖତିୟାନ"];
            const controls = [...document.querySelectorAll("input,button,a")];
            const control = controls.find(el => {
                const text = (el.value || "") + " " + (el.textContent || "");
                return labels.some(label => text.includes(label));
            });
            if (!control) return false;
            control.click();
            return true;
        }
    """)
    if clicked:
        await _wait_for_ajax(page)
        await page.wait_for_selector('select[id$="_ddlBindData"]', timeout=NAV_TIMEOUT_MS)
    else:
        await page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await _wait_for_ajax(page)


def _save_khatiyan(db_path: Path, ror_data: Dict, html_content: str):
    """Save a single khatiyan to the district DB."""
    conn = sqlite3.connect(str(db_path))
    district = ror_data.get("district", "")
    tahasil = ror_data.get("tehsil", "")
    village = ror_data.get("mouja", "")
    kh_value = ror_data.get("khatiyan_value", "")
    kh_text = ror_data.get("khatiyan_text", "")
    data_json = json.dumps(ror_data, ensure_ascii=False)

    conn.execute("""
        INSERT OR REPLACE INTO khatiyans
        (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, html_content, needs_review, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
    """, (district, tahasil, village, kh_value, kh_text, data_json, html_content))
    conn.commit()
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def print_report(report: Dict):
    """Print a readable verification report."""
    print(f"\n{'='*60}")
    print(f"  VERIFICATION REPORT: {report['district_name']} (D{report['district_code']})")
    print(f"{'='*60}")
    print(f"  Website total villages: {report['total_villages_on_website']}")
    print(f"  Website total khatiyans: {report['total_khatiyans_on_website']}")
    print(f"  Complete villages: {report['complete_villages']}")
    print(f"  Missing villages: {len(report['missing_villages'])}")
    print(f"  Incomplete villages: {len(report['incomplete_villages'])}")
    print(f"  Total missing khatiyans: {report['total_missing_khatiyans']}")

    completeness = (
        (report['complete_villages'] / report['total_villages_on_website'] * 100)
        if report['total_villages_on_website'] else 0
    )
    print(f"  Completeness: {completeness:.1f}%")

    if report['missing_villages']:
        print(f"\n  Missing villages (top 20):")
        for v in report['missing_villages'][:20]:
            print(f"    {v['tahasil_name']} / {v['village_name']} ({v['khatiyan_count']} khatiyans)")

    if report['incomplete_villages']:
        print(f"\n  Incomplete villages (top 20):")
        for v in report['incomplete_villages'][:20]:
            missing_count = len(v.get('missing_khatiyans', []))
            print(f"    {v['tahasil_name']} / {v['village_name']}: missing {missing_count}/{v['khatiyan_count']}")

    print(f"\n{'='*60}\n")


async def main():
    parser = argparse.ArgumentParser(description="Verify district completeness against live website")
    parser.add_argument("--district", type=int, required=True, help="District code to verify")
    parser.add_argument("--data-dir", default="bhulekh_data", help="Data directory with district DBs")
    parser.add_argument("--fetch-missing", action="store_true", help="Auto-fetch missing khatiyans via Playwright")
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode")
    parser.add_argument("--concurrency", type=int, default=20, help="SOAP API concurrency")
    parser.add_argument("--report-file", help="Save report JSON to file")
    args = parser.parse_args()

    log.info("=== District Verification Pipeline ===")
    log.info("District: %d | Data dir: %s | Fetch missing: %s",
             args.district, args.data_dir, args.fetch_missing)

    # Step 1: Enumerate from website
    log.info("Step 1: Enumerating from live website...")
    t0 = time.time()
    website_data = await enumerate_from_website(args.district, args.concurrency)
    log.info("  Enumeration took %.1fs", time.time() - t0)
    log.info("  Found %d villages, %d total khatiyans",
             len(website_data["villages"]),
             sum(v["khatiyan_count"] for v in website_data["villages"]))

    # Step 2: Load scraped data from district DB
    log.info("Step 2: Loading scraped data...")
    db_path = find_district_db(args.data_dir, args.district, website_data["district_name"])
    if db_path:
        log.info("  Found DB: %s", db_path)
        scraped_data = get_scraped_khatiyans(db_path)
        log.info("  Scraped: %d villages, %d total khatiyans",
                 len(scraped_data), sum(len(v) for v in scraped_data.values()))
    else:
        log.warning("  No district DB found! All data will be reported as missing.")
        scraped_data = {}

    # Step 3: Compare
    log.info("Step 3: Comparing...")
    report = compare_data(website_data, scraped_data)
    print_report(report)

    # Save report
    if args.report_file:
        report_serializable = report.copy()
        for v in report_serializable.get("missing_villages", []):
            v["khatiyan_ids"] = list(v.get("khatiyan_ids", []))
        for v in report_serializable.get("incomplete_villages", []):
            v["khatiyan_ids"] = list(v.get("khatiyan_ids", []))
        with open(args.report_file, "w", encoding="utf-8") as f:
            json.dump(report_serializable, f, ensure_ascii=False, indent=2)
        log.info("Report saved to: %s", args.report_file)

    # Step 4: Fetch missing (if requested)
    if args.fetch_missing and report["total_missing_khatiyans"] > 0:
        log.info("Step 4: Fetching %d missing khatiyans via Playwright...", report["total_missing_khatiyans"])
        await fetch_missing_with_playwright(report, args.data_dir, headless=args.headless)
    elif args.fetch_missing:
        log.info("Step 4: No missing khatiyans — district is complete!")


if __name__ == "__main__":
    asyncio.run(main())

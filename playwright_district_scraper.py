#!/usr/bin/env python3
"""
District-focused multi-worker Playwright scraper (type12-fast-scraper.js flow).

When a district is handed off to the verifier, this script takes ALL workers on
the instance and completes villages at 100% khatiyan coverage (no 80% shortcut).

Usage:
    python playwright_district_scraper.py --district 21 --workers 20 \\
        --db work_queue.db --data-dir bhulekh_data --headless
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from http_scraper import _resolve_khatiyan_value, soap_get_khatiyans
from ror_parser import parse_ror_html
from storage import create_storage
from work_queue import (
    claim_village,
    complete_village,
    checkpoint_village,
    fail_village,
    heartbeat,
)

START_URL = "http://bhulekh.ori.nic.in/RoRView.aspx"
SETUP_TIMEOUT_MS = 120_000
AJAX_TIMEOUT_MS = 45_000
ROR_TIMEOUT_MS = 60_000
RETRIES = 4
BETWEEN_KHATIYAN_S = 0.25
FAIL_PAUSE_S = 2.5
LONG_FAIL_PAUSE_S = 20.0
WORKER_START_GAP_S = 1.2
SKIP_KHATIYAN_RE = re.compile(r"'D'\s*$", re.I)
VILLAGE_TIMEOUT_S = 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("playwright_district.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

_shutdown = asyncio.Event()


def _normalize_kh(val: str) -> str:
    return (val or "").strip()


def _install_signal_handlers() -> None:
    def _handler(*_):
        log.info("Shutdown requested")
        _shutdown.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ── Playwright navigation (ported from type12-fast-scraper.js) ────────────────

async def wait_for_ajax_idle(page, timeout_ms: int = AJAX_TIMEOUT_MS) -> None:
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 12_000))
    except Exception:
        pass
    try:
        await page.wait_for_function(
            """() => {
                const prm = window.Sys && window.Sys.WebForms && window.Sys.WebForms.PageRequestManager;
                if (!prm) return true;
                try { return !prm.getInstance().get_isInAsyncPostBack(); } catch { return true; }
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        pass


async def goto_start(page, timeout_ms: int = SETUP_TIMEOUT_MS) -> None:
    last_err = None
    for attempt in range(1, 6):
        try:
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await wait_for_ajax_idle(page, timeout_ms)
            await page.wait_for_selector('select[id$="_ddlDistrict"]', timeout=timeout_ms)
            return
        except Exception as e:
            last_err = e
            log.warning("RoRView load retry %d/5: %s", attempt, e)
            await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"Failed to load RoRView.aspx: {last_err}")


async def setup_location(page, d_val: str, t_val: str, v_val: str) -> None:
    for attempt in range(1, 6):
        await goto_start(page)
        visible = await page.locator('select[id$="_ddlDistrict"]').count() > 0
        if visible:
            break
        log.warning("setup retry %d/5: district dropdown missing", attempt)
        await asyncio.sleep(5 * attempt)

    await page.locator('select[id$="_ddlDistrict"]').select_option(value=d_val)
    await wait_for_ajax_idle(page)

    await page.wait_for_function(
        """(value) => {
            const select = document.querySelector('select[id$="_ddlTahsil"]');
            return select && [...select.options].some(o => o.value === value);
        }""",
        t_val,
        timeout=AJAX_TIMEOUT_MS,
    )
    await page.locator('select[id$="_ddlTahsil"]').select_option(value=t_val)
    await wait_for_ajax_idle(page)

    await page.wait_for_function(
        """(value) => {
            const select = document.querySelector('select[id$="_ddlVillage"]');
            return select && [...select.options].some(o => o.value === value);
        }""",
        v_val,
        timeout=AJAX_TIMEOUT_MS,
    )
    await page.locator('select[id$="_ddlVillage"]').select_option(value=v_val)
    await wait_for_khatiyan_page(page, long_timeout=True)


async def wait_for_khatiyan_page(page, long_timeout: bool = False) -> None:
    timeout = SETUP_TIMEOUT_MS if long_timeout else 30_000
    await wait_for_ajax_idle(page, timeout)
    await page.wait_for_selector('select[id$="_ddlBindData"]', timeout=timeout)


async def get_khatiyan_options(page) -> List[Dict[str, str]]:
    return await page.locator('select[id$="_ddlBindData"] option').evaluate_all(
        """options => options
            .map((option, index) => ({
                index,
                value: option.value,
                text: (option.textContent || '').replace(/\\s+/g, ' ').trim(),
            }))
            .filter(o => o.value && !/select/i.test(o.value) && !/select/i.test(o.text))
        """
    )


async def get_dropdown_map(page) -> Dict[str, str]:
    options = await get_khatiyan_options(page)
    mapping: Dict[str, str] = {}
    for opt in options:
        text = opt.get("text", "")
        value = opt.get("value", "")
        if text and value:
            mapping[text] = value
            mapping[text.strip()] = value
    return mapping


async def click_view_ror(page) -> None:
    btn = page.locator(
        'input[id$="_btnRORFront"], input[name$="$btnRORFront"], '
        'input[type="submit"][value*="View"]'
    ).first
    await btn.click()
    await wait_for_ajax_idle(page, ROR_TIMEOUT_MS)
    await page.wait_for_selector(
        '[id$="_lblKhatiyanslNo"], [id$="_lblPlotNo"]',
        timeout=ROR_TIMEOUT_MS,
    )


async def return_khatiyan_page(page) -> None:
    clicked = await page.evaluate(
        """() => {
            const labels = ['Khatiyan Page', 'Khatiyan', 'ଖତିୟାନ'];
            const controls = [...document.querySelectorAll('input,button,a')];
            const control = controls.find(el => {
                const text = `${el.value || ''} ${el.textContent || ''}`.replace(/\\s+/g, ' ').trim();
                return labels.some(label => text.includes(label));
            });
            if (!control) return false;
            control.click();
            return true;
        }"""
    )
    if clicked:
        await wait_for_khatiyan_page(page)
        return
    try:
        await page.go_back(wait_until="domcontentloaded", timeout=30_000)
    except Exception:
        pass
    await wait_for_khatiyan_page(page)


async def scrape_one_khatiyan(
    page,
    text: str,
    value: str,
    village_info: dict,
    storage,
    recover,
) -> bool:
    for attempt in range(1, RETRIES + 1):
        try:
            await wait_for_khatiyan_page(page)
            await page.locator('select[id$="_ddlBindData"]').select_option(value=value)
            await asyncio.sleep(0.08)
            await click_view_ror(page)

            html = await page.content()
            ror_data = parse_ror_html(html, village_info=village_info)
            ror_data["district"] = village_info["district_name"]
            ror_data["tahasil"] = village_info["tahasil_name"]
            ror_data["village"] = village_info["village_name"]
            ror_data["khatiyan_value"] = value
            ror_data["khatiyan_text"] = text

            storage.append_khatiyan(ror_data, html_content=html)
            storage.increment_layout_stat(ror_data.get("ror_type", "type1"))
            await return_khatiyan_page(page)
            return True

        except Exception as e:
            log.warning(
                "Khatiyan %r attempt %d/%d in %s: %s",
                text, attempt, RETRIES, village_info["village_name"], e,
            )
            pause = LONG_FAIL_PAUSE_S if attempt == RETRIES else FAIL_PAUSE_S
            await asyncio.sleep(pause)
            try:
                await return_khatiyan_page(page)
            except Exception:
                if recover:
                    await recover(page)

    return False


@dataclass
class SharedWork:
    cursor: int = 0
    done: int = 0
    failed: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


async def worker_loop(
    worker_id: int,
    page,
    pending: List[Tuple[str, str]],
    village_info: dict,
    storage,
    shared: SharedWork,
    recover,
    on_saved,
) -> None:
    while not _shutdown.is_set():
        async with shared.lock:
            if shared.cursor >= len(pending):
                break
            text, value = pending[shared.cursor]
            shared.cursor += 1

        if SKIP_KHATIYAN_RE.search(text):
            log.info("W%d skip D-khatiyan %s", worker_id, text)
            async with shared.lock:
                shared.done += 1
            continue

        ok = await scrape_one_khatiyan(page, text, value, village_info, storage, recover)
        async with shared.lock:
            if ok:
                shared.done += 1
                on_saved(text, value)
            else:
                shared.failed += 1

        await asyncio.sleep(BETWEEN_KHATIYAN_S)


async def prepare_worker(
    worker_id: int,
    browser,
    village_info: dict,
) -> Optional[Any]:
    page = await browser.new_page()
    d_val = str(village_info["district_code"])
    t_val = str(village_info["tahasil_code"])
    v_val = str(village_info["village_code"])

    async def recover(p):
        await setup_location(p, d_val, t_val, v_val)

    try:
        log.info("W%d: setting up %s", worker_id, village_info["village_name"])
        await setup_location(page, d_val, t_val, v_val)
        return page, recover
    except Exception as e:
        log.error("W%d setup failed: %s", worker_id, e)
        await page.close()
        return None


async def build_pending_khatiyans(
    soap_client: httpx.AsyncClient,
    village_info: dict,
    dropdown_map: Dict[str, str],
    existing: set,
) -> List[Tuple[str, str]]:
    rows = await soap_get_khatiyans(
        soap_client,
        village_info["district_code"],
        village_info["tahasil_code"],
        village_info["village_code"],
    )
    pending: List[Tuple[str, str]] = []
    existing_norm = {_normalize_kh(x) for x in existing}

    if rows:
        for row in rows:
            text = row.get("okhata_no") or row.get("code") or row.get("oname") or ""
            if not text:
                continue
            value = _resolve_khatiyan_value(text, dropdown_map)
            if _normalize_kh(value) in existing_norm or _normalize_kh(text) in existing_norm:
                continue
            pending.append((text.strip(), value))
    else:
        for text, value in dropdown_map.items():
            if text == _normalize_kh(value):
                continue
            if _normalize_kh(value) in existing_norm:
                continue
            pending.append((text, value))

    return pending


async def process_village(
    village_info: dict,
    workers: int,
    storage,
    soap_client: httpx.AsyncClient,
    db_path: str,
    headless: bool,
) -> bool:
    from playwright.async_api import async_playwright

    v_id = village_info["id"]
    vil_name = village_info["village_name"]
    t_name = village_info["tahasil_name"]

    existing = storage.get_existing_khatiyans(t_name, vil_name)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        # Bootstrap worker 1 to read dropdown map
        prep = await prepare_worker(1, browser, village_info)
        if not prep:
            return False
        page1, recover1 = prep
        dropdown_map = await get_dropdown_map(page1)

        pending = await build_pending_khatiyans(
            soap_client, village_info, dropdown_map, existing,
        )
        soap_rows = await soap_get_khatiyans(
            soap_client,
            village_info["district_code"],
            village_info["tahasil_code"],
            village_info["village_code"],
        )
        expected_count = len(soap_rows) if soap_rows else len(dropdown_map) // 2

        if not pending:
            if len(existing) >= expected_count:
                log.info("Village %s already 100%% (%d khatiyans)", vil_name, len(existing))
                complete_village(db_path, v_id, len(existing))
                await page1.close()
                await browser.close()
                return True
            log.warning("Village %s: 0 pending but only %d/%d in DB", vil_name, len(existing), expected_count)

        log.info(
            "Village %s: %d pending / %d expected (%d already in DB), %d workers",
            vil_name, len(pending), expected_count, len(existing), workers,
        )

        pages: List[Any] = [page1]
        recovers = [recover1]

        for wid in range(2, workers + 1):
            await asyncio.sleep(WORKER_START_GAP_S)
            prep = await prepare_worker(wid, browser, village_info)
            if prep:
                pages.append(prep[0])
                recovers.append(prep[1])

        if not pages:
            await browser.close()
            return False

        shared = SharedWork()
        saved_count = [len(existing)]

        def on_saved(text: str, value: str) -> None:
            saved_count[0] += 1
            checkpoint_village(db_path, v_id, saved_count[0], text)

        async def hb_loop() -> None:
            while True:
                await asyncio.sleep(120)
                heartbeat(db_path, v_id)

        hb_task = asyncio.create_task(hb_loop())

        try:
            await asyncio.wait_for(
                asyncio.gather(*[
                    worker_loop(
                        i + 1, pages[i], pending, village_info, storage,
                        shared, recovers[i], on_saved,
                    )
                    for i in range(len(pages))
                ]),
                timeout=VILLAGE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error("Village %s timed out after %ds", vil_name, VILLAGE_TIMEOUT_S)
            fail_village(db_path, v_id, f"Playwright timeout after {VILLAGE_TIMEOUT_S}s")
            hb_task.cancel()
            for pg in pages:
                await pg.close()
            await browser.close()
            return False
        finally:
            hb_task.cancel()

        for pg in pages:
            await pg.close()
        await browser.close()

    final_existing = storage.get_existing_khatiyans(t_name, vil_name)
    if expected_count > 0 and len(final_existing) >= expected_count:
        complete_village(db_path, v_id, len(final_existing))
        log.info(
            "Village %s COMPLETE: %d/%d khatiyans (failed this run: %d)",
            vil_name, len(final_existing), expected_count, shared.failed,
        )
        return True

    fail_village(
        db_path, v_id,
        f"Incomplete: {len(final_existing)}/{expected_count} khatiyans after Playwright pass",
    )
    log.error(
        "Village %s INCOMPLETE: %d/%d khatiyans, %d fetch failures",
        vil_name, len(final_existing), expected_count, shared.failed,
    )
    return False


async def run_district(
    district_code: int,
    workers: int,
    db_path: str,
    data_dir: str,
    headless: bool,
) -> None:
    villages_done = 0
    villages_failed = 0
    started = time.time()

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as soap_client:
        while not _shutdown.is_set():
            village_info = claim_village(
                db_path,
                worker_id=f"pw-d{district_code}",
                district_codes=[district_code],
            )
            if village_info is None:
                log.info("No pending villages left for D%d", district_code)
                break

            storage = create_storage(data_dir, village_info["district_name"])
            ok = await process_village(
                village_info, workers, storage, soap_client, db_path, headless,
            )
            storage.close()

            if ok:
                villages_done += 1
            else:
                villages_failed += 1

            elapsed = max(time.time() - started, 1)
            log.info(
                "District D%d progress: %d done, %d failed this session | %.1f villages/hr",
                district_code, villages_done, villages_failed,
                villages_done / elapsed * 3600,
            )

    log.info(
        "Playwright district scrape finished D%d: %d villages ok, %d failed",
        district_code, villages_done, villages_failed,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-worker Playwright district scraper")
    parser.add_argument("--district", type=int, required=True)
    parser.add_argument("--workers", type=int, default=15)
    parser.add_argument("--db", default="work_queue.db")
    parser.add_argument("--data-dir", default="bhulekh_data")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    _install_signal_handlers()
    log.info(
        "Starting Playwright district scraper: D%d, %d workers, headless=%s",
        args.district, args.workers, args.headless,
    )

    asyncio.run(run_district(
        args.district, args.workers, args.db, args.data_dir, args.headless,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())

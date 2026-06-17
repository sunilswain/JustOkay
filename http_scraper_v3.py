#!/usr/bin/env python3
"""
http_scraper_v3.py — Fast HTTP scraper using scraper_v3 progress system.

Design goals vs http_scraper.py
================================
1. NO work_queue.db
   - Villages:  villages.json  (same source scraper_v3 uses)
   - Khatiyans: SOAP KhatiyanUnicode + dropdown fallback (already in http_scraper)
   - Resume:    storage.get_existing_khatiyans()  (already per-district SQLite)
   - Progress:  progress/.lock / .done files  (same as Playwright fleet)
   => HTTP workers and Playwright workers can run on the same district
      simultaneously without any conflicts.

2. Fast ViewState extraction
   - Full BeautifulSoup parse on every POST was expensive
   - Regex extraction of hidden fields only: ~10x faster on large HTML pages
   - We always override district/tahasil/village/khatiyan ourselves so we
     only need the ASP.NET hidden state tokens from the form.

3. Immediate session recovery (no 67-second backoff spiral)
   - Old: SessionError → retry 4 times with 2+5+15+45s backoff, cold nav each time
   - New: SessionError → re-navigate to village once (~8-12s), resume from
     exact khatiyan VALUE (not position).  After 3 failures on same village → give up.

4. Skip redundant GET after 302
   - POST View RoR → 302 → httpx auto-follows to SRoRFront_Uni.aspx
   - If that body already contains RoR content (gvfront/gvRorBack), skip the
     extra GET that old code did unconditionally.

5. Per-worker semaphore, lower delay
   - Each worker owns its semaphore (no cross-worker starvation)
   - Default request delay: 0.05s (down from 0.15s)

6. Batched DB checkpoints
   - Write to SQLite every _CHECKPOINT_INTERVAL khatiyans (default 10)
     instead of every single khatiyan.

7. 98% completion threshold (matches Playwright fleet)

Usage
-----
  python http_scraper_v3.py --districts 3 --workers 20
  python http_scraper_v3.py --districts 18 --workers 15 --request-delay 0.05
  python http_scraper_v3.py --districts 3 18 --workers 30 --data-dir bhulekh_data
"""
from __future__ import annotations

import argparse
import asyncio
import html as html_lib
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

# ── shared imports from existing modules ──────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))

from http_scraper import (
    BASE_URL,
    BTN_KHATIYAN_PAGE,
    BTN_ROR_FRONT,
    DDL_DISTRICT,
    DDL_KHATIYAN,
    DDL_TAHASIL,
    DDL_VILLAGE,
    KHATIYAN_VALUE_WIDTH,
    RADIO_SEARCH,
    ROR_PATH,
    SOAP_BASE,
    SessionError,
    BhulekhHttpSession,
    _extraction_has_issues,
    _find_back_page_button,
    _get_submit_button_value,
    _is_error_page,
    _is_ror_content_page,
    _is_ror_view_page,
    _is_session_error,
    _is_timeout_page,
    _pad_khatiyan_value,
    _post_url_from_html,
    _resolve_khatiyan_value,
    parse_khatiyan_dropdown,
    soap_get_khatiyans,
)
from ror_parser import parse_ror_html
from scraper_v3 import (
    DEFAULT_DATA_DIR,
    PROGRESS_DIR,
    SKIP_DISTRICTS,
    SKIP_TAHASILS,
    _get_village_resume_from_storage,
    is_village_done,
    load_priority_config,
    mark_village_done,
    mark_village_failed,
    sort_villages_for_worker,
    try_claim_village,
    village_khatiyan_complete,
)
from storage import create_storage

# ── tunables ──────────────────────────────────────────────────────────────────
_REQUEST_DELAY    = 0.0     # seconds between HTTP calls per worker (server is fast)
_VILLAGE_TIMEOUT  = 1800    # min seconds per village (scaled up by size in _worker)
_TIMEOUT_PER_KH   = 2.5     # extra seconds of village budget per expected khatiyan
_SESSION_RESETS   = 3       # max re-navigations on session errors per village
_CONSEC_ERRORS    = 5       # consecutive khatiyan failures before abandoning village
_WRITE_BATCH      = 50      # flush khatiyans to SQLite every N (incremental, crash-safe)
_CHECKPOINT_EVERY = 10      # write SQLite checkpoint every N khatiyans
_MAX_VILLAGE_RETRIES = 2    # after N retries with 0 new khatiyans, accept gap and mark done
_CONNECT_TIMEOUT  = 15.0    # httpx connect timeout (fail fast, not 30s)
_READ_TIMEOUT     = 45.0    # httpx read timeout (down from 60s)
_SOAP_TIMEOUT     = 20.0    # SOAP KhatiyanUnicode timeout

log = logging.getLogger("http_scraper_v3")

_shutdown = asyncio.Event()


def _setup_logging(log_file: str = "http_scraper_v3.log") -> None:
    fmt = "%(asctime)s %(levelname)s %(message)s"

    # Silence httpx/httpcore request-level noise — we only want our own messages
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Use our own named logger so basicConfig from http_scraper (called at import
    # time) doesn't swallow our messages into the wrong file.
    v3_log = logging.getLogger("http_scraper_v3")
    v3_log.setLevel(logging.INFO)
    v3_log.propagate = False  # don't bubble up to root (= http_scraper.log)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    v3_log.addHandler(fh)
    v3_log.addHandler(sh)


# ── fast ViewState extraction (regex, not full BeautifulSoup) ─────────────────

_HIDDEN_RE = re.compile(
    r'<input(?=[^>]*\stype=["\']hidden["\'])[^>]*/?>',
    re.I | re.S,
)
_ATTR_NAME_RE  = re.compile(r'\sname=["\']([^"\']+)["\']',  re.I)
_ATTR_VALUE_RE = re.compile(r'\svalue=["\']([^"\']*)["\']', re.I)
_RADIO_RE = re.compile(
    r'<input(?=[^>]*\stype=["\']radio["\'])(?=[^>]*\schecked)[^>]*/?>',
    re.I | re.S,
)


def _fast_form_fields(html: str) -> Dict[str, str]:
    """
    Extract hidden inputs and checked radios via regex.

    ~10x faster than full BeautifulSoup parse for large ASP.NET pages.
    We only need hidden fields (ViewState/EventValidation/etc.) and the
    checked radio; all visible select values are explicitly overridden in
    our postback calls anyway.
    """
    fields: Dict[str, str] = {}

    for m in _HIDDEN_RE.finditer(html):
        tag = m.group(0)
        nm = _ATTR_NAME_RE.search(tag)
        if not nm:
            continue
        vl = _ATTR_VALUE_RE.search(tag)
        raw = vl.group(1) if vl else ""
        fields[nm.group(1)] = html_lib.unescape(raw)

    for m in _RADIO_RE.finditer(html):
        tag = m.group(0)
        nm = _ATTR_NAME_RE.search(tag)
        vl = _ATTR_VALUE_RE.search(tag)
        if nm and nm.group(1) not in fields:
            fields[nm.group(1)] = vl.group(1) if vl else ""

    return fields


# ── fast HTTP session subclass ────────────────────────────────────────────────

class FastHttpSession(BhulekhHttpSession):
    """
    Subclasses BhulekhHttpSession with:
    - Regex-based form field extraction (avoid full BS4 on every POST)
    - Per-worker semaphore (no cross-worker starvation)
    - Lower timeouts
    """

    def __init__(self, base_url: str, request_delay: float = _REQUEST_DELAY):
        sem = asyncio.Semaphore(4)  # per-worker: max 4 inflight (sequential anyway)
        super().__init__(base_url, sem, request_delay)

    async def __aenter__(self) -> "FastHttpSession":
        limits = httpx.Limits(max_connections=6, max_keepalive_connections=3)
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
            limits=limits,
        )
        return self

    async def fast_postback(
        self,
        html: str,
        event_target: str = "",
        extra: Optional[Dict[str, str]] = None,
        button: Optional[str] = None,
        button_value: str = "",
    ) -> str:
        """postback() using regex field extraction instead of BeautifulSoup."""
        fields = _fast_form_fields(html)
        fields["__EVENTTARGET"]   = event_target
        fields["__EVENTARGUMENT"] = ""
        if extra:
            fields.update(extra)
        # Remove other btn* fields (keep only the one we're clicking)
        for key in list(fields):
            if "btn" in key.lower() and key != button:
                del fields[key]
        if button is not None:
            fields[button] = button_value

        post_url = _post_url_from_html(html, self.base_url, ROR_PATH)
        r = await self._request("POST", post_url, data=fields)
        if _is_session_error(r.text, r.status_code):
            raise SessionError(
                f"HTTP {r.status_code} from postback target={event_target!r}"
            )
        return r.text

    async def navigate_to_village_fast(
        self,
        district_code: str,
        tahasil_code: str,
        village_code: str,
    ) -> Tuple[str, Dict[str, str]]:
        """
        GET + 3 POSTs → village page with khatiyan dropdown.
        Uses fast_postback (regex VS extraction).
        Returns (village_html, khatiyan_dropdown_map).
        """
        for attempt in range(3):
            try:
                html = await self.reset_session()
                if not _is_ror_view_page(html):
                    await asyncio.sleep(1)
                    continue

                html = await self.fast_postback(
                    html,
                    event_target=DDL_DISTRICT,
                    extra={DDL_DISTRICT: district_code},
                )
                if _is_error_page(html) or _is_timeout_page(html):
                    await asyncio.sleep(2)
                    continue

                html = await self.fast_postback(
                    html,
                    event_target=DDL_TAHASIL,
                    extra={DDL_DISTRICT: district_code, DDL_TAHASIL: tahasil_code},
                )
                if _is_error_page(html) or _is_timeout_page(html):
                    await asyncio.sleep(2)
                    continue

                html = await self.fast_postback(
                    html,
                    event_target=DDL_VILLAGE,
                    extra={
                        DDL_DISTRICT: district_code,
                        DDL_TAHASIL:  tahasil_code,
                        DDL_VILLAGE:  village_code,
                        RADIO_SEARCH: "Khatiyan",
                    },
                )
                kh_map = parse_khatiyan_dropdown(html)
                if kh_map:
                    return html, kh_map
                log.warning(
                    "navigate_to_village_fast: empty khatiyan dropdown attempt %d/3", attempt + 1
                )
                await asyncio.sleep(2)
            except SessionError:
                await asyncio.sleep(2)
        # Last resort: return whatever we have
        return html, parse_khatiyan_dropdown(html)

    async def fetch_ror_fast(
        self,
        district_code: str,
        tahasil_code: str,
        village_code: str,
        khatiyan_value: str,
        dropdown_map: Dict[str, str],
        village_html: str,
    ) -> Tuple[str, str]:
        """
        Fetch RoR for one khatiyan.  Returns (ror_html, next_village_html).

        Optimisations:
        - Uses fast_postback (regex VS extraction)
        - Skips extra GET if POST+302 body already has RoR content
        - Back-page button only when 0 plots (same as before)
        - Returns next_village_html="" on back-nav failure (caller re-navigates)
        """
        if not _is_ror_view_page(village_html):
            raise SessionError("village_html is stale — caller should re-navigate")

        kh_val = khatiyan_value
        if kh_val not in dropdown_map.values():
            kh_val = _resolve_khatiyan_value(khatiyan_value.strip(), dropdown_map)

        ror_btn_val = _get_submit_button_value(village_html, BTN_ROR_FRONT, "View RoR")

        html = await self.fast_postback(
            village_html,
            event_target="",
            extra={
                DDL_DISTRICT: district_code,
                DDL_TAHASIL:  tahasil_code,
                DDL_VILLAGE:  village_code,
                DDL_KHATIYAN: kh_val,
                RADIO_SEARCH: "Khatiyan",
            },
            button=BTN_ROR_FRONT,
            button_value=ror_btn_val,
        )

        if _is_timeout_page(html):
            raise TimeoutError("Bhulekh timeout page after View RoR")

        # Optimisation: only do the extra GET if the POST/redirect body is NOT
        # already an RoR content page.  httpx auto-follows 302, so most of the
        # time the body IS the SRoR page already — the old code did the GET
        # unconditionally, wasting one round-trip per khatiyan.
        if not _is_ror_content_page(html):
            for path in ("/SRoRFront_Uni.aspx", "/SRoRFront.aspx"):
                try:
                    r = await self._request("GET", f"{self.base_url}{path}")
                    if _is_ror_content_page(r.text):
                        html = r.text
                        break
                except Exception:
                    pass

        # Back-page POST only when no plots in front page
        data = parse_ror_html(html)
        if not data.get("plots"):
            back_btn = _find_back_page_button(html)
            if back_btn:
                btn_name, btn_val = back_btn
                try:
                    html_back = await self.fast_postback(
                        html,
                        extra={
                            DDL_DISTRICT: district_code,
                            DDL_TAHASIL:  tahasil_code,
                            DDL_VILLAGE:  village_code,
                            DDL_KHATIYAN: kh_val,
                            RADIO_SEARCH: "Khatiyan",
                        },
                        button=btn_name,
                        button_value=btn_val,
                    )
                    back_data = parse_ror_html(html_back)
                    if len(back_data.get("plots", [])) > len(data.get("plots", [])):
                        html = html_back
                except Exception:
                    pass

        if not _is_ror_content_page(html):
            raise SessionError("RoR content not found after View RoR")

        # Back-nav to village page for the NEXT khatiyan
        try:
            next_html = await self.fast_postback(html, button=BTN_KHATIYAN_PAGE)
            if not _is_ror_view_page(next_html):
                r = await self._request("GET", self.ror_url)
                next_html = r.text if _is_ror_view_page(r.text) else ""
        except Exception:
            next_html = ""

        return html, next_html


# ── village scraping logic ────────────────────────────────────────────────────

def _flush_checkpoint(
    storage,
    d_val: str, d_name: str,
    t_val: str, t_name: str,
    v_val: str, v_name: str,
    buffer: List[Tuple[str, str, int]],
) -> None:
    """Write a checkpoint after processing a batch of khatiyans."""
    if not buffer:
        return
    last_val, last_text, count = buffer[-1]
    try:
        storage.set_checkpoint(
            d_val, d_name,
            t_val, t_name,
            v_val, v_name,
            last_val, last_text,
            count,
        )
    except Exception as e:
        log.debug("Checkpoint write failed: %s", e)


async def scrape_village(
    session: FastHttpSession,
    soap_client: httpx.AsyncClient,
    village: Dict[str, Any],
    storage,
    progress_dir: str,
    worker_id: str,
) -> Tuple[int, int]:
    """
    Scrape one village via HTTP.  Returns (saved_this_run, total_in_db).

    Recovery strategy
    -----------------
    On SessionError: immediately re-navigate (no backoff), resume from
    exact khatiyan VALUE.  Up to _SESSION_RESETS re-navigations before
    abandoning the village.
    """
    d = str(village["district_code"])
    t = str(village["tahasil_code"])
    v = str(village["village_code"])
    d_name = village["district_name"]
    t_name = village["tahasil_name"]
    v_name = village["village_name"]
    expected = village.get("khatiyan_count", 0) or 0

    # ── what's already in DB ──────────────────────────────────────────────────
    try:
        existing: Set[str] = storage.get_existing_khatiyans(t_name, v_name)
    except Exception:
        existing = set()
    already_done = len(existing)

    # ── navigate to village ───────────────────────────────────────────────────
    try:
        village_html, dropdown_map = await session.navigate_to_village_fast(d, t, v)
    except Exception as e:
        log.error("Worker %s: cannot navigate to %s: %s", worker_id, v_name, e)
        return 0, already_done

    # ── build khatiyan list ───────────────────────────────────────────────────
    # SOAP is one fast GET and gives ground-truth khatiyan numbers.
    # Dropdown fallback is used when SOAP returns nothing.
    soap_rows = await soap_get_khatiyans(
        soap_client, village["district_code"], village["tahasil_code"], village["village_code"]
    )
    if soap_rows:
        khatiyans: List[Tuple[str, str]] = []
        for row in soap_rows:
            text = (row.get("okhata_no") or row.get("code") or row.get("oname") or "").strip()
            if not text:
                continue
            val = _resolve_khatiyan_value(text, dropdown_map)
            khatiyans.append((text, val))
        if not khatiyans:
            # SOAP returned rows but none had usable text — use dropdown
            khatiyans = [
                (txt, val)
                for txt, val in dropdown_map.items()
                if txt != val.strip()
            ]
    else:
        log.warning(
            "Worker %s: SOAP returned no khatiyans for %s — using dropdown (%d opts)",
            worker_id, v_name, len(dropdown_map),
        )
        khatiyans = [
            (txt, val)
            for txt, val in dropdown_map.items()
            if txt != val.strip()
        ]

    if not khatiyans:
        log.info("Worker %s: %s has no khatiyans to scrape", worker_id, v_name)
        return 0, already_done

    # ── filter to what's not yet in DB ───────────────────────────────────────
    # get_existing_khatiyans returns 30-char padded khatiyan_value strings.
    # Normalise both sides to stripped text so "20" matches "20   " in DB.
    existing_stripped = {v.strip() for v in existing}
    to_scrape = [
        (txt, val)
        for txt, val in khatiyans
        if val.strip() not in existing_stripped and txt.strip() not in existing_stripped
    ]

    if not to_scrape:
        log.info(
            "Worker %s: %s — all %d khatiyans already in DB",
            worker_id, v_name, already_done,
        )
        return 0, already_done

    log.info(
        "Worker %s: scraping %s — %d khatiyans to fetch (%d already in DB, %d expected)",
        worker_id, v_name, len(to_scrape), already_done, expected,
    )

    # ── main khatiyan loop ────────────────────────────────────────────────────
    saved = 0
    consecutive_errors = 0
    session_resets = 0
    current_village_html = village_html
    checkpoint_buffer: List[Tuple[str, str, int]] = []
    write_buffer: List[Tuple[dict, any]] = []

    def _flush() -> None:
        # Persist accumulated khatiyans + checkpoint mid-village so a timeout or
        # crash never discards completed work (resume reads them back from DB).
        nonlocal write_buffer, checkpoint_buffer
        if write_buffer:
            try:
                storage.append_khatiyans_batch(write_buffer)
            except Exception as e:
                log.warning("Worker %s: batch storage error for %s: %s", worker_id, v_name, e)
            write_buffer = []
        if checkpoint_buffer:
            _flush_checkpoint(storage, d, d_name, t, t_name, v, v_name, checkpoint_buffer)
            checkpoint_buffer = []

    for khatiyan_text, khatiyan_value in to_scrape:
        if _shutdown.is_set():
            break

        try:
            ror_html, next_village_html = await session.fetch_ror_fast(
                d, t, v, khatiyan_value, dropdown_map, current_village_html,
            )
            current_village_html = next_village_html or ""

        except SessionError as e:
            log.warning(
                "Worker %s: session error at %s/%s — re-navigating (reset %d/%d): %s",
                worker_id, v_name, khatiyan_text, session_resets + 1, _SESSION_RESETS, e,
            )
            session_resets += 1
            if session_resets > _SESSION_RESETS:
                log.error("Worker %s: too many session resets on %s — giving up", worker_id, v_name)
                break
            try:
                current_village_html, new_map = await session.navigate_to_village_fast(d, t, v)
                if new_map:
                    dropdown_map = new_map
                # Retry this exact khatiyan with fresh session
                ror_html, nxt = await session.fetch_ror_fast(
                    d, t, v, khatiyan_value, dropdown_map, current_village_html,
                )
                current_village_html = nxt or ""
            except Exception as e2:
                log.warning("Worker %s: retry after re-nav also failed for %s: %s", worker_id, khatiyan_text, e2)
                consecutive_errors += 1
                if consecutive_errors >= _CONSEC_ERRORS:
                    log.error("Worker %s: %d consecutive errors on %s — stopping", worker_id, _CONSEC_ERRORS, v_name)
                    break
                continue

        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            log.warning("Worker %s: timeout on %s/%s — clearing session: %s", worker_id, v_name, khatiyan_text, e)
            current_village_html = ""
            consecutive_errors += 1
            if consecutive_errors >= _CONSEC_ERRORS:
                break
            continue

        except Exception as e:
            log.warning("Worker %s: error on %s/%s: %s", worker_id, v_name, khatiyan_text, e)
            current_village_html = ""
            consecutive_errors += 1
            if consecutive_errors >= _CONSEC_ERRORS:
                break
            continue

        # ── parse and accumulate (batch write at end of village) ─────────────
        ror_data = parse_ror_html(ror_html)
        ror_data.update({
            "district":       d_name,
            "tahasil":        t_name,
            "village":        v_name,
            "khatiyan_value": khatiyan_value,
            "khatiyan_text":  khatiyan_text,
        })

        write_buffer.append((ror_data, None))
        saved += 1
        consecutive_errors = 0
        checkpoint_buffer.append((khatiyan_value, khatiyan_text, already_done + saved))

        # Incremental flush: never hold more than _WRITE_BATCH in memory so a
        # village timeout/cancel loses at most _WRITE_BATCH-1 khatiyans, not all.
        if len(write_buffer) >= _WRITE_BATCH:
            _flush()

    # ── final flush of any remaining khatiyans + checkpoint ───────────────────
    _flush()

    return saved, already_done + saved


# ── worker process ────────────────────────────────────────────────────────────

async def _worker(
    worker_id: str,
    villages: List[Dict[str, Any]],
    district_codes: Set[int],
    progress_dir: str,
    data_dir: str,
    base_url: str,
    request_delay: float,
    work_dir: str,
) -> None:
    my_villages = [
        v for v in villages
        if v["district_code"] in district_codes
        and v["district_code"] not in SKIP_DISTRICTS
        and (v["district_code"], v["tahasil_code"]) not in SKIP_TAHASILS
    ]

    soap_client = httpx.AsyncClient(
        timeout=httpx.Timeout(_SOAP_TIMEOUT),
        follow_redirects=True,
    )

    async with FastHttpSession(base_url, request_delay=request_delay) as session:
        villages_done = 0
        total_kh = 0
        t_start = time.time()
        retry_queue: List[Dict[str, Any]] = []
        retry_counts: Dict[tuple, int] = {}

        while not _shutdown.is_set():
            ordered = sort_villages_for_worker(my_villages, progress_dir, work_dir)
            # Stickiness: retry small-gap villages first (same logic as scraper_v3)
            seen_retry = {
                (v["district_code"], v["tahasil_code"], v["village_code"])
                for v in retry_queue
            }
            ordered = retry_queue + [
                v for v in ordered
                if (v["district_code"], v["tahasil_code"], v["village_code"]) not in seen_retry
            ]
            retry_queue = []

            claimed_this_pass = False
            for village in ordered:
                if _shutdown.is_set():
                    break

                d_code = village["district_code"]
                t_code = village["tahasil_code"]
                v_code = village["village_code"]
                v_name = village["village_name"]
                expected = village.get("khatiyan_count", 0) or 0

                if is_village_done(progress_dir, d_code, t_code, v_code):
                    continue
                if not try_claim_village(progress_dir, d_code, t_code, v_code, worker_id):
                    continue

                claimed_this_pass = True

                storage = create_storage(
                    data_dir, village["district_name"],
                    backend="sqlite", district_code=d_code,
                )

                # Village already complete in DB — mark done without re-scraping
                try:
                    existing = storage.get_existing_khatiyans(
                        village["tahasil_name"], village["village_name"]
                    )
                    kh_done = len(existing)
                except Exception:
                    kh_done = 0

                if village_khatiyan_complete(kh_done, expected):
                    log.info(
                        "Worker %s: %s already %d/%d in DB — marking done",
                        worker_id, v_name, kh_done, expected,
                    )
                    mark_village_done(progress_dir, d_code, t_code, v_code, kh_done)
                    villages_done += 1
                    continue

                log.info(
                    "Worker %s: claiming %s (D%d/T%d) | expected %d | in_db %d",
                    worker_id, v_name, d_code, t_code, expected, kh_done,
                )

                # Scale timeout by village size — incremental writes mean a long
                # village keeps committing progress, so a generous budget is safe.
                village_timeout = max(_VILLAGE_TIMEOUT, int(expected * _TIMEOUT_PER_KH))
                try:
                    saved, total_now = await asyncio.wait_for(
                        scrape_village(
                            session, soap_client, village, storage,
                            progress_dir, worker_id,
                        ),
                        timeout=village_timeout,
                    )
                except asyncio.TimeoutError:
                    log.error("Worker %s: village %s timed out (%ds)", worker_id, v_name, village_timeout)
                    mark_village_failed(progress_dir, d_code, t_code, v_code, "timeout")
                    continue
                except Exception as e:
                    log.error("Worker %s: village %s failed: %s", worker_id, v_name, str(e)[:200])
                    mark_village_failed(progress_dir, d_code, t_code, v_code, str(e)[:200])
                    continue

                complete = village_khatiyan_complete(total_now, expected)
                gap = max(0, expected - total_now)
                if complete:
                    mark_village_done(progress_dir, d_code, t_code, v_code, total_now)
                    villages_done += 1
                    total_kh += total_now
                    elapsed = time.time() - t_start
                    speed = total_kh / (elapsed / 60) if elapsed > 60 else 0
                    log.info(
                        "Worker %s: done %s | %d kh | %.0f kh/min | total %d",
                        worker_id, v_name, total_now, speed, total_kh,
                    )
                else:
                    pct = total_now / expected * 100 if expected else 0
                    # Track retries: if we made no progress this run, count it
                    v_key = (d_code, t_code, v_code)
                    if saved == 0:
                        retry_counts[v_key] = retry_counts.get(v_key, 0) + 1
                    else:
                        retry_counts[v_key] = 0

                    if retry_counts.get(v_key, 0) >= _MAX_VILLAGE_RETRIES:
                        log.warning(
                            "Worker %s: %s stuck at %d/%d (%.0f%%) after %d retries — accepting gap, marking done",
                            worker_id, v_name, total_now, expected, pct, _MAX_VILLAGE_RETRIES,
                        )
                        mark_village_done(progress_dir, d_code, t_code, v_code, total_now)
                        villages_done += 1
                        total_kh += total_now
                    else:
                        log.warning(
                            "Worker %s: %s only %d/%d kh (%.0f%%) — retry later (attempt %d/%d)",
                            worker_id, v_name, total_now, expected, pct,
                            retry_counts.get(v_key, 0), _MAX_VILLAGE_RETRIES,
                        )
                        mark_village_failed(progress_dir, d_code, t_code, v_code, "underscraped")
                        if gap <= 20:
                            retry_queue.append(village)

            if not claimed_this_pass:
                log.info("Worker %s: no villages left to claim — done", worker_id)
                break

        await soap_client.aclose()
        elapsed = time.time() - t_start
        log.info(
            "Worker %s: finished. %d villages, %d khatiyans in %.0f min",
            worker_id, villages_done, total_kh, elapsed / 60,
        )


# ── main entry point ──────────────────────────────────────────────────────────

def _load_villages(villages_file: str) -> List[Dict[str, Any]]:
    import json
    path = Path(villages_file)
    if not path.exists():
        raise FileNotFoundError(
            f"villages.json not found at {path}. "
            "Run soap_enumerator.py first or copy villages.json to the working directory."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(description="Fast HTTP bulk scraper (no work_queue.db)")
    parser.add_argument("--districts",   nargs="+", type=int, required=True,
                        help="District codes to scrape (e.g. 3 18)")
    parser.add_argument("--workers",     type=int, default=20,
                        help="Number of concurrent async workers (default 20)")
    parser.add_argument("--request-delay", type=float, default=_REQUEST_DELAY,
                        help=f"Seconds between HTTP calls per worker (default {_REQUEST_DELAY})")
    parser.add_argument("--data-dir",    default=str(DEFAULT_DATA_DIR),
                        help="SQLite database directory")
    parser.add_argument("--progress-dir", default=PROGRESS_DIR,
                        help="Progress .lock/.done directory")
    parser.add_argument("--villages-file", default="villages.json",
                        help="Path to villages.json")
    parser.add_argument("--base-url",    default=BASE_URL,
                        help="Bhulekh base URL")
    parser.add_argument("--log-file",    default="http_scraper_v3.log")
    args = parser.parse_args()

    _setup_logging(args.log_file)

    villages = _load_villages(args.villages_file)
    district_codes = set(args.districts)
    log.info(
        "Starting HTTP scraper v3: districts=%s workers=%d delay=%.3fs",
        sorted(district_codes), args.workers, args.request_delay,
    )
    log.info("Villages in file: %d | filtered to districts: %d",
             len(villages),
             sum(1 for v in villages if v["district_code"] in district_codes))

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except (NotImplementedError, RuntimeError):
            pass

    async def _run_all() -> None:
        tasks = []
        for i in range(args.workers):
            wid = f"http-v3-w{i}"
            task = asyncio.create_task(
                _worker(
                    wid,
                    villages,
                    district_codes,
                    args.progress_dir,
                    args.data_dir,
                    args.base_url,
                    args.request_delay,
                    ".",
                )
            )
            tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)

    loop.run_until_complete(_run_all())
    log.info("All workers finished.")


if __name__ == "__main__":
    main()

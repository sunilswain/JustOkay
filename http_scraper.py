#!/usr/bin/env python3
"""
HTTP-based Bhulekh RoR scraper — no browser required.

Uses ASP.NET postbacks on RoRView.aspx plus SOAP KhatiyanUnicode for
enumeration.  Multiple asyncio workers claim villages from work_queue.db,
scrape every khatiyan via pure HTTP, and append results to storage.py.

Usage:
  python http_scraper.py --workers 5 --districts 3 14
  python http_scraper.py --workers 8 --db work_queue.db --data-dir bhulekh_data
"""

from __future__ import annotations

from urllib.parse import urljoin
import argparse
import asyncio
import logging
import os
import re
import signal
import socket
import sys
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from ror_parser import is_form20_body, parse_ror_html
from storage import DEFAULT_DATA_DIR, DEFAULT_STORAGE_BACKEND, create_storage
from work_queue import (
    DEFAULT_QUEUE_PATH,
    checkpoint_village,
    claim_village,
    complete_village,
    fail_village,
    get_stats,
    heartbeat,
    make_queue,
    reclaim_stuck_villages,
    reset_errors_for_districts,
)

# ── Site constants ────────────────────────────────────────────────────────────

BASE_URL = "http://bhulekh.ori.nic.in"
ROR_PATH = "/RoRView.aspx"
SOAP_BASE = f"{BASE_URL}/BhulekhService.asmx"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DDL_DISTRICT = "ctl00$ContentPlaceHolder1$ddlDistrict"
DDL_TAHASIL = "ctl00$ContentPlaceHolder1$ddlTahsil"
DDL_VILLAGE = "ctl00$ContentPlaceHolder1$ddlVillage"
DDL_KHATIYAN = "ctl00$ContentPlaceHolder1$ddlBindData"
RADIO_SEARCH = "ctl00$ContentPlaceHolder1$rbtnRORSearchtype"
BTN_ROR_FRONT = "ctl00$ContentPlaceHolder1$btnRORFront"
BTN_KHATIYAN_PAGE = "btnKhatiyan"

KHATIYAN_VALUE_WIDTH = 30

# ── Tunables ──────────────────────────────────────────────────────────────────

_VILLAGE_TIMEOUT = 1200          # 20 min per village
_KHATIYAN_RETRIES = 4
_KHATIYAN_BACKOFF = [2, 5, 15, 45]
_SITE_BACKOFF = [5, 10, 20, 40, 60]
_DEFAULT_REQUEST_DELAY = 0.15    # seconds between HTTP calls per worker
_DEFAULT_MAX_INFLIGHT = 20       # global concurrent HTTP requests
_COMPLETION_MIN_FRACTION = 0.8   # min fraction of expected khatiyans before marking done (override via CLI)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("http_scraper.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

_shutdown = asyncio.Event()


# ── HTML helpers ──────────────────────────────────────────────────────────────


def _post_url_from_html(html: str, base_url: str, default_path: str) -> str:
    """Resolve the POST target from the page's <form action=...>."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return f"{base_url.rstrip('/')}{default_path}"
    action = (form.get("action") or default_path).strip()
    if action.startswith("http://") or action.startswith("https://"):
        return action
    if action.startswith("./"):
        action = action[2:]
    base = base_url.rstrip("/") + "/"
    return urljoin(base, action)


def _is_ror_view_page(html: str) -> bool:
    return "ddlBindData" in html or "ddlDistrict" in html


def _is_error_page(html: str) -> bool:
    return "BhulekhError" in html or "Invalid postback" in html or "Server Error" in html


def _is_ror_content_page(html: str) -> bool:
    if any(marker in html for marker in ("gvfront", "gvRorFront", "gvRorBack", "SRoRFront_Uni")):
        return True
    if "lblBhuswami" in html or "lblRaiyat" in html or "gvRorSettBack" in html:
        return True
    return is_form20_body(BeautifulSoup(html, "html.parser").get_text())


def extract_all_form_fields(html: str) -> Dict[str, str]:
    """Extract hidden fields, select values, and checked radios from ASP.NET form."""
    soup = BeautifulSoup(html, "html.parser")
    fields: Dict[str, str] = {}

    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name", "")
        if name:
            fields[name] = inp.get("value", "")

    for sel in soup.find_all("select"):
        name = sel.get("name", "")
        if not name:
            continue
        opts = sel.find_all("option")
        selected = sel.find("option", selected=True) or (opts[0] if opts else None)
        fields[name] = selected.get("value", "") if selected else ""

    for radio in soup.find_all("input", {"type": "radio", "checked": True}):
        name = radio.get("name", "")
        if name and name not in fields:
            fields[name] = radio.get("value", "")

    return fields


def _strip_button_fields(fields: Dict[str, str], keep: Optional[str] = None) -> None:
    for key in list(fields):
        if "btn" in key.lower() and key != keep:
            del fields[key]


def parse_khatiyan_dropdown(html: str) -> Dict[str, str]:
    """Map khatiyan display text → padded ddlBindData value."""
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", {"name": DDL_KHATIYAN}) or soup.find(
        "select", id=re.compile(r"ddlBindData", re.I)
    )
    if not sel:
        return {}

    mapping: Dict[str, str] = {}
    for opt in sel.find_all("option"):
        value = opt.get("value", "")
        text = opt.get_text(strip=True)
        if not text or value in ("", "Select Khatiyan"):
            continue
        mapping[text] = value
        mapping[value.strip()] = value
    return mapping


def _is_session_error(html: str, status_code: int) -> bool:
    if status_code >= 500:
        return True
    lower = html.lower()
    # Avoid matching generic JS "setTimeout" / "timeout" in scripts
    error_markers = (
        "invalid viewstate",
        "viewstate is invalid",
        "session expired",
        "server error in '/' application",
        "runtime error",
        "service unavailable",
    )
    if any(m in lower for m in error_markers):
        return True
    # Bhulekh-specific timeout error page (not generic JS)
    if "the request timed out" in lower or "operation timed out" in lower:
        return True
    if re.search(r"<title>\s*error", lower):
        return True
    return False


def _is_timeout_page(html: str) -> bool:
    lower = html.lower()
    return (
        "the request timed out" in lower
        or "operation timed out" in lower
        or "server timeout" in lower
    )


def _get_submit_button_value(html: str, button_name: str, default: str = "") -> str:
    """Read the value attribute for a named submit button from the current form."""
    soup = BeautifulSoup(html, "html.parser")
    btn = soup.find("input", {"name": button_name})
    if btn is None:
        btn = soup.find("input", id=re.compile(re.escape(button_name.split("$")[-1]), re.I))
    if btn is not None:
        return (btn.get("value") or default).strip()
    return default


async def _fetch_ror_display_html(session: "BhulekhHttpSession", html: str) -> str:
    """GET RoR display pages if POST response did not include plot tables."""
    if _is_ror_content_page(html):
        return html
    for path in ("/SRoRFront_Uni.aspx", "/SRoRFront.aspx"):
        try:
            r = await session._request("GET", f"{session.base_url}{path}")
            if _is_ror_content_page(r.text):
                log.debug("Loaded RoR content via GET %s", path)
                return r.text
        except Exception as exc:
            log.debug("GET %s failed: %s", path, exc)
    return html


def _find_back_page_button(html: str) -> Optional[Tuple[str, str]]:
    """Return (name, value) for a back-page submit button if present."""
    soup = BeautifulSoup(html, "html.parser")
    for btn in soup.find_all("input", {"type": "submit"}):
        name = btn.get("name", "")
        value = btn.get("value", "")
        combined = f"{name} {value}".lower()
        if "back" in combined and "front" not in combined:
            return name, value
    for btn in soup.find_all("input", {"type": "submit"}):
        name = btn.get("name", "")
        if name and "btnrorback" in name.lower():
            return name, btn.get("value", "")
    return None


def _pad_khatiyan_value(text: str) -> str:
    return text.ljust(KHATIYAN_VALUE_WIDTH)


def _resolve_khatiyan_value(text: str, dropdown_map: Dict[str, str]) -> str:
    if text in dropdown_map:
        return dropdown_map[text]
    stripped = text.strip()
    if stripped in dropdown_map:
        return dropdown_map[stripped]
    return _pad_khatiyan_value(text)


def _extraction_has_issues(ror_data: Dict[str, Any]) -> bool:
    plots = ror_data.get("plots") or []
    if not plots:
        return True
    for plot in plots:
        if not (plot.get("plot_no") or "").strip():
            return True
        acre = (plot.get("acre") or "").strip()
        decimil = (plot.get("decimil") or "").strip()
        hector = (plot.get("hector") or "").strip()
        kisam = plot.get("kisam") or ""
        if not acre and not decimil and not hector and "ଉପଲବ୍ଧ ନାହିଁ" not in kisam:
            return True
    return False


# ── SOAP ──────────────────────────────────────────────────────────────────────


def _parse_soap_rows(xml_text: str) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    rows: List[Dict[str, str]] = []
    for table in root.iter("Table"):
        row = {child.tag: (child.text or "").strip() for child in table}
        if row:
            rows.append(row)
    return rows


async def soap_get_khatiyans(
    client: httpx.AsyncClient,
    district_code: int,
    tahasil_code: int,
    village_code: int,
    retries: int = 4,
) -> List[Dict[str, str]]:
    url = f"{SOAP_BASE}/KhatiyanUnicode"
    params = {"dCode": district_code, "tCode": tahasil_code, "vCode": village_code}
    for attempt in range(retries):
        try:
            r = await client.get(url, params=params, timeout=60)
            if r.status_code == 500 and "ConnectionString" in r.text:
                return []
            r.raise_for_status()
            return _parse_soap_rows(r.text)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            wait = 2 ** attempt
            log.warning(
                "KhatiyanUnicode d=%s t=%s v=%s attempt %d/%d: %s — retry in %ds",
                district_code, tahasil_code, village_code,
                attempt + 1, retries, exc, wait,
            )
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "KhatiyanUnicode HTTP %d for d=%s t=%s v=%s",
                exc.response.status_code, district_code, tahasil_code, village_code,
            )
            return []
    return []


# ── HTTP session / postback client ───────────────────────────────────────────


class BhulekhHttpSession:
    """Async HTTP client for one worker — maintains cookies across a village."""

    def __init__(
        self,
        base_url: str,
        request_sem: asyncio.Semaphore,
        request_delay: float,
    ):
        self.base_url = base_url.rstrip("/")
        self.ror_url = f"{self.base_url}{ROR_PATH}"
        self.request_sem = request_sem
        self.request_delay = request_delay
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "BhulekhHttpSession":
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=30.0),
            limits=limits,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert self._client is not None
        async with self.request_sem:
            if self.request_delay > 0:
                await asyncio.sleep(self.request_delay)
            if method == "GET":
                return await self._client.get(url, **kwargs)
            return await self._client.post(url, **kwargs)

    async def reset_session(self) -> str:
        """GET / to establish a fresh ASP.NET session."""
        assert self._client is not None
        if self._client.cookies:
            self._client.cookies.clear()
        r = await self._request("GET", self.base_url)
        r.raise_for_status()
        html = r.text
        if "ddlDistrict" not in html:
            r = await self._request("GET", self.ror_url)
            r.raise_for_status()
            html = r.text
        if "ddlDistrict" not in html:
            raise RuntimeError("RoR search page did not load (no ddlDistrict)")
        return html

    async def postback(
        self,
        html: str,
        event_target: str = "",
        extra: Optional[Dict[str, str]] = None,
        button: Optional[str] = None,
        button_value: str = "",
    ) -> str:
        fields = extract_all_form_fields(html)
        fields["__EVENTTARGET"] = event_target
        fields["__EVENTARGUMENT"] = ""
        if extra:
            fields.update(extra)
        _strip_button_fields(fields, keep=button)
        if button is not None:
            fields[button] = button_value

        post_url = _post_url_from_html(html, self.base_url, ROR_PATH)
        r = await self._request("POST", post_url, data=fields)
        if _is_session_error(r.text, r.status_code):
            raise SessionError(f"HTTP {r.status_code} from postback target={event_target!r}")
        return r.text

    async def return_to_village_page(self, ror_html: str) -> str:
        """Click Khatiyan Page on SRoRFront_Uni.aspx to return to village selection."""
        html = await self.postback(ror_html, button=BTN_KHATIYAN_PAGE)
        if _is_ror_view_page(html):
            return html
        # Fallback: re-open search page (session cookies may still be valid)
        r = await self._request("GET", self.ror_url)
        r.raise_for_status()
        return r.text

    async def navigate_to_village(
        self,
        district_code: str,
        tahasil_code: str,
        village_code: str,
    ) -> Tuple[str, Dict[str, str]]:
        """Full GET + district/tahasil/village postbacks. Returns HTML + khatiyan map."""
        for nav_attempt in range(3):
            html = await self.reset_session()
            if not _is_ror_view_page(html):
                await asyncio.sleep(1)
                continue

            html = await self.postback(
                html,
                event_target=DDL_DISTRICT,
                extra={DDL_DISTRICT: district_code},
            )
            if _is_error_page(html) or _is_timeout_page(html):
                log.warning("District postback got error/timeout page, retry %d", nav_attempt + 1)
                await asyncio.sleep(2)
                continue

            html = await self.postback(
                html,
                event_target=DDL_TAHASIL,
                extra={
                    DDL_DISTRICT: district_code,
                    DDL_TAHASIL: tahasil_code,
                },
            )
            if _is_error_page(html) or _is_timeout_page(html):
                log.warning("Tahasil postback got error/timeout page, retry %d", nav_attempt + 1)
                await asyncio.sleep(2)
                continue

            html = await self.postback(
                html,
                event_target=DDL_VILLAGE,
                extra={
                    DDL_DISTRICT: district_code,
                    DDL_TAHASIL: tahasil_code,
                    DDL_VILLAGE: village_code,
                    RADIO_SEARCH: "Khatiyan",
                },
            )
            if _is_error_page(html) or _is_timeout_page(html):
                log.warning("Village postback got error/timeout page, retry %d", nav_attempt + 1)
                await asyncio.sleep(2)
                continue

            kh_map = parse_khatiyan_dropdown(html)
            if kh_map:
                return html, kh_map

            log.warning("Khatiyan dropdown empty after navigation, retry %d", nav_attempt + 1)
            await asyncio.sleep(2)

        kh_map = parse_khatiyan_dropdown(html)
        return html, kh_map

    async def fetch_ror_for_khatiyan(
        self,
        district_code: str,
        tahasil_code: str,
        village_code: str,
        khatiyan_value: str,
        dropdown_map: Dict[str, str],
        village_html: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        View RoR for one khatiyan. Returns (ror_html, village_html_for_next_khatiyan).

        When village_html is provided, reuses the existing session (2 POSTs per khatiyan).
        Otherwise establishes a fresh session and navigates (5 POSTs).
        """
        if village_html is None or not _is_ror_view_page(village_html):
            village_html = await self.reset_session()
            village_html = await self.postback(
                village_html,
                event_target=DDL_DISTRICT,
                extra={DDL_DISTRICT: district_code},
            )
            village_html = await self.postback(
                village_html,
                event_target=DDL_TAHASIL,
                extra={DDL_DISTRICT: district_code, DDL_TAHASIL: tahasil_code},
            )
            village_html = await self.postback(
                village_html,
                event_target=DDL_VILLAGE,
                extra={
                    DDL_DISTRICT: district_code,
                    DDL_TAHASIL: tahasil_code,
                    DDL_VILLAGE: village_code,
                    RADIO_SEARCH: "Khatiyan",
                },
            )

        kh_val = khatiyan_value
        if kh_val not in dropdown_map.values():
            kh_val = _resolve_khatiyan_value(khatiyan_value.strip(), dropdown_map)

        ror_btn_val = _get_submit_button_value(village_html, BTN_ROR_FRONT, "View RoR")
        html = await self.postback(
            village_html,
            event_target="",
            extra={
                DDL_DISTRICT: district_code,
                DDL_TAHASIL: tahasil_code,
                DDL_VILLAGE: village_code,
                DDL_KHATIYAN: kh_val,
                RADIO_SEARCH: "Khatiyan",
            },
            button=BTN_ROR_FRONT,
            button_value=ror_btn_val,
        )

        if _is_timeout_page(html):
            raise TimeoutError("Bhulekh timeout page after View RoR")

        html = await _fetch_ror_display_html(self, html)

        data = parse_ror_html(html)
        if not data.get("plots"):
            back_btn = _find_back_page_button(html)
            if back_btn:
                btn_name, btn_val = back_btn
                log.debug("Trying back-page button %s for khatiyan %r", btn_name, kh_val.strip())
                html_back = await self.postback(
                    html,
                    event_target="",
                    extra={
                        DDL_DISTRICT: district_code,
                        DDL_TAHASIL: tahasil_code,
                        DDL_VILLAGE: village_code,
                        DDL_KHATIYAN: kh_val,
                        RADIO_SEARCH: "Khatiyan",
                    },
                    button=btn_name,
                    button_value=btn_val,
                )
                back_data = parse_ror_html(html_back)
                if back_data.get("plots") and len(back_data.get("plots", [])) > len(data.get("plots", [])):
                    html = html_back

        if not _is_ror_content_page(html):
            raise SessionError("RoR content not found in HTTP response")

        try:
            next_village_html = await self.return_to_village_page(html)
            if not _is_ror_view_page(next_village_html):
                next_village_html = ""
        except SessionError:
            next_village_html = ""

        return html, next_village_html


class SessionError(Exception):
    """ASP.NET session / ViewState failure — retry with fresh session."""


# ── Village processing ────────────────────────────────────────────────────────


async def fetch_khatiyan_with_retry(
    session: BhulekhHttpSession,
    district_code: str,
    tahasil_code: str,
    village_code: str,
    khatiyan_text: str,
    khatiyan_value: str,
    dropdown_map: Dict[str, str],
    village_html: Optional[str] = None,
) -> Tuple[Dict[str, Any], Optional[str], str]:
    """Fetch and parse one khatiyan; returns (ror_data, html_for_review, next_village_html)."""
    last_err: Optional[Exception] = None
    current_village_html = village_html

    for attempt in range(_KHATIYAN_RETRIES):
        try:
            ror_html, next_html = await session.fetch_ror_for_khatiyan(
                district_code, tahasil_code, village_code,
                khatiyan_value, dropdown_map,
                village_html=current_village_html,
            )
            ror_data = parse_ror_html(ror_html)

            filled = sum(
                1 for k, v in ror_data.items()
                if v and v != [] and k not in ("plots", "ror_type")
            )
            plots_n = len(ror_data.get("plots") or [])
            log.debug(
                "Parsed khatiyan %r: %d fields, %d plots",
                khatiyan_text, filled, plots_n,
            )

            html_content = ror_html if _extraction_has_issues(ror_data) else None
            return ror_data, html_content, next_html

        except (SessionError, TimeoutError, httpx.TimeoutException, httpx.NetworkError) as exc:
            last_err = exc
            current_village_html = None
            wait = _KHATIYAN_BACKOFF[min(attempt, len(_KHATIYAN_BACKOFF) - 1)]
            log.warning(
                "Khatiyan %r attempt %d/%d failed: %s — retry in %ds",
                khatiyan_text, attempt + 1, _KHATIYAN_RETRIES, exc, wait,
            )
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as exc:
            last_err = exc
            current_village_html = None
            wait = _KHATIYAN_BACKOFF[min(attempt, len(_KHATIYAN_BACKOFF) - 1)]
            log.warning(
                "Khatiyan %r HTTP %d attempt %d/%d — retry in %ds",
                khatiyan_text, exc.response.status_code, attempt + 1, _KHATIYAN_RETRIES, wait,
            )
            await asyncio.sleep(wait)

    raise RuntimeError(f"Khatiyan {khatiyan_text!r} failed after {_KHATIYAN_RETRIES} attempts: {last_err}")


async def process_village(
    session: BhulekhHttpSession,
    soap_client: httpx.AsyncClient,
    village_info: dict,
    storage,
    dropdown_map: Dict[str, str],
    resume_after: Optional[str],
    already_done: int,
    on_progress,
    limit_khatiyans: Optional[int],
    village_html: Optional[str] = None,
) -> Tuple[int, bool]:
    """
    Process khatiyans in a village. Returns (saved_count, limit_reached).
    """
    d_code = village_info["district_code"]
    t_code = village_info["tahasil_code"]
    v_code = village_info["village_code"]
    d_name = village_info["district_name"]
    t_name = village_info["tahasil_name"]
    v_name = village_info["village_name"]

    d_str, t_str, v_str = str(d_code), str(t_code), str(v_code)

    # Khatiyan list via SOAP (fast); fallback to dropdown map keys
    soap_rows = await soap_get_khatiyans(soap_client, d_code, t_code, v_code)
    khatiyans: List[Tuple[str, str]] = []  # (text, value)

    if soap_rows:
        for row in soap_rows:
            text = row.get("okhata_no") or row.get("code") or row.get("oname") or ""
            if not text:
                continue
            value = _resolve_khatiyan_value(text, dropdown_map)
            if not dropdown_map and value == _pad_khatiyan_value(text):
                log.debug("Skipping khatiyan %r — dropdown_map empty, cannot resolve value", text)
                continue
            khatiyans.append((text, value))
    else:
        log.warning("SOAP returned no khatiyans for %s — using dropdown (%d opts)", v_name, len(dropdown_map))
        seen: set = set()
        for text, value in dropdown_map.items():
            if text == value.strip():
                continue  # skip reverse mapping entries
            if text in seen:
                continue
            seen.add(text)
            khatiyans.append((text, value))

    if not khatiyans:
        log.info("Village %s has no khatiyans — marking done", v_name)
        return 0, False

    # Resume: skip until past checkpoint
    start_idx = 0
    if resume_after:
        resume_stripped = resume_after.strip()
        for i, (text, value) in enumerate(khatiyans):
            if text.strip() == resume_stripped or value.strip() == resume_stripped:
                start_idx = i + 1
                log.info(
                    "Resuming village %s after khatiyan %r (skipping %d)",
                    v_name, resume_after, start_idx,
                )
                break

    # Skip khatiyans already in storage (prevents duplicates on re-runs)
    existing_kh = set()
    if hasattr(storage, "get_existing_khatiyans"):
        existing_kh = storage.get_existing_khatiyans(t_name, v_name)
        if existing_kh:
            log.info(
                "Village %s: %d khatiyans already in DB, will skip them",
                v_name, len(existing_kh),
            )

    saved = 0
    skipped_existing = 0
    failed_khatiyans = []
    limit_reached = False
    current_village_html = village_html or ""
    consecutive_errors = 0
    _MAX_CONSECUTIVE_ERRORS = 5
    _SESSION_RESETS_ALLOWED = 3
    session_resets_done = 0

    # Base count = known khatiyans already in DB (from storage, not work_queue)
    # This avoids double-counting when already_done != len(existing_kh)
    base_count = max(already_done, len(existing_kh))

    for text, value in khatiyans[start_idx:]:
        if _shutdown.is_set():
            break
        if limit_khatiyans is not None and saved >= limit_khatiyans:
            limit_reached = True
            break

        if value in existing_kh:
            skipped_existing += 1
            continue

        try:
            ror_data, html_content, next_village_html = await fetch_khatiyan_with_retry(
                session, d_str, t_str, v_str, text, value, dropdown_map,
                village_html=current_village_html if current_village_html else None,
            )
            current_village_html = next_village_html
            consecutive_errors = 0

            ror_data["district"] = d_name
            ror_data["tahasil"] = t_name
            ror_data["village"] = v_name
            ror_data["khatiyan_value"] = value
            ror_data["khatiyan_text"] = text

            storage.append_khatiyan(ror_data, html_content=html_content)
            storage.increment_layout_stat(ror_data.get("ror_type", "type1"))
            saved += 1

            total_now = base_count + saved
            storage.set_checkpoint(
                d_str, d_name, t_str, t_name, v_str, v_name,
                value, text, total_now,
            )
            on_progress(total_now, value.strip() if value else value)

            log.info(
                "Saved khatiyan %r (%d plots) | village %s | progress %d/%d",
                text, len(ror_data.get("plots") or []), v_name, total_now,
                len(khatiyans),
            )

        except (RuntimeError, Exception) as exc:
            consecutive_errors += 1
            failed_khatiyans.append(text)
            log.warning(
                "Khatiyan %r failed in village %s: %s (consecutive errors: %d)",
                text, v_name, exc, consecutive_errors,
            )

            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                if session_resets_done < _SESSION_RESETS_ALLOWED:
                    session_resets_done += 1
                    log.warning(
                        "Village %s: %d consecutive errors — resetting session (reset %d/%d)",
                        v_name, consecutive_errors, session_resets_done, _SESSION_RESETS_ALLOWED,
                    )
                    try:
                        current_village_html, dropdown_map = await session.navigate_to_village(
                            d_str, t_str, v_str
                        )
                        consecutive_errors = 0
                        log.info("Session reset successful for village %s", v_name)
                    except Exception as nav_exc:
                        log.error(
                            "Session reset FAILED for village %s: %s — aborting remaining",
                            v_name, nav_exc,
                        )
                        break
                else:
                    log.error(
                        "Village %s: exhausted %d session resets with %d consecutive errors — "
                        "aborting remaining khatiyans",
                        v_name, _SESSION_RESETS_ALLOWED, consecutive_errors,
                    )
                    break

    if failed_khatiyans:
        log.warning(
            "Village %s: %d khatiyans failed out of %d attempted (skipped %d existing)",
            v_name, len(failed_khatiyans), len(khatiyans) - start_idx - skipped_existing, skipped_existing,
        )

    return saved, limit_reached


# ── Worker loop ───────────────────────────────────────────────────────────────


async def worker_loop(
    worker_id: str,
    queue_path: str,
    queue_api_key: Optional[str],
    district_codes: Optional[List[int]],
    data_dir: str,
    storage_backend: str,
    base_url: str,
    request_sem: asyncio.Semaphore,
    request_delay: float,
    limit_khatiyans: Optional[int],
) -> None:
    _queue = make_queue(queue_path, api_key=queue_api_key)
    _is_remote = hasattr(_queue, "claim_village")

    def _claim():
        if _is_remote:
            return _queue.claim_village(worker_id=worker_id, district_codes=district_codes)
        return claim_village(queue_path, worker_id=worker_id, district_codes=district_codes)

    def _complete(vid: int, n: int) -> None:
        if _is_remote:
            _queue.complete_village(vid, n)
        else:
            complete_village(queue_path, vid, n)

    def _fail(vid: int, err: str) -> None:
        if _is_remote:
            _queue.fail_village(vid, err)
        else:
            fail_village(queue_path, vid, err)

    def _heartbeat(vid: int) -> None:
        if _is_remote:
            _queue.heartbeat(vid)
        else:
            heartbeat(queue_path, vid)

    def _checkpoint(vid: int, n: int, last_kh: str) -> None:
        if _is_remote:
            _queue.checkpoint_village(vid, n, last_kh)
        else:
            checkpoint_village(queue_path, vid, n, last_kh)

    consecutive_failures = 0
    villages_done = 0
    khatiyans_processed = 0

    async with BhulekhHttpSession(base_url, request_sem, request_delay) as session:
        soap_client = session._client
        assert soap_client is not None

        log.info("Worker %s ready (queue=%s)", worker_id, queue_path)

        while not _shutdown.is_set():
            village_info = _claim()
            if village_info is None:
                log.info("Worker %s: no pending villages — exiting", worker_id)
                break

            v_id = village_info["id"]
            vil_name = village_info["village_name"]
            d_name = village_info["district_name"]
            resume_kh = village_info.get("last_khatiyan_no")
            already_done = village_info.get("khatiyans_fetched", 0) or 0

            log.info(
                "Worker %s: village %s (%s) | tahasil %s | district %s",
                worker_id, vil_name, village_info["village_code"],
                village_info["tahasil_name"], d_name,
            )

            storage = create_storage(data_dir, d_name, backend=storage_backend)
            khatiyans_saved = 0
            village_ok = False

            async def _heartbeat_loop() -> None:
                while True:
                    await asyncio.sleep(120)
                    _heartbeat(v_id)

            async def _checkpoint_loop(last_ref: dict) -> None:
                while True:
                    await asyncio.sleep(60)
                    if last_ref.get("count", 0) > 0 and last_ref.get("last_kh"):
                        _checkpoint(v_id, last_ref["count"], last_ref["last_kh"])

            progress_ref = {"count": already_done, "last_kh": resume_kh or ""}

            def on_progress(total: int, last_kh: str) -> None:
                progress_ref["count"] = total
                progress_ref["last_kh"] = last_kh

            hb_task = asyncio.create_task(_heartbeat_loop())
            cp_task = asyncio.create_task(_checkpoint_loop(progress_ref))

            try:
                d_str = str(village_info["district_code"])
                t_str = str(village_info["tahasil_code"])
                v_str = str(village_info["village_code"])

                village_html, dropdown_map = await session.navigate_to_village(d_str, t_str, v_str)

                khatiyans_saved, limit_reached = await asyncio.wait_for(
                    process_village(
                        session=session,
                        soap_client=soap_client,
                        village_info=village_info,
                        storage=storage,
                        dropdown_map=dropdown_map,
                        resume_after=resume_kh,
                        already_done=already_done,
                        on_progress=on_progress,
                        limit_khatiyans=limit_khatiyans,
                        village_html=village_html,
                    ),
                    timeout=_VILLAGE_TIMEOUT,
                )

                # khatiyans_saved = only NEWLY fetched this run (excludes pre-existing)
                # Actual total in DB = existing + newly saved
                if hasattr(storage, 'get_existing_khatiyans'):
                    actual_in_db = len(storage.get_existing_khatiyans(
                        village_info["tahasil_name"], vil_name))
                else:
                    actual_in_db = already_done + khatiyans_saved
                kh_done = actual_in_db

                expected = village_info.get("khatiyan_count", 0) or 0
                min_required = int(expected * _COMPLETION_MIN_FRACTION) if expected > 0 else 0

                # Accept as done if: no new progress AND already close (within 5 or 98%)
                _close_enough = (
                    expected > 0
                    and kh_done > 0
                    and khatiyans_saved == 0
                    and (kh_done >= expected - 5 or kh_done >= expected * 0.98)
                )

                if kh_done == 0 and expected > 0 and not _shutdown.is_set():
                    _fail(v_id, f"0 khatiyans saved (expected {expected})")
                elif _close_enough:
                    _complete(v_id, kh_done)
                    log.info(
                        "Worker %s: accepting village %s as done (%d/%d khatiyans, "
                        "no new progress — remaining likely don't exist on server)",
                        worker_id, vil_name, kh_done, expected,
                    )
                    village_ok = True
                    villages_done += 1
                elif expected > 0 and kh_done < min_required and not _shutdown.is_set():
                    _fail(v_id, f"Only {kh_done}/{expected} khatiyans (<{int(_COMPLETION_MIN_FRACTION * 100)}% threshold, need {min_required})")
                    log.error(
                        "Worker %s: village %s has only %d/%d khatiyans (<%d%% threshold) — "
                        "FAILED, will retry. New this run: %d.",
                        worker_id, vil_name, kh_done, expected,
                        int(_COMPLETION_MIN_FRACTION * 100), khatiyans_saved,
                    )
                else:
                    _complete(v_id, kh_done)
                    log.info(
                        "Worker %s: completed village %s (%d khatiyans total, %d new this run)",
                        worker_id, vil_name, kh_done, khatiyans_saved,
                    )
                    village_ok = True
                    villages_done += 1

            except asyncio.TimeoutError:
                err = f"Village timed out after {_VILLAGE_TIMEOUT}s"
                log.error("Worker %s: %s on village %s", worker_id, err, vil_name)
                if progress_ref["count"] > already_done and progress_ref["last_kh"]:
                    _checkpoint(v_id, progress_ref["count"], progress_ref["last_kh"])
                _fail(v_id, err)

            except Exception as exc:
                log.error("Worker %s: error on village %s: %s", worker_id, vil_name, exc)
                if progress_ref["count"] > already_done and progress_ref["last_kh"]:
                    _checkpoint(v_id, progress_ref["count"], progress_ref["last_kh"])
                _fail(v_id, str(exc))

            finally:
                hb_task.cancel()
                cp_task.cancel()
                for task in (hb_task, cp_task):
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                storage.close()

            if _shutdown.is_set():
                break

            if village_ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    wait = _SITE_BACKOFF[min(consecutive_failures - 5, len(_SITE_BACKOFF) - 1)]
                    log.warning(
                        "Worker %s: %d consecutive village failures — backing off %ds",
                        worker_id, consecutive_failures, wait,
                    )
                    await asyncio.sleep(wait)

    log.info("Worker %s finished (%d villages completed)", worker_id, villages_done)


# ── Main ──────────────────────────────────────────────────────────────────────


async def run_workers(
    workers: int,
    queue_path: str,
    queue_api_key: Optional[str],
    district_codes: Optional[List[int]],
    data_dir: str,
    storage_backend: str,
    base_url: str,
    request_delay: float,
    max_inflight: int,
    limit_khatiyans: Optional[int],
) -> None:
    request_sem = asyncio.Semaphore(max_inflight)
    host = socket.gethostname()
    pid = os.getpid()

    tasks = [
        asyncio.create_task(
            worker_loop(
                worker_id=f"{host}-{pid}-w{i}",
                queue_path=queue_path,
                queue_api_key=queue_api_key,
                district_codes=district_codes,
                data_dir=data_dir,
                storage_backend=storage_backend,
                base_url=base_url,
                request_sem=request_sem,
                request_delay=request_delay,
                limit_khatiyans=limit_khatiyans,
            )
        )
        for i in range(workers)
    ]

    await asyncio.gather(*tasks)


def _install_signal_handlers() -> None:
    def _handle(signum, _frame) -> None:
        log.warning("Signal %s received — graceful shutdown requested", signum)
        _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HTTP-based Bhulekh RoR scraper (no browser). "
        "Run soap_enumerator.py first to populate work_queue.db.",
    )
    parser.add_argument(
        "--workers", type=int, default=4, metavar="N",
        help="Number of concurrent asyncio workers (default: 4)",
    )
    parser.add_argument(
        "--db", default=DEFAULT_QUEUE_PATH, metavar="PATH",
        help=f"Work queue SQLite path or remote URL (default: {DEFAULT_QUEUE_PATH})",
    )
    parser.add_argument(
        "--key", default=None,
        help="API key for remote queue server",
    )
    parser.add_argument(
        "--data-dir", default=DEFAULT_DATA_DIR, metavar="DIR",
        help="Directory for per-district storage",
    )
    parser.add_argument(
        "--storage", choices=["sqlite", "ndjson"], default=DEFAULT_STORAGE_BACKEND,
        help="Storage backend (default: sqlite)",
    )
    parser.add_argument(
        "--url", default=BASE_URL,
        help=f"Bhulekh base URL (default: {BASE_URL})",
    )
    parser.add_argument(
        "--districts", nargs="+", type=int, metavar="CODE",
        help="Only process these district codes (e.g. --districts 3 14)",
    )
    parser.add_argument(
        "--request-delay", type=float, default=_DEFAULT_REQUEST_DELAY,
        help=f"Delay seconds between HTTP calls per worker (default: {_DEFAULT_REQUEST_DELAY})",
    )
    parser.add_argument(
        "--max-inflight", type=int, default=_DEFAULT_MAX_INFLIGHT,
        help=f"Max concurrent HTTP requests across all workers (default: {_DEFAULT_MAX_INFLIGHT})",
    )
    parser.add_argument(
        "--limit-khatiyans", type=int, default=None, metavar="N",
        help="Stop each worker after N khatiyans (testing)",
    )
    parser.add_argument(
        "--reset-errors",
        action="store_true",
        help="Before starting: reset error villages to pending (in --districts if set)",
    )
    parser.add_argument(
        "--reset-stuck-zero",
        action="store_true",
        help="Also reset in_progress villages with 0 khatiyans_fetched to pending",
    )
    parser.add_argument(
        "--completion-min-fraction", type=float, default=0.8, metavar="F",
        help="Min fraction of expected khatiyans before marking village done (default: 0.8; use 1.0 for verifier)",
    )
    args = parser.parse_args()

    global _COMPLETION_MIN_FRACTION
    _COMPLETION_MIN_FRACTION = max(0.0, min(1.0, args.completion_min_fraction))

    is_remote = args.db.startswith("http://") or args.db.startswith("https://")
    if is_remote:
        try:
            headers = {"X-Api-Key": args.key} if args.key else {}
            r = httpx.get(f"{args.db.rstrip('/')}/health", headers=headers, timeout=10)
            r.raise_for_status()
        except Exception as exc:
            log.error("Cannot reach queue server %s: %s", args.db, exc)
            sys.exit(1)
    elif not os.path.exists(args.db):
        log.error("Queue file not found: %s — run soap_enumerator.py first", args.db)
        sys.exit(1)
    else:
        if args.reset_errors or args.reset_stuck_zero:
            n_err, n_stuck = reset_errors_for_districts(
                args.db,
                district_codes=args.districts,
                reset_errors=args.reset_errors,
                reset_zero_progress_in_progress=args.reset_stuck_zero,
            )
            if args.reset_errors:
                log.info("Reset %d error villages to pending", n_err)
            if args.reset_stuck_zero:
                log.info("Reset %d stuck at 0%% in_progress villages to pending", n_stuck)
        else:
            stuck = reclaim_stuck_villages(args.db)
            if stuck:
                log.info("Reclaimed %d stuck in_progress villages", stuck)

    _install_signal_handlers()

    log.info(
        "Starting %d HTTP workers | districts=%s | queue=%s | data_dir=%s | min_complete=%.0f%%",
        args.workers, args.districts or "all", args.db, args.data_dir,
        _COMPLETION_MIN_FRACTION * 100,
    )

    t0 = time.time()
    try:
        asyncio.run(
            run_workers(
                workers=args.workers,
                queue_path=args.db,
                queue_api_key=args.key,
                district_codes=args.districts,
                data_dir=args.data_dir,
                storage_backend=args.storage,
                base_url=args.url.rstrip("/"),
                request_delay=args.request_delay,
                max_inflight=args.max_inflight,
                limit_khatiyans=args.limit_khatiyans,
            )
        )
    except KeyboardInterrupt:
        log.info("Interrupted")

    elapsed = time.time() - t0
    try:
        stats = get_stats(args.db) if not is_remote else {}
        if stats:
            log.info(
                "Done in %.0fs | villages fetched khatiyans: %s",
                elapsed, stats.get("total_khatiyans_fetched"),
            )
    except Exception:
        pass


if __name__ == "__main__":
    main()

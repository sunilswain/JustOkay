"""
Bhulekh RoR Data Scraper
Automates the process of fetching RoR (Record of Rights) data from the Bhulekh website.
"""

import os
import sys

# Browser path: when exe = folder next to exe (no env). When script = AppData (shared with installers).
if getattr(sys, "frozen", False):
    # Exe: use "browsers" folder beside the exe — no dependency on LOCALAPPDATA
    _browsers_path = os.path.join(os.path.dirname(sys.executable), "browsers")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _browsers_path
else:
    _apd = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if not _apd and sys.platform == "win32":
        _apd = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    _browsers_path = os.path.join(_apd, "BhulekhScraper", "browsers") if _apd else ""
    if _apd:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _browsers_path)


def _find_chromium_executable():
    """When running as exe, find chrome.exe under _browsers_path and pass it explicitly to Playwright."""
    if not _browsers_path or not os.path.isdir(_browsers_path):
        return None
    for name in os.listdir(_browsers_path):
        if name.startswith("chromium-") and os.path.isdir(os.path.join(_browsers_path, name)):
            for sub in ("chrome-win64", "chrome-win"):
                exe = os.path.join(_browsers_path, name, sub, "chrome.exe")
                if os.path.isfile(exe):
                    return exe
    return None


import asyncio
import re
import pandas as pd
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
import logging
from typing import Optional, List, Dict
import time
import json
from pathlib import Path
import random
from datetime import date

from storage import create_storage, BhulekhStorageBase, DEFAULT_DATA_DIR
from ror_parser import parse_ror_html

# Expiry: program will refuse to run after this date (useful for .exe builds).
# Set to None to disable expiry check.
EXPIRY_DATE = date(2026, 2, 25)  # YYYY, MM, DD
VERSION = "1.0.1"

# Configure logging (UTF-8 so Odia/Unicode log messages don't fail on Windows)
def _setup_logging():
    import sys
    log_format = '%(asctime)s - %(levelname)s - %(message)s'
    handlers = [
        logging.FileHandler('bhulekh_scraper.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ]
    # Ensure console handler can emit Unicode on Windows
    if hasattr(handlers[1].stream, 'reconfigure'):
        try:
            handlers[1].stream.reconfigure(encoding='utf-8')
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format=log_format, handlers=handlers)

_setup_logging()
logger = logging.getLogger(__name__)

# ASP.NET control selectors: exact ID first (in case of dynamic IDs, use fallback).
SELECTOR_DISTRICT = 'select#ctl00_ContentPlaceHolder1_ddlDistrict, select[id*="ddlDistrict"]'
SELECTOR_TAHASIL = 'select#ctl00_ContentPlaceHolder1_ddlTahsil, select[id*="ddlTahsil"]'
SELECTOR_VILLAGE = 'select#ctl00_ContentPlaceHolder1_ddlVillage, select[id*="ddlVillage"]'
SELECTOR_KHATIYAN = 'select#ctl00_ContentPlaceHolder1_ddlBindData, select[id*="ddlBindData"]'
# RoR view page markers (Type-1/2 and common landlord/tenant fields)
SELECTOR_ROR_VIEW_READY = (
    "#gvfront_ctl02_lblMouja, #gvfront, #gvRorFront, #gvRorBack, "
    "[id*='lblBhuswami'], [id*='lblRaiyat']"
)
# Khatiyan radio can be disabled until District/Tahasil are selected
SELECTOR_RADIO_KHATIYAN = 'input#ctl00_ContentPlaceHolder1_rbtnRORSearchtype_0, input[value="Khatiyan"][name*="rbtnRORSearchtype"]'

# RoR back-page plot table IDs (Type-1 gvfront + Form-99 variants share these)
_PLOT_TABLE_IDS = ['gvRorBack', 'gvRorBack2', 'gvRorFrontBack', 'gvplotdetail']
_NON_PLOT_TABLE_IDS = frozenset({'gvfront'})

# Form-20 / survey-settlement plot tables (separate from Type-1/2 — not in _PLOT_TABLE_IDS)
_FORM20_PLOT_TABLE_IDS = [
    'gvRorSettBack', 'gvSettPlot', 'gvSettPlotDetail', 'gvPlotSettle', 'gvForm20Back',
    'gvRorBack', 'gvRorBack2', 'gvplotdetail',
]


def _is_form20_body(body_text: str) -> bool:
    """True when page body indicates Form-20 / survey-settlement RoR (not Form-99)."""
    if re.search(r'ଫର୍ମ\s*ନଂ\s*[-–]?\s*20(?:\D|$)', body_text):
        return True
    if re.search(r'Form\s*(?:No\.?\s*)?20\b', body_text, re.I):
        return True
    settlement_markers = (
        'ସମୀକ୍ଷା ସେଟଲମେଣ୍ଟ',
        'Special Survey & Settlement',
        'Survey & Settlement Act',
        'Odisha Special Survey',
    )
    if any(m in body_text for m in settlement_markers):
        if re.search(r'ଫର୍ମ\s*ନଂ', body_text) and not re.search(r'ଫର୍ମ\s*ନଂ\s*[-–]?\s*99\b', body_text):
            return True
    return False

# Single JS blob for bulk plot extraction — used by Type-1 and Type-2 paths alike.
_EXTRACT_PLOTS_JS = """
() => {
    const PLOT_TABLE_IDS = %PLOT_TABLE_IDS%;
    const NON_PLOT_TABLE_IDS = new Set(%NON_PLOT_TABLE_IDS%);

    const cellText = (el) => {
        if (!el) return '';
        return (el.innerText || el.textContent || '').trim();
    };

    const getText = (row, patterns) => {
        if (typeof patterns === 'string') patterns = [patterns];
        for (const sel of patterns) {
            const el = row.querySelector(sel);
            const val = cellText(el);
            if (val !== '') return val;
        }
        return '';
    };

    const getPlotNo = (row) => {
        // Order matters: non-chaka plot → chaka number → chaka plot name
        const selectors = [
            'a[id*="lblPlotcni"]', 'span[id*="lblPlotcni"]',
            'a[id*="lblPlotNo"]', 'span[id*="lblPlotNo"]',
            'a[id*="lblPlotci"]', 'span[id*="lblPlotci"]',
        ];
        for (const sel of selectors) {
            const val = cellText(row.querySelector(sel));
            if (val && /\\d/.test(val)) return val;
        }
        return '';
    };

    const getChaka = (row) => {
        // Layout A: lblchaka span (simple table)
        const direct = getText(row, [
            'span[id*="lblchaka"]', 'span[id*="Chaka"]', '[id*="lblchaka"]',
        ]);
        if (direct) return direct;
        // Layout B: lblPlotci link — e.g. "767/1089 ଗ୍ରାମତଳ" or "2 ପିତାବଳୀ"
        const plotci = getText(row, ['a[id*="lblPlotci"]', 'span[id*="lblPlotci"]']);
        if (!plotci) return '';
        const m = plotci.match(/^[\\d\\/]+\\s+(.+)$/);
        return m ? m[1].trim() : plotci;
    };

    const countPlotMarkers = (root) =>
        root.querySelectorAll('[id*="lblPlotNo"], [id*="lblPlotcni"], [id*="lblPlotci"]').length;

    const findPlotTable = () => {
        for (const tableId of PLOT_TABLE_IDS) {
            const table = document.getElementById(tableId);
            if (table) return { table, tableId };
        }
        let best = null;
        let bestScore = 0;
        for (const table of document.querySelectorAll('table[id]')) {
            const id = table.id || '';
            if (NON_PLOT_TABLE_IDS.has(id)) continue;
            if (!/Ror|plot|Back/i.test(id)) continue;
            const score = countPlotMarkers(table);
            if (score > bestScore) {
                bestScore = score;
                best = { table, tableId: id || 'scored-fallback' };
            }
        }
        return best;
    };

    const extractFromRows = (rows) => {
        const results = [];
        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            const plotNo = getPlotNo(row);
            if (!plotNo) continue;

            results.push({
                plot_no: plotNo,
                chaka: getChaka(row),
                land_type: getText(row, [
                    'span[id*="lblCNItype"]', 'span[id*="lbllType"]',
                    'span[id*="LandType"]', '[id*="CNItype"]',
                ]),
                kisam: getText(row, ['span[id*="lblKisama"]', 'span[id*="Kisam"]', '[id*="Kisam"]']),
                n_occu: getText(row, ['span[id*="lbln_occu"]', '[id*="n_occu"]']),
                e_occu: getText(row, ['span[id*="lble_occu"]', '[id*="e_occu"]']),
                s_occu: getText(row, ['span[id*="lbls_occu"]', '[id*="s_occu"]']),
                w_occu: getText(row, ['span[id*="lblw_occu"]', '[id*="w_occu"]']),
                acre: getText(row, ['span[id*="lblAcre"]', 'span[id*="Acre"]', '[id*="Acre"]']),
                decimil: getText(row, ['span[id*="lblDecimil"]', 'span[id*="Decimil"]', '[id*="Decimil"]']),
                hector: getText(row, ['span[id*="lblHector"]', 'span[id*="Hector"]', '[id*="Hector"]']),
                remarks: getText(row, ['span[id*="lblPlotRemarks"]', 'span[id*="Remarks"]', '[id*="Remark"]']),
            });
        }
        return results;
    };

    const found = findPlotTable();
    if (!found) return { plots: [], tableId: '', rowCount: 0 };

    const rows = found.table.querySelectorAll('tr');
    const plots = extractFromRows(rows);
    return { plots, tableId: found.tableId, rowCount: rows.length };
}
""".replace('%PLOT_TABLE_IDS%', json.dumps(_PLOT_TABLE_IDS)).replace(
    '%NON_PLOT_TABLE_IDS%', json.dumps(list(_NON_PLOT_TABLE_IDS))
)

# Form-20 plot extraction — extends standard selectors with settlement table IDs
# and a plain-table fallback when rows lack lblPlotNo spans.
_EXTRACT_PLOTS_FORM20_JS = """
() => {
    const PLOT_TABLE_IDS = %FORM20_PLOT_TABLE_IDS%;
    const NON_PLOT_TABLE_IDS = new Set(%NON_PLOT_TABLE_IDS%);

    const cellText = (el) => {
        if (!el) return '';
        return (el.innerText || el.textContent || '').trim();
    };

    const getText = (row, patterns) => {
        if (typeof patterns === 'string') patterns = [patterns];
        for (const sel of patterns) {
            const el = row.querySelector(sel);
            const val = cellText(el);
            if (val !== '') return val;
        }
        return '';
    };

    const getPlotNo = (row) => {
        const selectors = [
            'a[id*="lblPlotcni"]', 'span[id*="lblPlotcni"]',
            'a[id*="lblPlotNo"]', 'span[id*="lblPlotNo"]',
            'a[id*="lblPlotci"]', 'span[id*="lblPlotci"]',
            'span[id*="lblSlNo"]', 'span[id*="lblPlot"]',
        ];
        for (const sel of selectors) {
            const val = cellText(row.querySelector(sel));
            if (val && /\\d/.test(val)) return val;
        }
        const cells = row.querySelectorAll('td, th');
        if (cells.length >= 2) {
            const first = cellText(cells[0]);
            if (/^\\d+$/.test(first)) return first;
        }
        return '';
    };

    const getChaka = (row) => {
        const direct = getText(row, [
            'span[id*="lblchaka"]', 'span[id*="Chaka"]', '[id*="lblchaka"]',
        ]);
        if (direct) return direct;
        const plotci = getText(row, ['a[id*="lblPlotci"]', 'span[id*="lblPlotci"]']);
        if (!plotci) return '';
        const m = plotci.match(/^[\\d\\/]+\\s+(.+)$/);
        return m ? m[1].trim() : plotci;
    };

    const countPlotMarkers = (root) =>
        root.querySelectorAll(
            '[id*="lblPlotNo"], [id*="lblPlotcni"], [id*="lblPlotci"], [id*="lblAcre"]'
        ).length;

    const findPlotTable = () => {
        for (const tableId of PLOT_TABLE_IDS) {
            const table = document.getElementById(tableId);
            if (table) return { table, tableId };
        }
        let best = null;
        let bestScore = 0;
        for (const table of document.querySelectorAll('table[id]')) {
            const id = table.id || '';
            if (NON_PLOT_TABLE_IDS.has(id)) continue;
            if (!/Ror|plot|Sett|Back|Form20/i.test(id)) continue;
            const score = countPlotMarkers(table);
            if (score > bestScore) {
                bestScore = score;
                best = { table, tableId: id || 'scored-fallback' };
            }
        }
        if (best) return best;
        const content = document.getElementById('ContentArea');
        if (content) {
            for (const table of content.querySelectorAll('table')) {
                const score = countPlotMarkers(table);
                if (score > bestScore) {
                    bestScore = score;
                    best = { table, tableId: 'ContentArea-table' };
                }
            }
        }
        return best;
    };

    const extractFromRows = (rows) => {
        const results = [];
        for (let i = 0; i < rows.length; i++) {
            const row = rows[i];
            const plotNo = getPlotNo(row);
            if (!plotNo) continue;

            let acre = getText(row, ['span[id*="lblAcre"]', 'span[id*="Acre"]', '[id*="Acre"]']);
            let decimil = getText(row, ['span[id*="lblDecimil"]', 'span[id*="Decimil"]', '[id*="Decimil"]']);
            let hector = getText(row, ['span[id*="lblHector"]', 'span[id*="Hector"]', '[id*="Hector"]']);
            let landType = getText(row, [
                'span[id*="lblCNItype"]', 'span[id*="lbllType"]',
                'span[id*="LandType"]', '[id*="CNItype"]', '[id*="lblKisam"]',
            ]);

            if (!acre && !decimil && !hector) {
                const nums = Array.from(row.querySelectorAll('td, span'))
                    .map(cellText)
                    .filter(v => v && /^\\d+(\\.\\d+)?$/.test(v));
                if (nums.length >= 3) {
                    acre = nums[nums.length - 4] || nums[0] || '';
                    decimil = nums[nums.length - 3] || '';
                    hector = nums[nums.length - 2] || '';
                }
            }

            results.push({
                plot_no: plotNo,
                chaka: getChaka(row),
                land_type: landType,
                kisam: getText(row, ['span[id*="lblKisama"]', 'span[id*="Kisam"]', '[id*="Kisam"]']),
                n_occu: getText(row, ['span[id*="lbln_occu"]', '[id*="n_occu"]']),
                e_occu: getText(row, ['span[id*="lble_occu"]', '[id*="e_occu"]']),
                s_occu: getText(row, ['span[id*="lbls_occu"]', '[id*="s_occu"]']),
                w_occu: getText(row, ['span[id*="lblw_occu"]', '[id*="w_occu"]']),
                acre,
                decimil,
                hector,
                remarks: getText(row, ['span[id*="lblPlotRemarks"]', 'span[id*="Remarks"]', '[id*="Remark"]']),
            });
        }
        return results;
    };

    const found = findPlotTable();
    if (!found) return { plots: [], tableId: '', rowCount: 0 };

    const rows = found.table.querySelectorAll('tr');
    const plots = extractFromRows(rows);
    return { plots, tableId: found.tableId, rowCount: rows.length };
}
""".replace('%FORM20_PLOT_TABLE_IDS%', json.dumps(_FORM20_PLOT_TABLE_IDS)).replace(
    '%NON_PLOT_TABLE_IDS%', json.dumps(list(_NON_PLOT_TABLE_IDS))
)

# ── Resilience tunables ────────────────────────────────────────────────────────
# Per-village hard timeout (seconds).  If a village takes longer than this the
# worker aborts it (marks failed → will be re-claimed later) and moves on.
_VILLAGE_TIMEOUT = 1200         # 20 minutes — allows large khatiyans (750+ rows) to complete

# Backoff between retries inside process_khatiyan (seconds).
# 4 attempts total; wait grows: 5 → 20 → 60 → 180 s
_KHATIYAN_RETRY_BACKOFF = [5, 20, 60, 180]

# How many consecutive village-level failures before we declare "site is down"
# and start the long backoff + browser restart cycle.
_SITE_DOWN_THRESHOLD = 2        # was 3 — trigger backoff sooner

# Backoff schedule (seconds) when site-down is detected.
# Each consecutive failure after the threshold adds one step.
_SITE_BACKOFF = [30, 60, 120, 300, 600, 1200]

# After exhausting ALL backoff steps with no recovery, the worker exits cleanly
# so systemd can restart the service with fresh browser instances.
# Total patience before exit: ~2*_VILLAGE_TIMEOUT + sum(_SITE_BACKOFF) ≈ 38 min.
_SITE_DOWN_EXIT_AFTER = _SITE_DOWN_THRESHOLD + len(_SITE_BACKOFF)

# Restart the browser every N successfully completed villages to prevent
# Chromium memory leaks from slowly eating all RAM.
_BROWSER_RESTART_EVERY = 40

# Khatiyan dropdown: consecutive polls with unchanged option count before accepting.
_DROPDOWN_STABLE_POLLS = 3
_DROPDOWN_STABLE_INTERVAL_S = 0.5

# Minimum fraction of expected khatiyans that must be saved before marking done.
_COMPLETION_MIN_FRACTION = 0.5


def _checkpoint_looks_corrupt(
    last_khatiyan_no: Optional[str],
    khatiyans_fetched: int,
    expected_count: int,
) -> bool:
    """
    Detect resume checkpoints that would skip to end-of-dropdown while
    khatiyans_fetched is still far below expected (infinite retry loop).
    """
    if not last_khatiyan_no or expected_count <= 0 or khatiyans_fetched <= 0:
        return False
    if khatiyans_fetched >= expected_count * _COMPLETION_MIN_FRACTION:
        return False
    try:
        # Handles values like '205/255' as well as plain '58'
        kh_num = int(str(last_khatiyan_no).split("/")[0].strip())
    except (ValueError, AttributeError):
        return False
    # Checkpoint claims we finished a late khatiyan but saved very few records
    return kh_num >= expected_count * 0.85


class BhulekhScraper:
    """Main scraper class for Bhulekh website automation."""
    
    def __init__(self, base_url: str = "http://bhulekh.ori.nic.in", 
                 browser_type: str = "chromium",
                 use_persistent_context: bool = False,
                 user_data_dir: Optional[str] = None,
                 brave_executable_path: Optional[str] = None,
                 connect_to_browser: Optional[str] = None,
                 debug: bool = False,
                 limit_khatiyans: Optional[int] = None,
                 data_dir: Optional[str] = None,
                 resume: bool = False,
                 storage_backend: str = "sqlite",
                 delay_scale: float = 1.0):
        """
        Initialize the scraper.
        
        Args:
            base_url: Base URL of the website
            browser_type: Browser to use - 'chromium', 'firefox', 'webkit', or 'brave'
            use_persistent_context: If True, use persistent browser context (saves cookies, session)
            user_data_dir: Directory for persistent browser data (only used if use_persistent_context=True)
            brave_executable_path: Path to Brave browser executable (e.g., 'C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe')
            connect_to_browser: CDP endpoint URL to connect to existing browser (e.g., 'http://localhost:9222')
            limit_khatiyans: If set, stop after processing this many Khatiyans (for dry run).
        """
        self.base_url = base_url
        self.start_url = f"{base_url}/RoRView.aspx"
        self.data_list = []
        self.page: Optional[Page] = None
        self.browser = None
        self.context = None
        self.playwright = None
        self.browser_type = browser_type.lower()
        self.use_persistent_context = use_persistent_context
        self.user_data_dir = user_data_dir or "browser_data"
        self.brave_executable_path = brave_executable_path
        self.connect_to_browser = connect_to_browser
        self.debug = debug
        self.limit_khatiyans = limit_khatiyans
        self.khatiyans_processed = 0
        self.data_dir = data_dir  # if set, write to file per district (sqlite or ndjson)
        self.resume = resume
        self.storage_backend = storage_backend  # "sqlite" or "ndjson"
        self._current_storage: Optional[BhulekhStorageBase] = None
        self.delay_scale = delay_scale  # 1.0 = normal; 0.15 = fast (--fast). All human_delay() and fixed sleeps scaled.
        self._current_district_value: Optional[str] = None
        self._current_district_text: Optional[str] = None
        self.layout_stats: Dict[str, int] = {'type1': 0, 'type2': 0, 'form20': 0}
        
    def _record_layout_stat(self, ror_type: str) -> None:
        """Track RoR layout counts (session + persistent storage)."""
        key = ror_type if ror_type in self.layout_stats else 'type1'
        self.layout_stats[key] = self.layout_stats.get(key, 0) + 1
        if self._current_storage:
            # Persistent storage only tracks type1/type2 buckets
            storage_key = key if key in ('type1', 'type2') else 'type2'
            self._current_storage.increment_layout_stat(storage_key)
        total = sum(self.layout_stats.values())
        if total % 25 == 0:
            logger.info(
                f"Layout stats (session): type1={self.layout_stats.get('type1', 0)}, "
                f"type2={self.layout_stats.get('type2', 0)}, "
                f"form20={self.layout_stats.get('form20', 0)}"
            )

    async def _extract_form99_header_fields(self) -> Dict[str, str]:
        """Parse Form 99 header fields embedded as plain text (not always in labeled spans)."""
        fields: Dict[str, str] = {'form_no': '', 'parichheda': ''}
        try:
            body = await self.page.locator('body').inner_text()
            m = re.search(r'ଫର୍ମ\s*ନଂ\s*[-–]?\s*(\S+)', body)
            if m:
                fields['form_no'] = m.group(1).strip()
            m = re.search(r'ପରିଚ୍ଛେଦ\s*[-–]?\s*(\S+)', body)
            if m:
                fields['parichheda'] = m.group(1).strip()
        except Exception:
            pass
        return fields

    async def _extract_form20_header_fields(self) -> Dict[str, str]:
        """Parse Form-20 header fields from plain text (parishista, form no, section)."""
        fields: Dict[str, str] = {'form_no': '', 'parichheda': '', 'parishista': ''}
        try:
            body = await self.page.locator('body').inner_text()
            m = re.search(r'ଫର୍ମ\s*ନଂ\s*[-–]?\s*(\d+)', body)
            if m:
                fields['form_no'] = m.group(1).strip()
            m = re.search(r'ପରିଚ୍ଛେଦ\s*[-–]?\s*(\d+)', body)
            if m:
                fields['parichheda'] = m.group(1).strip()
            m = re.search(r'ପରିଶିଷ୍ଟ\s*[-–]?\s*(\S+)', body)
            if m:
                fields['parishista'] = m.group(1).strip()
        except Exception:
            pass
        return fields
        
    async def init_browser(self, headless: bool = False):
        """
        Initialize browser and page with human-like settings.
        
        Supports:
        - Playwright bundled browsers (chromium, firefox, webkit)
        - Brave browser (via executable path or CDP connection)
        """
        self.playwright = await async_playwright().start()
        
        # Browser-specific settings
        browser_config = {
            'chromium': {
                'launch_args': [],  # Args will be added in launch_options
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            },
            'brave': {
                'launch_args': [],  # Args will be added in launch_options
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Brave/120'
            },
            'firefox': {
                'launch_args': [],
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0'
            },
            'webkit': {
                'launch_args': [],
                'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15'
            }
        }
        
        config = browser_config.get(self.browser_type, browser_config['chromium'])
        
        # Handle Brave browser or CDP connection
        if self.connect_to_browser:
            # Connect to existing browser via CDP
            logger.info(f"Connecting to browser at {self.connect_to_browser}")
            self.browser = await self.playwright.chromium.connect_over_cdp(self.connect_to_browser)
            self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            logger.info("Connected to existing browser")
            return
        
        # Launch browser
        if self.browser_type == 'brave':
            # Use Brave browser
            if not self.brave_executable_path:
                # Try to find Brave in common locations
                import platform
                import os
                system = platform.system()
                
                if system == 'Windows':
                    common_paths = [
                        os.path.expanduser(r'~\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe'),
                        r'C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe',
                        r'C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe',
                    ]
                elif system == 'Darwin':  # macOS
                    common_paths = [
                        '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
                    ]
                else:  # Linux
                    common_paths = [
                        '/usr/bin/brave-browser',
                        '/usr/bin/brave',
                        '/snap/bin/brave',
                    ]
                
                brave_path = None
                for path in common_paths:
                    if os.path.exists(path):
                        brave_path = path
                        break
                
                if not brave_path:
                    raise FileNotFoundError(
                        "Brave browser not found. Please specify the path using --brave-path or install Brave browser.\n"
                        "Common locations:\n" + "\n".join(f"  - {p}" for p in common_paths)
                    )
                
                self.brave_executable_path = brave_path
                logger.info(f"Found Brave browser at: {self.brave_executable_path}")
            
            browser_launcher = self.playwright.chromium
        elif self.browser_type == 'chromium':
            browser_launcher = self.playwright.chromium
        elif self.browser_type == 'firefox':
            browser_launcher = self.playwright.firefox
        elif self.browser_type == 'webkit':
            browser_launcher = self.playwright.webkit
        else:
            raise ValueError(f"Unsupported browser type: {self.browser_type}. Use 'chromium', 'firefox', 'webkit', or 'brave'")
        
        # Create context (persistent or regular) with enhanced anti-detection
        context_options = {
            'viewport': {'width': 1920, 'height': 1080},
            'user_agent': config['user_agent'],
            'locale': 'en-US',
            'timezone_id': 'Asia/Kolkata',
            'permissions': ['geolocation'],
            'geolocation': {'latitude': 20.2961, 'longitude': 85.8245},  # Bhubaneswar coordinates
            'color_scheme': 'light',
            'extra_http_headers': {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
                'DNT': '1',
            }
        }
        
        # Launch options with enhanced anti-detection
        anti_detection_args = [
            '--disable-blink-features=AutomationControlled',
            '--disable-features=IsolateOrigins,site-per-process',
            '--disable-site-isolation-trials',
            '--disable-dev-shm-usage',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-default-apps',
            '--disable-popup-blocking',
            '--disable-translate',
            '--disable-background-timer-throttling',
            '--disable-renderer-backgrounding',
            '--disable-backgrounding-occluded-windows',
            '--disable-ipc-flooding-protection',
            '--force-color-profile=srgb',
            '--mute-audio',
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-infobars',
            '--window-size=1920,1080',
            '--start-maximized',
            '--disable-extensions-except',
            '--disable-extensions',
        ]
        
        launch_options = {
            'headless': headless,
            'args': config['launch_args'] + anti_detection_args
        }
        
        # For Brave, specify executable path
        if self.browser_type == 'brave':
            launch_options['executable_path'] = self.brave_executable_path
        elif self.browser_type == 'chromium':
            # Check env var set by aws_setup.sh for Ubuntu 26.04+ system Chromium
            import os
            sys_chromium = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH', '')
            if sys_chromium and os.path.exists(sys_chromium):
                launch_options['executable_path'] = sys_chromium
                logger.info(f"Using system Chromium: {sys_chromium}")
            elif getattr(sys, "frozen", False):
                _chromium_exe = _find_chromium_executable()
                if _chromium_exe:
                    launch_options['executable_path'] = _chromium_exe
        
        if self.use_persistent_context:
            # Use persistent context (saves cookies, session, etc.)
            from pathlib import Path
            user_data_path = Path(self.user_data_dir)
            user_data_path.mkdir(exist_ok=True)
            
            if self.browser_type in ['chromium', 'brave']:
                persistent_options = {
                    'user_data_dir': str(user_data_path),
                    'headless': headless,
                    'viewport': context_options['viewport'],
                    'user_agent': context_options['user_agent'],
                    'locale': context_options['locale'],
                    'timezone_id': context_options['timezone_id'],
                    'extra_http_headers': context_options['extra_http_headers'],
                    'args': config['launch_args']
                }
                
                if self.browser_type == 'brave':
                    persistent_options['executable_path'] = self.brave_executable_path
                elif getattr(sys, "frozen", False) and self.browser_type == 'chromium':
                    _chromium_exe = _find_chromium_executable()
                    if _chromium_exe:
                        persistent_options['executable_path'] = _chromium_exe
                
                try:
                    self.context = await browser_launcher.launch_persistent_context(**persistent_options)
                except Exception as e:
                    err_msg = str(e)
                    if "Executable doesn't exist" in err_msg or "doesn't exist at" in err_msg:
                        if getattr(sys, "frozen", False):
                            raise RuntimeError(
                                "Chromium not installed next to this exe.\n"
                                "Run setup.exe from this folder (it will create a 'browsers' folder here),\n"
                                f"then run this exe again. Folder used: {_browsers_path}"
                            ) from e
                        raise RuntimeError(
                            "Chromium not installed. Run once: uv run python setup.py\n"
                            f"(installs Chromium to {_browsers_path})"
                        ) from e
                    raise
                self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
                self.browser = None  # No browser object for persistent context
            else:
                logger.warning(f"Persistent context not fully supported for {self.browser_type}, using regular context")
                try:
                    self.browser = await browser_launcher.launch(**launch_options)
                except Exception as e:
                    err_msg = str(e)
                    if "Executable doesn't exist" in err_msg or "doesn't exist at" in err_msg:
                        if getattr(sys, "frozen", False):
                            raise RuntimeError(
                                "Chromium not installed next to this exe.\n"
                                "Run setup.exe from this folder (it will create a 'browsers' folder here),\n"
                                f"then run this exe again. Folder used: {_browsers_path}"
                            ) from e
                        raise RuntimeError(
                            "Chromium not installed. Run once: uv run python setup.py\n"
                            f"(installs Chromium to {_browsers_path})"
                        ) from e
                    raise
                self.context = await self.browser.new_context(**context_options)
                self.page = await self.context.new_page()
        else:
            # Regular context
            try:
                self.browser = await browser_launcher.launch(**launch_options)
            except Exception as e:
                err_msg = str(e)
                if "Executable doesn't exist" in err_msg or "doesn't exist at" in err_msg:
                    if getattr(sys, "frozen", False):
                        raise RuntimeError(
                            "Chromium not installed next to this exe.\n"
                            "Run setup.exe from this folder (it will create a 'browsers' folder here),\n"
                            f"then run this exe again. Folder used: {_browsers_path}"
                        ) from e
                    raise RuntimeError(
                        "Chromium not installed. Run once: uv run python setup.py\n"
                        f"(installs Chromium to {_browsers_path})"
                    ) from e
                raise
            self.context = await self.browser.new_context(**context_options)
            self.page = await self.context.new_page()
        
        # Enhanced anti-detection scripts (only for Chromium-based browsers/WebKit)
        if self.browser_type in ['chromium', 'brave', 'webkit']:
            await self.context.add_init_script("""
                // Remove webdriver property
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                // Override plugins to look more realistic
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                
                // Override languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });
                
                // Override platform
                Object.defineProperty(navigator, 'platform', {
                    get: () => 'Win32'
                });
                
                // Override hardwareConcurrency
                Object.defineProperty(navigator, 'hardwareConcurrency', {
                    get: () => 8
                });
                
                // Override deviceMemory
                Object.defineProperty(navigator, 'deviceMemory', {
                    get: () => 8
                });
                
                // Chrome runtime
                window.chrome = {
                    runtime: {}
                };
                
                // Override permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: Notification.permission }) :
                        originalQuery(parameters)
                );
                
                // Override getBattery
                if (navigator.getBattery) {
                    navigator.getBattery = () => Promise.resolve({
                        charging: true,
                        chargingTime: 0,
                        dischargingTime: Infinity,
                        level: 1
                    });
                }
                
                // Canvas fingerprint protection
                const getImageData = CanvasRenderingContext2D.prototype.getImageData;
                CanvasRenderingContext2D.prototype.getImageData = function() {
                    const imageData = getImageData.apply(this, arguments);
                    for (let i = 0; i < imageData.data.length; i += 4) {
                        imageData.data[i] += Math.floor(Math.random() * 10) - 5;
                    }
                    return imageData;
                };
                
                // WebGL fingerprint protection
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) {
                        return 'Intel Inc.';
                    }
                    if (parameter === 37446) {
                        return 'Intel Iris OpenGL Engine';
                    }
                    return getParameter.apply(this, arguments);
                };
            """)
        
        browser_name = "Brave" if self.browser_type == 'brave' else self.browser_type
        logger.info(f"Browser initialized: {browser_name} (persistent={self.use_persistent_context})")
    
    async def human_delay(self, min_seconds: float = 1.0, max_seconds: float = 3.0):
        """Add random human-like delay. Scaled by self.delay_scale when set (e.g. --fast)."""
        if self.delay_scale != 1.0:
            min_seconds = max(0.02, min_seconds * self.delay_scale)
            max_seconds = max(0.02, max_seconds * self.delay_scale)
        delay = random.uniform(min_seconds, max_seconds)
        await asyncio.sleep(delay)
    
    async def check_for_timeout_error(self) -> bool:
        """
        Check if the page shows the website's timeout error screen.
        Only returns True when the actual timeout message is shown, not when
        generic words like 'session' or 'timeout' appear elsewhere on the page.
        """
        try:
            page_text = await self.page.content()
            page_text_lower = page_text.lower()

            # Specific phrases that appear ONLY on the Bhulekh timeout error page
            # (from: "You have been timed out of the website due to the following reasons")
            timeout_page_phrases = [
                "timed out of the website",
                "you have been timed out",
                "back/forward/refresh button",
                "multiple clicks on an option or button",
                "long inactive period",
                "you may start another session by clicking",
                "start another session by clicking here",
            ]

            # Require at least 2 distinct phrases to avoid false positives
            # (e.g. "session" and "timeout" appear in many normal pages)
            matches = [p for p in timeout_page_phrases if p in page_text_lower]
            if len(matches) >= 2:
                logger.error(f"Timeout error screen detected. Matched phrases: {matches}")
                return True

            # Strong single-phrase: the exact timeout message heading
            if "timed out of the website" in page_text_lower or "you have been timed out" in page_text_lower:
                logger.error("Timeout error screen detected (heading phrase).")
                return True

            return False
        except Exception as e:
            msg = str(e).lower()
            # Page navigating / not readable = can't check; assume no timeout so caller can retry
            if "navigating" in msg or "not readable" in msg:
                logger.debug("Could not check timeout (page changing): %s", e)
            else:
                logger.warning("Error checking for timeout: %s", e)
            return False
    
    async def wait_for_page_load(self, timeout: int = 30000):
        """Wait for page to fully load."""
        try:
            await self.page.wait_for_load_state('networkidle', timeout=timeout)
            await self.human_delay(0.2, 0.4)  # Minor delay after page load
            
            # Check for timeout errors
            if await self.check_for_timeout_error():
                raise Exception("Website timeout error detected")
        except PlaywrightTimeoutError:
            logger.warning("Page load timeout, continuing anyway")
            await self.human_delay(0.2, 0.4)
    
    async def select_dropdown(self, selector: str, value: str, wait_for_update: bool = True, label: str = None):
        """Select a value from a dropdown and wait for dependent fields to update.
        
        For Khatiyan dropdowns, prefer using label= parameter since the option values
        have trailing whitespace padding that can cause matching issues.
        """
        try:
            # Simulate human behavior: move mouse to dropdown first
            try:
                dropdown = self.page.locator(selector)
                await dropdown.hover()
                await self.human_delay(0.15, 0.35)
            except:
                pass
            
            # For Khatiyan dropdown, use label-based selection (values have trailing spaces)
            is_khatiyan = 'ddlBindData' in selector or 'Khatiyan' in selector
            
            # Select the option with timeout protection
            try:
                if label:
                    await asyncio.wait_for(
                        self.page.select_option(selector, label=label),
                        timeout=15.0
                    )
                    logger.info(f"Selected label '{label}' from {selector}")
                elif is_khatiyan:
                    # Khatiyan values have trailing spaces; try label first using trimmed value
                    trimmed = value.strip() if value else value
                    try:
                        await asyncio.wait_for(
                            self.page.select_option(selector, label=trimmed),
                            timeout=15.0
                        )
                        logger.info(f"Selected khatiyan by label '{trimmed}' from {selector}")
                    except Exception:
                        # Fallback to value-based selection
                        await asyncio.wait_for(
                            self.page.select_option(selector, value),
                            timeout=15.0
                        )
                        logger.info(f"Selected khatiyan by value '{value}' from {selector}")
                else:
                    await asyncio.wait_for(
                        self.page.select_option(selector, value),
                        timeout=15.0
                    )
                    logger.info(f"Selected {value} from {selector}")
            except asyncio.TimeoutError:
                logger.error(f"TIMEOUT selecting from {selector} (value={value!r}, label={label!r})")
                raise Exception(f"Dropdown selection timed out after 15s: {selector}")
            
            await self.human_delay(0.2, 0.4)
            
            if wait_for_update:
                await self.wait_for_page_load()
                await self.human_delay(0.3, 0.6)
        except Exception as e:
            logger.error(f"Error selecting dropdown {selector}: {e}")
            raise
    
    async def wait_for_dropdown_populated(self, selector: str, min_options: int = 1, 
                                          timeout_ms: int = 20000) -> bool:
        """
        Wait for a dropdown to be populated (e.g. after ASP.NET postback).
        Polls until the dropdown has at least min_options real options (excluding placeholders).
        """
        try:
            async def _check():
                opts = await self.get_dropdown_options(selector)
                return len(opts) >= min_options

            deadline = time.time() + (timeout_ms / 1000)
            while time.time() < deadline:
                if await _check():
                    return True
                await asyncio.sleep(max(0.05, 0.25 * self.delay_scale))
            logger.warning(f"Dropdown {selector} did not populate within {timeout_ms}ms")
            return False
        except Exception as e:
            logger.warning(f"Error waiting for dropdown: {e}")
            return False

    async def wait_for_dropdown_stable(
        self,
        selector: str,
        *,
        expected_count: Optional[int] = None,
        stable_polls: int = _DROPDOWN_STABLE_POLLS,
        poll_interval_s: float = _DROPDOWN_STABLE_INTERVAL_S,
        timeout_ms: int = 20000,
    ) -> int:
        """
        Wait until a dropdown's option count stabilizes or reaches expected_count.

        Stability means the count stays unchanged for `stable_polls` consecutive
        polls (~poll_interval_s apart).  If expected_count is known, stabilization
        below that count is not accepted — polling continues until expected_count
        is reached or timeout.
        """
        deadline = time.time() + (timeout_ms / 1000)
        interval = max(0.05, poll_interval_s * self.delay_scale)
        last_count: Optional[int] = None
        stable_streak = 0

        while time.time() < deadline:
            count = len(await self.get_dropdown_options(selector))

            if expected_count is not None and count >= expected_count:
                logger.info(
                    "Dropdown %s reached expected count %d (got %d)",
                    selector, expected_count, count,
                )
                return count

            if last_count is not None and count == last_count:
                stable_streak += 1
                if stable_streak >= stable_polls and count > 0:
                    if expected_count is not None and count < expected_count:
                        logger.warning(
                            "Dropdown %s stabilized at %d options (expected %d) — accepting anyway",
                            selector, count, expected_count,
                        )
                    else:
                        logger.info(
                            "Dropdown %s stabilized at %d options",
                            selector, count,
                        )
                    return count
            else:
                stable_streak = 1 if count > 0 else 0
                last_count = count

            await asyncio.sleep(interval)

        final_count = len(await self.get_dropdown_options(selector))
        raise TimeoutError(
            f"Dropdown {selector} did not stabilize within {timeout_ms}ms "
            f"(last count: {final_count}, expected: {expected_count})"
        )

    async def wait_for_khatiyan_selector_ready(
        self,
        *,
        min_options: int = 1,
        timeout_ms: int = 12000,
    ) -> bool:
        """
        Fast wait after Khatiyan Page back-navigation.

        District/tahasil/village/khatiyan dropdowns stay populated on the search
        form — no need for networkidle. Wait until ddlBindData is visible with
        real options.
        """
        try:
            await self.page.wait_for_selector(
                SELECTOR_KHATIYAN, state="visible", timeout=timeout_ms,
            )
            deadline = time.time() + (timeout_ms / 1000)
            interval = max(0.05, 0.1 * self.delay_scale)
            while time.time() < deadline:
                if await self.check_for_timeout_error():
                    raise Exception("Website timeout error detected")
                if len(await self.get_dropdown_options(SELECTOR_KHATIYAN)) >= min_options:
                    await self.human_delay(0.05, 0.15)
                    return True
                await asyncio.sleep(interval)
            logger.warning(
                "Khatiyan selector visible but <%d options within %dms",
                min_options, timeout_ms,
            )
            return False
        except PlaywrightTimeoutError:
            logger.warning("Khatiyan selector not visible within %dms", timeout_ms)
            return False
        except Exception as e:
            logger.warning("wait_for_khatiyan_selector_ready failed: %s", e)
            return False

    async def wait_for_ror_view_ready(
        self,
        *,
        timeout_ms: int = 12000,
    ) -> bool:
        """
        Fast wait after View RoR — wait for RoR content markers, not networkidle.
        """
        try:
            if await self.check_for_timeout_error():
                raise Exception("Website timeout error detected")
            await self.page.wait_for_selector(
                SELECTOR_ROR_VIEW_READY, state="visible", timeout=timeout_ms,
            )
            await self.human_delay(0.05, 0.15)
            return True
        except PlaywrightTimeoutError:
            try:
                if await self._is_form20_ror():
                    logger.info("Form-20 RoR layout detected (fast wait)")
                    return True
            except Exception:
                pass
            return False
        except Exception as e:
            logger.warning("wait_for_ror_view_ready failed: %s", e)
            return False

    async def get_dropdown_options(self, selector: str) -> List[Dict[str, str]]:
        """Get all options from a dropdown.
        
        The selector uses 'id*=' which means "contains", so it will match ASP.NET IDs like:
        - ctl00_ContentPlaceHolder1_ddlDistrict
        - ctl00_ContentPlaceHolder1_ddlTahsil
        - ctl00_ContentPlaceHolder1_ddlVillage
        - ctl00_ContentPlaceHolder1_ddlBindData (Khatiyan - has padded values!)
        
        Returns:
            List of dicts with 'value' (raw), 'value_trimmed' (whitespace stripped), 
            and 'text' (visible label, always trimmed).
            
        Note: Khatiyan dropdown values are padded to 30 chars (e.g., "01                            ").
              Use 'value_trimmed' or select by 'text' (label) to avoid matching issues.
        """
        try:
            # Escape selector for use inside JS double-quoted string
            sel_esc = selector.replace('\\', '\\\\').replace('"', '\\"')
            options = await self.page.evaluate(f"""
                () => {{
                    const select = document.querySelector("{sel_esc}");
                    if (!select) return [];
                    return Array.from(select.options).map(opt => ({{
                        value: opt.value,
                        value_trimmed: opt.value.trim(),
                        text: opt.text.trim()
                    }})).filter(opt => opt.value && opt.value !== 'Select District' && 
                                     opt.value !== 'Select Tahasil' && 
                                     opt.value !== 'Select Village' &&
                                     opt.value !== 'Select Khatiyan');
                }}
            """)
            return options
        except Exception as e:
            logger.error(f"Error getting dropdown options for {selector}: {e}")
            return []
    
    async def select_search_type(self, search_type: str = "Khatiyan"):
        """Select the search type (Khatiyan/Plot/Tenant). Khatiyan radio may be disabled until District is selected."""
        try:
            if search_type == "Khatiyan":
                radio = self.page.locator(SELECTOR_RADIO_KHATIYAN)
            elif search_type == "Plot":
                radio = self.page.locator('input#ctl00_ContentPlaceHolder1_rbtnRORSearchtype_1, input[value="Plot"][name*="rbtnRORSearchtype"]')
            elif search_type == "Tenant":
                radio = self.page.locator('input#ctl00_ContentPlaceHolder1_rbtnRORSearchtype_2, input[value="Tenant"][name*="rbtnRORSearchtype"]')
            else:
                raise ValueError(f"Invalid search type: {search_type}")

            await radio.wait_for(state='visible', timeout=10000)

            # Khatiyan radio can be disabled until District (and sometimes Tahsil) is selected
            is_disabled = await radio.get_attribute('disabled')
            if is_disabled:
                logger.info("Search type radio is disabled; waiting for it to enable (e.g. after Tahsil loads)...")
                for _ in range(20):
                    await self.human_delay(0.3, 0.6)
                    is_disabled = await radio.get_attribute('disabled')
                    if not is_disabled:
                        break
            if is_disabled:
                logger.warning("Search type radio still disabled; assuming Khatiyan mode and continuing.")
            else:
                await radio.hover()
                await self.human_delay(0.15, 0.35)
                await radio.check()

            await self.wait_for_page_load()
            logger.info(f"Selected search type: {search_type}")
        except Exception as e:
            logger.error(f"Error selecting search type: {e}")
            raise
    
    async def navigate_to_ror_page(self):
        """Navigate to the initial RoR view page with anti-detection measures."""
        try:
            # First, visit a simple page to establish session (like a real user)
            logger.info("Establishing browser session...")
            await self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=30000)
            await self.human_delay(0.4, 0.8)
            
            try:
                await self.page.mouse.move(random.randint(100, 500), random.randint(100, 500))
                await asyncio.sleep(max(0.02, random.uniform(0.2, 0.5) * self.delay_scale))
            except:
                pass
            
            # Now navigate to the actual page
            logger.info("Navigating to RoR view page...")
            
            # Use referer header to look like we came from the main page
            await self.page.set_extra_http_headers({
                'Referer': self.base_url + '/'
            })
            
            await self.page.goto(
                self.start_url, 
                wait_until='domcontentloaded',  # Changed from networkidle to be faster
                timeout=60000,
                referer=self.base_url + '/'
            )
            
            await self.human_delay(0.3, 0.6)
            await self.wait_for_page_load()
            
            # Check if we're actually on the page (look for key elements)
            try:
                await self.page.wait_for_selector(SELECTOR_DISTRICT, timeout=10000)
                logger.info("Successfully loaded RoR view page")
            except:
                # Page might have redirected or shown error
                page_url = self.page.url
                page_title = await self.page.title()
                page_content = await self.page.content()
                
                logger.warning(f"Page may not have loaded correctly. URL: {page_url}, Title: {page_title}")
                
                # Save page content for debugging
                if self.debug:
                    try:
                        with open('debug_page_content.html', 'w', encoding='utf-8') as f:
                            f.write(page_content)
                        await self.page.screenshot(path='debug_page_screenshot.png', full_page=True)
                        logger.info("Saved debug files: debug_page_content.html and debug_page_screenshot.png")
                    except Exception as e:
                        logger.warning(f"Could not save debug files: {e}")
                
                # Check for common detection/block messages
                detection_keywords = [
                    'bot', 'automation', 'blocked', 'access denied', 
                    'captcha', 'verify', 'suspicious', 'security check',
                    'cloudflare', 'ddos protection', 'rate limit'
                ]
                
                content_lower = page_content.lower()
                detected_keywords = [kw for kw in detection_keywords if kw in content_lower]
                
                if detected_keywords:
                    logger.error(f"Detection keywords found on page: {detected_keywords}")
                    logger.error("The website appears to be blocking automation. Try:")
                    logger.error("1. Use --persistent flag to maintain session")
                    logger.error("2. Use --browser brave to use your actual browser")
                    logger.error("3. Add longer delays between requests")
                    logger.error("4. Check if you need to solve a CAPTCHA manually first")
                
                # Check for timeout error
                if await self.check_for_timeout_error():
                    logger.warning("Timeout error detected, retrying navigation...")
                    await self.human_delay(1.0, 2.0)  # Brief delay before retry
                    
                    # Clear cookies and try again
                    await self.context.clear_cookies()
                    await self.page.goto(self.base_url, wait_until='domcontentloaded', timeout=30000)
                    await self.human_delay(0.8, 1.5)
                    await self.page.goto(self.start_url, wait_until='domcontentloaded', timeout=60000)
                    await self.wait_for_page_load()
                    
                    try:
                        await self.page.wait_for_selector(SELECTOR_DISTRICT, timeout=10000)
                        logger.info("Successfully loaded RoR view page after retry")
                    except:
                        raise Exception("Could not load RoR page even after retry. Page may be blocking automation.")
                else:
                    raise Exception(f"Page loaded but district dropdown not found. URL: {page_url}. Check debug files if --debug was used.")
            
        except Exception as e:
            logger.error(f"Error navigating to page: {e}")
            # Try to get page content for debugging
            try:
                page_url = self.page.url
                page_title = await self.page.title()
                logger.error(f"Current URL: {page_url}, Title: {page_title}")
            except:
                pass
            raise
    
    async def _is_form20_ror(self) -> bool:
        """
        Detect Form-20 RoR layout: survey/settlement format (Subarnapur and similar).
        Must run before Type-2 detection — Form 20 shares some Odia markers with Form 99.
        """
        try:
            body = await self.page.locator('body').inner_text()
            return _is_form20_body(body)
        except Exception:
            return False

    async def _is_type2_ror(self) -> bool:
        """
        Detect Type-2 RoR layout: Form 99 / ପରିଶିଷ୍ଟ pages.
        Includes Form-99 appendix pages that still use #gvfront with lblPlotci plot tables.
        """
        body = await self.page.locator('body').inner_text()
        type2_markers = ['ପରିଶିଷ୍ଟ', 'ଫର୍ମ ନଂ', 'ପରିଚ୍ଛେଦ', 'ଭୂ-ସ୍ୱାମୀ']
        return any(m in body for m in type2_markers)

    async def extract_ror_data(self) -> Dict:
        """
        Extract data from the RoR page.
        Automatically detects Form-20 (survey/settlement), Type-1 (gvfront GridView),
        or Type-2 (ପରିଶିଷ୍ଟ / Form 99) and uses the appropriate extractor.
        """
        try:
            html = await self.page.content()
            parsed = parse_ror_html(html)
            plot_count = len(parsed.get("plots", []))
            header_fields = sum(
                1 for k, v in parsed.items()
                if k not in ("plots", "ror_type") and v
            )
            if plot_count > 0 or header_fields >= 3:
                logger.info(
                    "Extracted via ror_parser: type=%s, %d plots, %d header fields",
                    parsed.get("ror_type"), plot_count, header_fields,
                )
                return parsed
        except Exception as e:
            logger.warning("ror_parser extraction failed, using legacy extractors: %s", e)

        try:
            if await self._is_form20_ror():
                logger.info("Detected Form-20 RoR layout (survey/settlement)")
                return await self.extract_ror_data_form20()

            if await self._is_type2_ror():
                logger.info("Detected Type-2 RoR layout (ପରିଶିଷ୍ଟ / Form-99)")
                data = await self.extract_ror_data_type2()
                # Form-99 appendix pages still render #gvfront — gvfront fields take precedence
                if await self.page.locator('#gvfront').count() > 0:
                    front_data = await self.extract_front_page_data()
                    for field, val in front_data.items():
                        if val:
                            data[field] = val
                return data

            data = {}
            front_data = await self.extract_front_page_data()
            data.update(front_data)
            back_data = await self.extract_back_page_data()
            data['plots'] = back_data
            data['ror_type'] = 'type1'
            return data
        except Exception as e:
            logger.error(f"Error extracting RoR data: {e}")
            return {}

    async def extract_ror_data_type2(self) -> Dict:
        """
        Extract data from Type-2 RoR layout: "ପରିଶିଷ୍ଟ - ଖ / Form 99".

        Type-2 uses a different HTML structure.  The page does not have a
        #gvfront GridView; instead it has a table-based layout with Odia
        label text.  We extract by:
          1. Trying known alternative element IDs (variation on gvfront pattern).
          2. Falling back to label-text scanning — find any <td> whose text
             matches a known Odia label, then read the NEXT sibling <td> as
             the value.  This is resilient to ID changes between districts.
        """
        data: Dict = {'ror_type': 'type2', 'plots': []}

        # ── Strategy 1: try alternative GridView IDs seen on Type-2 pages ─────
        # Some districts use gvRorFront instead of gvfront for Type-2
        alt_map = {
            'mouja':         ['#gvRorFront_ctl02_lblMouja',    '#ctl00_ContentPlaceHolder1_lblMouja'],
            'tehsil':        ['#gvRorFront_ctl02_lblTehsil',   '#ctl00_ContentPlaceHolder1_lblTehsil'],
            'thana':         ['#gvRorFront_ctl02_lblThana',    '#ctl00_ContentPlaceHolder1_lblThana'],
            'tehsil_no':     ['#gvRorFront_ctl02_lblTesilNo',  '#ctl00_ContentPlaceHolder1_lblTesilNo'],
            'thana_no':      ['#gvRorFront_ctl02_lblThanano',  '#ctl00_ContentPlaceHolder1_lblThanano'],
            'district':      ['#gvRorFront_ctl02_lblDist',     '#ctl00_ContentPlaceHolder1_lblDist'],
            'landlord_name': ['#gvRorFront_ctl02_lblLandlordName', '#ctl00_ContentPlaceHolder1_lblLandlordName',
                              '#gvRorFront_ctl02_lblBhuswami', '#ctl00_ContentPlaceHolder1_lblBhuswami'],
            'khatiyan_sl_no':['#gvRorFront_ctl02_lblKhatiyanslNo'],
            'tenant_name':   ['#gvRorFront_ctl02_lblName',     '#ctl00_ContentPlaceHolder1_lblName',
                              '#gvRorFront_ctl02_lblRaiyat',   '#ctl00_ContentPlaceHolder1_lblRaiyat'],
            'status':        ['#gvRorFront_ctl02_lblStatua',   '#ctl00_ContentPlaceHolder1_lblStatua'],
            'water_tax':     ['#gvRorFront_ctl02_lblWaterTax', '#ctl00_ContentPlaceHolder1_lblWaterTax'],
            'tax':           ['#gvRorFront_ctl02_lblTax',      '#ctl00_ContentPlaceHolder1_lblTax'],
            'ses':           ['#gvRorFront_ctl02_lblSes'],
            'other_ses':     ['#gvRorFront_ctl02_lblOtherses'],
            'total':         ['#gvRorFront_ctl02_lblTotal',    '#ctl00_ContentPlaceHolder1_lblTotal'],
            'description':   ['#gvRorFront_ctl02_lblDescription'],
            'special_case':  ['#gvRorFront_ctl02_lblSpecialCase'],
            'last_publish_date': ['#gvRorFront_ctl02_lblLastPublishDate'],
            'tax_date':      ['#gvRorFront_ctl02_lblTaxDate'],
            # Type-2 specific fields
            'form_no':       ['#ctl00_ContentPlaceHolder1_lblFormNo', '[id*="lblFormNo"]'],
            'parichheda':    ['#ctl00_ContentPlaceHolder1_lblParichheda', '[id*="lblParichheda"]'],
        }

        for field, selectors in alt_map.items():
            for sel in selectors:
                try:
                    el = self.page.locator(sel)
                    if await el.count() > 0:
                        val = await el.inner_text()
                        if val.strip():
                            data[field] = val.strip()
                            break
                except Exception:
                    pass
            if field not in data:
                data[field] = ''

        # ── Strategy 2: label-text scan (fallback, catches any layout variant) ─
        # Map of Odia label text → field name
        odia_labels = {
            'ମୌଜା':         'mouja',
            'ତହସିଲ':        'tehsil',
            'ଥାନା':         'thana',
            'ଜିଲ୍ଲା':      'district',
            'ଭୂ-ସ୍ୱାମୀ':   'landlord_name',
            'ଖେୱାଟ':        'landlord_name',
            'ରୟତ':          'tenant_name',
            'ଅଧିବାସୀ':     'tenant_name',
            'ଫର୍ମ ନଂ':      'form_no',
            'ପରିଚ୍ଛେଦ':    'parichheda',
        }
        # Only run label scan if Strategy 1 left critical fields empty
        if not data.get('mouja') or not data.get('landlord_name'):
            try:
                all_cells = await self.page.locator('td').all()
                for i, cell in enumerate(all_cells[:-1]):
                    cell_text = (await cell.inner_text()).strip()
                    for label, field in odia_labels.items():
                        if label in cell_text and not data.get(field):
                            try:
                                next_cells = await self.page.locator('td').nth(i + 1).inner_text()
                                val = next_cells.strip().lstrip(':：').strip()
                                if val and val not in odia_labels:
                                    data[field] = val
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"Type-2 label scan failed: {e}")

        form99 = await self._extract_form99_header_fields()
        for field in ('form_no', 'parichheda'):
            if form99.get(field) and not data.get(field):
                data[field] = form99[field]

        # ── Plot rows: same back-table extractor as Type-1 ────────────────────
        back_plots = await self.extract_back_page_data()
        data['plots'] = back_plots

        # Log what we captured
        filled = sum(1 for v in data.values() if v and v != [] and v != 'type2')
        logger.info(f"Type-2 extraction: {filled} fields populated, {len(data['plots'])} plots")

        if filled <= 2:
            # Complete extraction failure — capture raw page text as fallback
            try:
                raw_text = await self.page.locator('body').inner_text()
                data['raw_page_text'] = raw_text[:3000]
                logger.warning("Type-2 extraction got nothing; raw page text captured for debugging")
            except Exception:
                pass

        return data

    async def extract_ror_data_form20(self) -> Dict:
        """
        Extract data from Form-20 RoR layout: survey/settlement format.

        Form-20 pages use Odia labels (ଭୂ-ସ୍ୱାମୀ, ରାୟତ) and often omit the standard
        #gvfront / #gvRorBack structure.  Extraction mirrors Type-2 (selector map +
        label scan) but uses Form-20 plot table IDs and maps bhuswami/raiyat fields.
        """
        data: Dict = {'ror_type': 'form20', 'plots': []}

        alt_map = {
            'mouja':         ['#gvRorFront_ctl02_lblMouja', '#gvfront_ctl02_lblMouja',
                              '#ctl00_ContentPlaceHolder1_lblMouja', '[id*="lblMouja"]'],
            'tehsil':        ['#gvRorFront_ctl02_lblTehsil', '#gvfront_ctl02_lblTehsil',
                              '#ctl00_ContentPlaceHolder1_lblTehsil', '[id*="lblTehsil"]'],
            'thana':         ['#gvRorFront_ctl02_lblThana', '#gvfront_ctl02_lblThana',
                              '#ctl00_ContentPlaceHolder1_lblThana', '[id*="lblThana"]'],
            'tehsil_no':     ['#gvRorFront_ctl02_lblTesilNo', '#gvfront_ctl02_lblTesilNo',
                              '[id*="lblTesilNo"]'],
            'thana_no':      ['#gvRorFront_ctl02_lblThanano', '#gvfront_ctl02_lblThanano',
                              '[id*="lblThanano"]'],
            'district':      ['#gvRorFront_ctl02_lblDist', '#gvfront_ctl02_lblDist',
                              '#ctl00_ContentPlaceHolder1_lblDist', '[id*="lblDist"]'],
            'landlord_name': ['#gvRorFront_ctl02_lblBhuswami', '#ctl00_ContentPlaceHolder1_lblBhuswami',
                              '#gvRorFront_ctl02_lblLandlordName', '#gvfront_ctl02_lblLandlordName',
                              '[id*="lblBhuswami"]', '[id*="lblLandlordName"]'],
            'khatiyan_sl_no':['#gvRorFront_ctl02_lblKhatiyanslNo', '#gvfront_ctl02_lblKhatiyanslNo',
                              '[id*="lblKhatiyanslNo"]'],
            'tenant_name':   ['#gvRorFront_ctl02_lblRaiyat', '#ctl00_ContentPlaceHolder1_lblRaiyat',
                              '#gvRorFront_ctl02_lblName', '#gvfront_ctl02_lblName',
                              '[id*="lblRaiyat"]', '[id*="lblName"]'],
            'form_no':       ['#ctl00_ContentPlaceHolder1_lblFormNo', '[id*="lblFormNo"]'],
            'parichheda':    ['#ctl00_ContentPlaceHolder1_lblParichheda', '[id*="lblParichheda"]'],
            'parishista':    ['#ctl00_ContentPlaceHolder1_lblParishista', '[id*="lblParishista"]'],
        }

        for field, selectors in alt_map.items():
            for sel in selectors:
                try:
                    el = self.page.locator(sel)
                    if await el.count() > 0:
                        val = await el.inner_text()
                        if val.strip():
                            data[field] = val.strip()
                            break
                except Exception:
                    pass
            if field not in data:
                data[field] = ''

        odia_labels = {
            'ମୌଜା':         'mouja',
            'ତହସିଲ':        'tehsil',
            'ଥାନା':         'thana',
            'ଜିଲ୍ଲା':      'district',
            'ଭୂ-ସ୍ୱାମୀ':   'landlord_name',
            'ଖେୱାଟ':        'landlord_name',
            'ରାୟତ':          'tenant_name',
            'ରୟତ':          'tenant_name',
            'ଅଧିବାସୀ':     'tenant_name',
            'ଫର୍ମ ନଂ':      'form_no',
            'ପରିଚ୍ଛେଦ':    'parichheda',
            'ପରିଶିଷ୍ଟ':    'parishista',
        }
        if not data.get('mouja') or not data.get('landlord_name'):
            try:
                all_cells = await self.page.locator('td').all()
                for i, cell in enumerate(all_cells[:-1]):
                    cell_text = (await cell.inner_text()).strip()
                    for label, field in odia_labels.items():
                        if label in cell_text and not data.get(field):
                            try:
                                next_cells = await self.page.locator('td').nth(i + 1).inner_text()
                                val = next_cells.strip().lstrip(':：').strip()
                                if val and val not in odia_labels:
                                    data[field] = val
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"Form-20 label scan failed: {e}")

        form20 = await self._extract_form20_header_fields()
        for field in ('form_no', 'parichheda', 'parishista'):
            if form20.get(field) and not data.get(field):
                data[field] = form20[field]

        data['plots'] = await self.extract_back_page_data_form20()

        filled = sum(1 for v in data.values() if v and v != [] and v != 'form20')
        logger.info(f"Form-20 extraction: {filled} fields populated, {len(data['plots'])} plots")

        if filled <= 2:
            try:
                raw_text = await self.page.locator('body').inner_text()
                data['raw_page_text'] = raw_text[:3000]
                logger.warning("Form-20 extraction got nothing; raw page text captured for debugging")
            except Exception:
                pass

        return data

    async def extract_back_page_data_form20(self) -> List[Dict]:
        """Extract plot rows from Form-20 settlement tables."""
        plots: List[Dict] = []
        try:
            result = await self.page.evaluate(_EXTRACT_PLOTS_FORM20_JS)
            plots = result.get('plots', []) if isinstance(result, dict) else (result or [])
            table_id = result.get('tableId', '') if isinstance(result, dict) else ''
            if plots:
                logger.info(
                    f"Form-20: extracted {len(plots)} plots"
                    + (f" (table={table_id})" if table_id else "")
                )
            elif await self.page.locator('#gvRorBack, #gvRorBack2, #gvplotdetail').count() > 0:
                plots = await self.extract_back_page_data()
        except Exception as e:
            logger.error(f"Error extracting Form-20 plot data: {e}")
        return plots
    
    async def extract_front_page_data(self) -> Dict:
        """Extract data from the front page of RoR."""
        data = {}
        
        try:
            # Extract location information
            data['mouja'] = await self.page.locator('#gvfront_ctl02_lblMouja').inner_text() if await self.page.locator('#gvfront_ctl02_lblMouja').count() > 0 else ""
            data['tehsil'] = await self.page.locator('#gvfront_ctl02_lblTehsil').inner_text() if await self.page.locator('#gvfront_ctl02_lblTehsil').count() > 0 else ""
            data['thana'] = await self.page.locator('#gvfront_ctl02_lblThana').inner_text() if await self.page.locator('#gvfront_ctl02_lblThana').count() > 0 else ""
            data['tehsil_no'] = await self.page.locator('#gvfront_ctl02_lblTesilNo').inner_text() if await self.page.locator('#gvfront_ctl02_lblTesilNo').count() > 0 else ""
            data['thana_no'] = await self.page.locator('#gvfront_ctl02_lblThanano').inner_text() if await self.page.locator('#gvfront_ctl02_lblThanano').count() > 0 else ""
            data['district'] = await self.page.locator('#gvfront_ctl02_lblDist').inner_text() if await self.page.locator('#gvfront_ctl02_lblDist').count() > 0 else ""
            
            # Extract landlord/khata information
            data['landlord_name'] = await self.page.locator('#gvfront_ctl02_lblLandlordName').inner_text() if await self.page.locator('#gvfront_ctl02_lblLandlordName').count() > 0 else ""
            data['khatiyan_sl_no'] = await self.page.locator('#gvfront_ctl02_lblKhatiyanslNo').inner_text() if await self.page.locator('#gvfront_ctl02_lblKhatiyanslNo').count() > 0 else ""
            
            # Extract tenant information
            data['tenant_name'] = await self.page.locator('#gvfront_ctl02_lblName').inner_text() if await self.page.locator('#gvfront_ctl02_lblName').count() > 0 else ""
            data['status'] = await self.page.locator('#gvfront_ctl02_lblStatua').inner_text() if await self.page.locator('#gvfront_ctl02_lblStatua').count() > 0 else ""
            
            # Extract tax information
            data['water_tax'] = await self.page.locator('#gvfront_ctl02_lblWaterTax').inner_text() if await self.page.locator('#gvfront_ctl02_lblWaterTax').count() > 0 else ""
            data['tax'] = await self.page.locator('#gvfront_ctl02_lblTax').inner_text() if await self.page.locator('#gvfront_ctl02_lblTax').count() > 0 else ""
            data['ses'] = await self.page.locator('#gvfront_ctl02_lblSes').inner_text() if await self.page.locator('#gvfront_ctl02_lblSes').count() > 0 else ""
            data['other_ses'] = await self.page.locator('#gvfront_ctl02_lblOtherses').inner_text() if await self.page.locator('#gvfront_ctl02_lblOtherses').count() > 0 else ""
            data['total'] = await self.page.locator('#gvfront_ctl02_lblTotal').inner_text() if await self.page.locator('#gvfront_ctl02_lblTotal').count() > 0 else ""
            data['description'] = await self.page.locator('#gvfront_ctl02_lblDescription').inner_text() if await self.page.locator('#gvfront_ctl02_lblDescription').count() > 0 else ""
            
            # Extract special case information
            data['special_case'] = await self.page.locator('#gvfront_ctl02_lblSpecialCase').inner_text() if await self.page.locator('#gvfront_ctl02_lblSpecialCase').count() > 0 else ""
            data['last_publish_date'] = await self.page.locator('#gvfront_ctl02_lblLastPublishDate').inner_text() if await self.page.locator('#gvfront_ctl02_lblLastPublishDate').count() > 0 else ""
            data['tax_date'] = await self.page.locator('#gvfront_ctl02_lblTaxDate').inner_text() if await self.page.locator('#gvfront_ctl02_lblTaxDate').count() > 0 else ""

            form99 = await self._extract_form99_header_fields()
            for field in ('form_no', 'parichheda'):
                if form99.get(field) and not data.get(field):
                    data[field] = form99[field]
            
        except Exception as e:
            logger.error(f"Error extracting front page data: {e}")
        
        return data
    
    async def extract_back_page_data(self) -> List[Dict]:
        """Extract plot data from the back page of RoR.

        Uses a single JavaScript evaluation to extract ALL rows at once,
        avoiding thousands of individual browser round-trips that would
        cause timeouts on large khatiyans (750+ rows).

        Tries multiple table IDs and scores fallback candidates by plot markers.
        """
        plots: List[Dict] = []

        try:
            result = await self.page.evaluate(_EXTRACT_PLOTS_JS)
            plots = result.get('plots', []) if isinstance(result, dict) else (result or [])
            table_id = result.get('tableId', '') if isinstance(result, dict) else ''
            row_count = result.get('rowCount', 0) if isinstance(result, dict) else 0

            if plots:
                logger.info(
                    f"Extracted {len(plots)} plots via JS bulk extraction"
                    + (f" (table={table_id}, rows={row_count})" if table_id else "")
                )
            else:
                try:
                    table_info = await self.page.evaluate("""
                        () => {
                            const tables = document.querySelectorAll('table[id]');
                            return Array.from(tables).map(t => ({
                                id: t.id || '(no id)',
                                rows: t.querySelectorAll('tr').length,
                                plotMarkers: t.querySelectorAll(
                                    '[id*="lblPlotNo"], [id*="lblPlotcni"], [id*="lblPlotci"]'
                                ).length,
                            })).filter(t => t.rows > 1)
                             .sort((a, b) => b.plotMarkers - a.plotMarkers)
                             .slice(0, 5);
                        }
                    """)
                    logger.warning(
                        f"No plots extracted. Top tables by plot markers: {table_info}"
                    )
                except Exception:
                    logger.warning("No plots extracted and couldn't inspect page tables")

        except Exception as e:
            logger.error(f"Error extracting back page data: {e}")

        return plots
    
    async def click_view_ror(self):
        """Click the View RoR button and wait for RoR content (not networkidle)."""
        try:
            button = self.page.locator('input[id*="btnRORFront"]')
            await button.hover()
            await self.human_delay(0.15, 0.4)

            await button.click()

            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except PlaywrightTimeoutError:
                pass

            if await self.wait_for_ror_view_ready():
                logger.info("Clicked View RoR button")
                return

            logger.warning(
                "RoR view not ready after click — falling back to full page load",
            )
            await self.wait_for_page_load(timeout=15000)

            if await self.check_for_timeout_error():
                raise Exception("Timeout error after clicking View RoR")

            if await self.wait_for_ror_view_ready(timeout_ms=8000):
                logger.info("Clicked View RoR button (fallback load)")
                return

            raise Exception("RoR view did not load after View RoR click")
        except Exception as e:
            logger.error(f"Error clicking View RoR button: {e}")
            raise
    
    async def click_khatiyan_page(self):
        """Click the Khatiyan Page button to go back to the khatiyan selector."""
        try:
            await self.page.wait_for_selector('input[id="btnKhatiyan"]', timeout=10000)

            button = self.page.locator('input[id="btnKhatiyan"]')
            await button.hover()
            await self.human_delay(0.15, 0.35)

            await button.click()

            # Fast path: dropdowns remain populated after back — skip networkidle.
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
            except PlaywrightTimeoutError:
                pass

            if await self.wait_for_khatiyan_selector_ready():
                logger.info("Clicked Khatiyan Page button (back)")
                return

            logger.warning(
                "Khatiyan selector not ready after back — falling back to full page load",
            )
            await self.wait_for_page_load(timeout=15000)

            if await self.check_for_timeout_error():
                logger.warning(
                    "Timeout error after clicking Khatiyan Page, navigating to main page...",
                )
                await self.navigate_to_ror_page()
                return

            await self.human_delay(0.2, 0.4)
            logger.info("Clicked Khatiyan Page button (back, fallback load)")
        except Exception as e:
            logger.error(f"Error clicking Khatiyan Page button: {e}")
            try:
                await self.navigate_to_ror_page()
            except Exception:
                raise
    
    async def _reselect_district_tahasil_village(self, tahasil_value: str, village_value: str) -> None:
        """Re-select district, tahasil, village and Khatiyan search type (e.g. after navigate_to_ror_page on retry)."""
        district_value = self._current_district_value or ""
        await self.select_dropdown(SELECTOR_DISTRICT, district_value, wait_for_update=True)
        await self.select_dropdown(SELECTOR_TAHASIL, tahasil_value, wait_for_update=True)
        await self.select_dropdown(SELECTOR_VILLAGE, village_value, wait_for_update=True)
        await self.select_search_type("Khatiyan")
        await self.human_delay(0.5, 1.0)

    async def _wait_for_khatiyan_dropdown_with_retries(
        self, village_value: str, village_text: str, tahasil_value: str,
        expected_khatiyan_count: Optional[int] = None,
    ) -> None:
        """Wait for Khatiyan dropdown; reload and reselect district/tahasil/village between retries."""
        attempts: List[tuple] = [
            (25000, False),
            (30000, True),
            (45000, True),
        ]
        for attempt_idx, (timeout_ms, reload) in enumerate(attempts, start=1):
            if reload:
                logger.warning(
                    "Khatiyan dropdown not ready for village %s (attempt %d/%d), reloading page…",
                    village_text, attempt_idx, len(attempts),
                )
                await self.navigate_to_ror_page()
                await self._reselect_district_tahasil_village(tahasil_value, village_value)
            else:
                logger.info(
                    "Waiting for Khatiyan dropdown to stabilize (attempt %d/%d, timeout %dms, expected %s)…",
                    attempt_idx, len(attempts), timeout_ms,
                    expected_khatiyan_count if expected_khatiyan_count is not None else "unknown",
                )

            try:
                count = await self.wait_for_dropdown_stable(
                    SELECTOR_KHATIYAN,
                    expected_count=expected_khatiyan_count,
                    timeout_ms=timeout_ms,
                )
                if count > 0 or expected_khatiyan_count == 0:
                    return
                logger.warning(
                    "Khatiyan dropdown stabilized at 0 options for village %s",
                    village_text,
                )
            except TimeoutError as e:
                logger.warning(
                    "Khatiyan dropdown wait timed out for village %s (attempt %d/%d): %s",
                    village_text, attempt_idx, len(attempts), e,
                )

        raise Exception(
            f"Khatiyan dropdown did not populate for village {village_text} "
            f"after {len(attempts)} attempts"
        )

    async def process_khatiyan(self, khatiyan_value: str, khatiyan_text: str,
                               district: str, tahasil: str, village: str,
                               tahasil_value: str = "", village_value: str = "") -> bool:
        """Process a single Khatiyan: select it, view RoR, extract data, and go back."""
        max_retries = len(_KHATIYAN_RETRY_BACKOFF) + 1   # 5 total attempts
        for attempt in range(max_retries):
            try:
                # Select the Khatiyan
                await self.select_dropdown(SELECTOR_KHATIYAN, khatiyan_value, wait_for_update=False)
                await self.human_delay(0.3, 0.6)
                
                await self.click_view_ror()

                # Check for timeout error before extracting
                if await self.check_for_timeout_error():
                    if attempt < max_retries - 1:
                        wait = _KHATIYAN_RETRY_BACKOFF[min(attempt, len(_KHATIYAN_RETRY_BACKOFF) - 1)]
                        logger.warning(
                            "Website timeout screen on Khatiyan %s (attempt %d/%d), retrying in %ds…",
                            khatiyan_value, attempt + 1, max_retries, wait,
                        )
                        await asyncio.sleep(wait)
                        await self.navigate_to_ror_page()
                        await self._reselect_district_tahasil_village(tahasil_value, village_value)
                        continue
                    else:
                        raise Exception("Website timeout error persists after all retries")

                # Wait for RoR view content — if the RoR page elements never
                # appear, the "View RoR" click likely didn't navigate away from
                # the search form.  Extracting from the search form produces an
                # all-empty record, so we MUST retry instead of proceeding.
                front_loaded = False
                try:
                    await self.page.wait_for_selector(
                        SELECTOR_ROR_VIEW_READY,
                        state="visible", timeout=5000,
                    )
                    front_loaded = True
                except Exception as e:
                    logger.warning(f"Front page elements not found after 5s: {e}")

                if not front_loaded:
                    try:
                        if await self._is_form20_ror():
                            front_loaded = True
                            logger.info("Form-20 RoR layout detected (survey/settlement)")
                    except Exception:
                        pass

                if not front_loaded:
                    if attempt < max_retries - 1:
                        wait = _KHATIYAN_RETRY_BACKOFF[min(attempt, len(_KHATIYAN_RETRY_BACKOFF) - 1)]
                        logger.warning(
                            "RoR page did NOT load for Khatiyan %s (attempt %d/%d) — "
                            "retrying in %ds with full page reload…",
                            khatiyan_text, attempt + 1, max_retries, wait,
                        )
                        await asyncio.sleep(wait)
                        await self.navigate_to_ror_page()
                        await self._reselect_district_tahasil_village(tahasil_value, village_value)
                        continue
                    else:
                        logger.error(
                            "RoR page never loaded for Khatiyan %s after %d attempts — skipping",
                            khatiyan_text, max_retries,
                        )
                        return False
                
                await self.human_delay(0.1, 0.25)
                
                # Scroll to bottom to trigger lazy loading of back table (plot data)
                # This ensures gvRorBack is loaded even if it's below the fold
                back_table_found = False
                try:
                    await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await self.human_delay(0.3, 0.5)
                    # Try to wait for back table specifically
                    await self.page.wait_for_selector("#gvRorBack, #gvRorBack2, #gvplotdetail, table[id*='RorBack']", state="visible", timeout=5000)
                    back_table_found = True
                except Exception:
                    # Back table not found - try scrolling again and looking for any table
                    try:
                        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await self.human_delay(0.5, 0.8)
                        # Look for any table that might contain plot data
                        tables = await self.page.locator('table[id*="gv"], table[id*="Ror"]').count()
                        if tables > 0:
                            back_table_found = True
                            logger.info(f"Found {tables} potential data tables via fallback")
                    except:
                        pass
                
                # Log loading status for debugging
                logger.debug(f"Page load status: front={front_loaded}, back_table={back_table_found}")

                # HTML will be captured only if extraction has issues (to avoid DB bloat)
                html_content = None
                
                # Extract data (with timeout protection for large khatiyans)
                logger.info(f"Starting extraction for Khatiyan {khatiyan_text}...")
                extract_start = time.time()
                try:
                    ror_data = await asyncio.wait_for(
                        self.extract_ror_data(),
                        timeout=600.0  # 10 minute timeout (JS bulk extraction is fast, but safety margin)
                    )
                except asyncio.TimeoutError:
                    logger.error(f"EXTRACTION TIMEOUT: Khatiyan {khatiyan_text} took >10min to extract")
                    raise Exception(f"Data extraction timed out for Khatiyan {khatiyan_text}")
                extract_elapsed = time.time() - extract_start
                
                # Validate extraction results
                plots_count = len(ror_data.get('plots', []))
                filled_fields = sum(1 for k, v in ror_data.items() if v and v != [] and k != 'ror_type')
                
                logger.info(f"Extraction completed for Khatiyan {khatiyan_text} in {extract_elapsed:.1f}s - {plots_count} plots, {filled_fields} fields")
                
                # Warn if back table was found but no plots extracted (potential bug)
                if back_table_found and plots_count == 0:
                    try:
                        no_plots_msg = await self.page.locator(
                            '[id*="lblKisama"]:has-text("ଉପଲବ୍ଧ ନାହିଁ")'
                        ).count() > 0
                    except Exception:
                        no_plots_msg = False
                    if not no_plots_msg:
                        logger.warning(
                            f"⚠️ POTENTIAL BUG: Back table found but 0 plots extracted for Khatiyan {khatiyan_text}"
                        )
                
                # Warn if almost nothing was extracted (page might not have loaded properly)
                if filled_fields < 3 and plots_count == 0:
                    logger.warning(f"⚠️ SPARSE DATA: Only {filled_fields} fields extracted for Khatiyan {khatiyan_text}")
                
                # Only capture HTML if extraction has issues (to avoid DB bloat)
                # Issues: no plots, or plots missing critical fields
                has_issues = False
                if plots_count == 0:
                    has_issues = True
                else:
                    for plot in ror_data.get('plots', []):
                        # Check for missing plot number
                        if not plot.get('plot_no', '').strip():
                            has_issues = True
                            break
                        # Check for empty area (but skip "no plots" messages)
                        acre = plot.get('acre', '').strip()
                        decimil = plot.get('decimil', '').strip()
                        hector = plot.get('hector', '').strip()
                        kisam = plot.get('kisam', '')
                        if not acre and not decimil and not hector and 'ଉପଲବ୍ଧ ନାହିଁ' not in kisam:
                            has_issues = True
                            break
                
                if has_issues:
                    try:
                        html_content = await self.page.content()
                        logger.info(f"Captured HTML for review: Khatiyan {khatiyan_text} (extraction issues detected)")
                    except Exception as html_err:
                        logger.warning(f"Failed to capture HTML: {html_err}")
                
                # Add metadata
                ror_data['district'] = district
                ror_data['tahasil'] = tahasil
                ror_data['village'] = village
                ror_data['khatiyan_value'] = khatiyan_value
                ror_data['khatiyan_text'] = khatiyan_text
                self._record_layout_stat(ror_data.get('ror_type', 'type1'))

                plots = ror_data.get('plots') or []
                landlord = (ror_data.get('landlord_name') or '').strip()
                tenant = (ror_data.get('tenant_name') or '').strip()
                if not plots and not landlord and not tenant:
                    logger.warning(
                        "Empty extraction for Khatiyan %s (no plots, landlord, or tenant) — skipping save",
                        khatiyan_text,
                    )
                    await self.click_khatiyan_page()
                    return False
                
                # Persistent storage: append immediately so no data loss on crash.
                # Do this BEFORE incrementing khatiyans_processed — if storage fails
                # the count must not increase, otherwise villages get marked "done"
                # with khatiyans_fetched > 0 but nothing in the DB.
                if self._current_storage:
                    self._current_storage.append_khatiyan(ror_data, html_content=html_content)
                    self._current_storage.set_checkpoint(
                        self._current_district_value or "",
                        self._current_district_text or "",
                        tahasil_value or tahasil,
                        tahasil,
                        village_value or village,
                        village,
                        khatiyan_value,
                        khatiyan_text,
                        self._current_storage.get_khatiyan_count(),
                    )

                # Only count as processed AFTER storage succeeded
                self.data_list.append(ror_data)
                self.khatiyans_processed += 1
                self._last_khatiyan_no = khatiyan_value.strip() if khatiyan_value else khatiyan_value
                total = len(self.data_list)
                logger.info(f"Processed Khatiyan: {khatiyan_text} (Value: {khatiyan_value}) | Records: {total}")
                # In dry-run/limit mode, save after each record so you see active file changes
                if self.limit_khatiyans is not None:
                    await self.save_data()
                    logger.info(f"File updated: {total} record(s) in bhulekh_data.json / bhulekh_data.csv")
                
                await self.click_khatiyan_page()
                return True
                
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = _KHATIYAN_RETRY_BACKOFF[min(attempt, len(_KHATIYAN_RETRY_BACKOFF) - 1)]
                    logger.warning(
                        "Error on Khatiyan %s (attempt %d/%d), retrying in %ds: %s",
                        khatiyan_value, attempt + 1, max_retries, wait, e,
                    )
                    await asyncio.sleep(wait)
                    try:
                        await self.navigate_to_ror_page()
                        await self._reselect_district_tahasil_village(tahasil_value, village_value)
                    except Exception as nav_e:
                        logger.warning("Reselect after error failed: %s", nav_e)
                else:
                    logger.error(
                        "Khatiyan %s failed after %d attempts: %s",
                        khatiyan_value, max_retries, e,
                    )
                    return False
        return False
    
    async def process_village(self, village_value: str, village_text: str,
                             district: str, tahasil: str,
                             tahasil_value: str = "",
                             start_after_khatiyan_value: Optional[str] = None,
                             expected_khatiyan_count: Optional[int] = None,
                             skip_khatiyan_values: Optional[set] = None) -> bool:
        """Process all Khatiyans in a village."""
        try:
            # Select village (triggers postback; Khatiyan dropdown will populate)
            await self.select_dropdown(SELECTOR_VILLAGE, village_value)
            
            await self._wait_for_khatiyan_dropdown_with_retries(
                village_value, village_text, tahasil_value,
                expected_khatiyan_count=expected_khatiyan_count,
            )
            await self.human_delay(0.2, 0.4)
            
            # Get all Khatiyans
            khatiyan_options = await self.get_dropdown_options(SELECTOR_KHATIYAN)
            
            if not khatiyan_options:
                raise Exception(
                    f"No Khatiyans found for village {village_text} despite populated dropdown"
                )
            
            # Resume: skip khatiyans until we're past the checkpoint
            start_index = 0
            if start_after_khatiyan_value:
                for i, k in enumerate(khatiyan_options):
                    if (k.get("value") or "").strip() == (start_after_khatiyan_value or "").strip():
                        start_index = i + 1
                        logger.info(f"Resuming after Khatiyan value {start_after_khatiyan_value!r}; skipping {start_index} khatiyans")
                        break

            skipped_existing = 0
            if skip_khatiyan_values:
                logger.info(
                    "Village %s: %d khatiyans already in DB, will skip them",
                    village_text, len(skip_khatiyan_values),
                )

            slice_options = khatiyan_options[start_index:]
            if skip_khatiyan_values:
                slice_options = [
                    k for k in slice_options
                    if (k.get("value") or "") not in skip_khatiyan_values
                ]
            # Position resume can overshoot: checkpoint at end while gaps remain earlier.
            if not slice_options and skip_khatiyan_values and start_index > 0:
                slice_options = [
                    k for k in khatiyan_options
                    if (k.get("value") or "") not in skip_khatiyan_values
                ]
                if slice_options:
                    logger.warning(
                        "Village %s: position resume left 0 work but %d khatiyans missing — "
                        "rescanning dropdown by value",
                        village_text, len(slice_options),
                    )

            logger.info(
                "Processing %d Khatiyans for village: %s",
                len(slice_options), village_text,
            )

            # Process each Khatiyan
            for khatiyan in slice_options:
                if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                    break
                # Re-select village if needed (after coming back from RoR page)
                current_village = await self.page.evaluate("document.querySelector('#ctl00_ContentPlaceHolder1_ddlVillage')?.value || document.querySelector('select[id*=\"ddlVillage\"]')?.value || ''")
                if current_village != village_value:
                    await self.select_dropdown(SELECTOR_VILLAGE, village_value)
                    await asyncio.sleep(max(0.05, 0.8 * self.delay_scale))
                
                success = await self.process_khatiyan(
                    khatiyan['value'],
                    khatiyan['text'],
                    district,
                    tahasil,
                    village_text,
                    tahasil_value=tahasil_value,
                    village_value=village_value,
                )
                
                if not success:
                    logger.warning(f"Failed to process Khatiyan: {khatiyan['text']}")
                if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                    break
                await self.human_delay(0.4, 0.8)
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing village {village_value}: {e}")
            raise
    
    async def process_tahasil(self, tahasil_value: str, tahasil_text: str,
                             district: str, start_village: Optional[str] = None,
                             start_after_khatiyan_value: Optional[str] = None) -> bool:
        """Process all villages in a Tahasil."""
        try:
            # Select Tahasil (triggers postback; Village dropdown will populate)
            await self.select_dropdown(SELECTOR_TAHASIL, tahasil_value)
            
            # Wait for Village dropdown to be populated by server
            logger.info("Waiting for Village dropdown to populate...")
            if not await self.wait_for_dropdown_populated(SELECTOR_VILLAGE, min_options=1, timeout_ms=25000):
                logger.warning(f"Village dropdown did not populate for Tahasil {tahasil_text}, skipping")
                return True
            await self.human_delay(0.2, 0.4)
            
            # Get all villages
            village_options = await self.get_dropdown_options(SELECTOR_VILLAGE)
            
            if not village_options:
                logger.warning(f"No villages found for Tahasil: {tahasil_text}")
                return True  # Continue to next Tahasil
            
            # Determine starting point (for resume)
            start_index = 0
            if start_village:
                for i, village in enumerate(village_options):
                    if village['value'] == start_village or village['text'] == start_village:
                        start_index = i
                        break

            logger.info(f"Processing {len(village_options) - start_index} villages for Tahasil: {tahasil_text}")

            # Process each village
            for idx, village in enumerate(village_options[start_index:]):
                # Re-select Tahasil if needed (after coming back from RoR page)
                current_tahasil = await self.page.evaluate("document.querySelector('#ctl00_ContentPlaceHolder1_ddlTahsil')?.value || document.querySelector('select[id*=\"ddlTahsil\"]')?.value || ''")
                if current_tahasil != tahasil_value:
                    await self.select_dropdown(SELECTOR_TAHASIL, tahasil_value)
                    await asyncio.sleep(max(0.05, 0.8 * self.delay_scale))

                # Only pass start_after_khatiyan for the first village when resuming
                khatiyan_resume = start_after_khatiyan_value if (start_index == 0 and idx == 0) else None
                try:
                    success = await self.process_village(
                        village['value'],
                        village['text'],
                        district,
                        tahasil_text,
                        tahasil_value=tahasil_value,
                        start_after_khatiyan_value=khatiyan_resume,
                    )
                except Exception as e:
                    logger.warning(f"Failed to process village: {village['text']}: {e}")
                    success = False
                if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                    break
                await self.human_delay(0.5, 1.0)
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing Tahasil {tahasil_value}: {e}")
            return False
    
    async def process_district(self, district_value: str, district_text: str,
                              start_tahasil: Optional[str] = None,
                              start_village: Optional[str] = None,
                              start_after_khatiyan_value: Optional[str] = None) -> bool:
        """Process all Tahasils in a District."""
        storage: Optional[BhulekhStorageBase] = None
        try:
            # Persistent storage for this district (file: .db or .ndjson)
            if self.data_dir:
                storage = create_storage(self.data_dir, district_text, backend=self.storage_backend)
                self._current_storage = storage
                self._current_district_value = district_value
                self._current_district_text = district_text
                if self.resume:
                    cp = storage.get_checkpoint()
                    if cp and cp.get("district_value") == district_value:
                        start_tahasil = start_tahasil or cp.get("tahasil_value")
                        start_village = start_village or cp.get("village_value")
                        start_after_khatiyan_value = cp.get("last_khatiyan_value")
                        logger.info(f"Resuming district {district_text}: from tahasil={start_tahasil}, village={start_village}, after khatiyan={start_after_khatiyan_value!r}")

            # Navigate to the page if not already there
            if self.page.url != self.start_url:
                await self.navigate_to_ror_page()

            # Select district
            await self.select_dropdown(SELECTOR_DISTRICT, district_value)
            
            # Select search type as Khatiyan
            await self.select_search_type("Khatiyan")
            
            # Wait for Tahasils to populate (human-like delay)
            logger.info("Waiting for Tahsil dropdown to populate...")
            if not await self.wait_for_dropdown_populated(SELECTOR_TAHASIL, min_options=1, timeout_ms=25000):
                raise Exception("Tahsil dropdown did not populate after selecting district")
            await self.human_delay(0.2, 0.4)
            
            # Get all Tahasils
            tahasil_options = await self.get_dropdown_options(SELECTOR_TAHASIL)
            
            if not tahasil_options:
                logger.warning(f"No Tahasils found for District: {district_text}")
                return True  # Continue to next District
            
            # Determine starting point
            start_index = 0
            if start_tahasil:
                for i, tahasil in enumerate(tahasil_options):
                    if tahasil['value'] == start_tahasil or tahasil['text'] == start_tahasil:
                        start_index = i
                        break
            
            logger.info(f"Processing {len(tahasil_options) - start_index} Tahasils for District: {district_text}")
            
            # Process each Tahasil
            for tahasil in tahasil_options[start_index:]:
                # Check if we need to start from a specific village
                start_village_value = None
                if start_tahasil and tahasil['value'] == start_tahasil and start_village:
                    start_village_value = start_village
                
                # Pass resume khatiyan only for the first tahasil when resuming
                khatiyan_resume = start_after_khatiyan_value if (start_tahasil and tahasil['value'] == start_tahasil) else None
                success = await self.process_tahasil(
                    tahasil['value'],
                    tahasil['text'],
                    district_text,
                    start_village_value,
                    start_after_khatiyan_value=khatiyan_resume,
                )
                
                if not success:
                    logger.warning(f"Failed to process Tahasil: {tahasil['text']}")
                if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                    break
                # Reset start_village after first Tahasil
                start_village_value = None
                
                await self.human_delay(0.8, 1.2)
            
            return True

        except Exception as e:
            logger.error(f"Error processing District {district_value}: {e}")
            return False
        finally:
            if storage:
                storage.close()
                self._current_storage = None
                self._current_district_value = None
                self._current_district_text = None
    
    @staticmethod
    async def _check_spot_termination() -> bool:
        """
        Returns True if an AWS Spot Instance termination notice is active.
        Polls the EC2 instance metadata endpoint (only reachable inside AWS).
        On non-AWS machines or network errors this always returns False.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(
                    "http://169.254.169.254/latest/meta-data/spot/termination-time"
                )
                return r.status_code == 200
        except Exception:
            return False

    async def _restart_browser(self, headless: bool = True) -> bool:
        """
        Fully close and reopen the browser.

        Called after a run of consecutive village failures to recover from stale
        browser / connection state.  Retries navigation with exponential backoff.
        Returns True on success, False if it still cannot reach the site.
        """
        logger.warning("Restarting browser (full restart)…")
        try:
            await self.cleanup()
        except Exception:
            pass
        await asyncio.sleep(5)

        for attempt in range(5):
            try:
                await self.init_browser(headless=headless)
                await self.navigate_to_ror_page()
                logger.info("Browser restarted and site reachable.")
                return True
            except Exception as e:
                wait = _SITE_BACKOFF[min(attempt, len(_SITE_BACKOFF) - 1)]
                logger.warning(
                    "Browser restart attempt %d/5 failed (%s), waiting %ds…",
                    attempt + 1, e, wait,
                )
                try:
                    await self.cleanup()
                except Exception:
                    pass
                await asyncio.sleep(wait)

        logger.error("Browser restart failed after 5 attempts — will keep trying on next village claim.")
        return False

    async def cleanup(self):
        """Clean up browser resources."""
        try:
            await asyncio.sleep(0.2)
            
            if self.page:
                try:
                    await self.page.close()
                except:
                    pass
                self.page = None
            
            if self.context:
                try:
                    # For persistent context, close all pages first
                    if self.use_persistent_context and hasattr(self.context, 'pages'):
                        for page in self.context.pages:
                            try:
                                await page.close()
                            except:
                                pass
                    await self.context.close()
                except:
                    pass
                self.context = None
            
            if self.browser:
                try:
                    await self.browser.close()
                except:
                    pass
                self.browser = None
            
            if self.playwright:
                try:
                    await self.playwright.stop()
                except:
                    pass
                self.playwright = None
            
            logger.info("Browser resources cleaned up")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")
    
    async def run(self, district: Optional[str] = None,
                 tahasil: Optional[str] = None,
                 village: Optional[str] = None,
                 headless: bool = False):
        """Main run method to start the scraping process."""
        try:
            self.khatiyans_processed = 0
            if self.limit_khatiyans is not None:
                logger.info(f"Dry run: will stop after {self.limit_khatiyans} Khatiyan(s). File will update after each record.")
            await self.init_browser(headless=headless)
            
            # Add initial delay to simulate human behavior
            logger.info("Initializing session...")
            await self.human_delay(0.5, 1.0)
            
            # Navigate to the page
            await self.navigate_to_ror_page()
            
            # Verify we can see the district dropdown
            try:
                district_dropdown = await self.page.locator(SELECTOR_DISTRICT).count()
                if district_dropdown == 0:
                    raise Exception("District dropdown not found - page may be blocking automation")
                logger.info("Successfully verified page loaded correctly")
            except Exception as e:
                logger.error(f"Page verification failed: {e}")
                # Try to get page screenshot for debugging
                try:
                    await self.page.screenshot(path='error_screenshot.png')
                    logger.info("Saved error screenshot to error_screenshot.png")
                except:
                    pass
                raise
            
            if district:
                # Find district by value or text
                district_options = await self.get_dropdown_options(SELECTOR_DISTRICT)
                district_value = None
                district_text = None
                
                for dist in district_options:
                    if dist['value'] == district or dist['text'] == district:
                        district_value = dist['value']
                        district_text = dist['text']
                        break
                
                if not district_value:
                    logger.error(f"District not found: {district}")
                    return
                
                await self.process_district(district_value, district_text, tahasil, village)
            elif getattr(self, '_districts_to_run', None):
                # Multi-worker: process only these (value, text) districts
                for district_value, district_text in self._districts_to_run:
                    await self.process_district(district_value, district_text)
                    if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                        break
                    await self.human_delay(1.0, 2.0)
            else:
                # Process all districts
                district_options = await self.get_dropdown_options(SELECTOR_DISTRICT)
                
                logger.info(f"Processing {len(district_options)} districts")
                
                for dist in district_options:
                    await self.process_district(dist['value'], dist['text'])
                    if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                        logger.info(f"Reached limit of {self.limit_khatiyans} Khatiyan(s). Stopping.")
                        break
                    await self.human_delay(1.0, 2.0)
            
            # Save in-memory data only when not using persistent storage (SQLite per district)
            if not self.data_dir:
                await self.save_data()
            
        except Exception as e:
            logger.error(f"Error in main run: {e}")
            raise
        finally:
            await self.cleanup()
    
    async def save_data(self, filename: str = "bhulekh_data.json"):
        """Save collected data to JSON and CSV files."""
        try:
            # Save as JSON
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(self.data_list, f, ensure_ascii=False, indent=2)
            
            logger.info(f"Saved {len(self.data_list)} records to {filename}")
            # Convert to DataFrame and save as CSV
            if self.data_list:
                # Flatten the data for CSV
                flattened_data = []
                for record in self.data_list:
                    base_record = {k: v for k, v in record.items() if k != 'plots'}
                    if 'plots' in record and record['plots']:
                        for plot in record['plots']:
                            row = base_record.copy()
                            row.update({f'plot_{k}': v for k, v in plot.items()})
                            flattened_data.append(row)
                    else:
                        flattened_data.append(base_record)
                
                df = pd.DataFrame(flattened_data)
                csv_filename = filename.replace('.json', '.csv')
                df.to_csv(csv_filename, index=False, encoding='utf-8-sig')
                logger.info(f"Saved data to {csv_filename}")
                
        except Exception as e:
            logger.error(f"Error saving data: {e}")


    async def run_from_queue(
        self,
        queue_path: str,
        headless: bool = True,
        worker_id: Optional[str] = None,
        district_codes: Optional[List[int]] = None,
    ) -> None:
        """
        Village-queue runner.

        Continuously claims the next pending village from work_queue.db,
        scrapes all its khatiyans, then marks it done.  Safe to run in
        multiple processes simultaneously — SQLite atomic UPDATE prevents
        two workers from claiming the same village.

        Resume: if a worker was interrupted mid-village the village is
        reclaimed automatically (CLAIM_TIMEOUT_SECONDS has elapsed) and
        the scraper picks up from last_khatiyan_no stored in the queue.
        """
        import socket, os
        from work_queue import make_queue, claim_village, complete_village, fail_village, checkpoint_village, heartbeat

        if worker_id is None:
            worker_id = f"{socket.gethostname()}-{os.getpid()}"

        # Support both local SQLite path and remote queue server URL
        _queue = make_queue(queue_path, api_key=getattr(self, '_queue_api_key', None))
        _is_remote = hasattr(_queue, 'claim_village')

        def _claim():
            return (_queue.claim_village(worker_id=worker_id, district_codes=district_codes)
                    if _is_remote
                    else claim_village(queue_path, worker_id=worker_id, district_codes=district_codes))

        def _complete(vid, n):
            return _queue.complete_village(vid, n) if _is_remote else complete_village(queue_path, vid, n)

        def _fail(vid, err):
            return _queue.fail_village(vid, err) if _is_remote else fail_village(queue_path, vid, err)

        def _heartbeat(vid):
            return _queue.heartbeat(vid) if _is_remote else heartbeat(queue_path, vid)

        def _checkpoint(vid, n, last_kh):
            return (_queue.checkpoint_village(vid, n, last_kh)
                    if _is_remote
                    else checkpoint_village(queue_path, vid, n, last_kh))

        # ── Initial browser start with retry ──────────────────────────────────
        for _init_attempt in range(5):
            try:
                await self.init_browser(headless=headless)
                await self.navigate_to_ror_page()
                break
            except Exception as _e:
                _wait = _SITE_BACKOFF[min(_init_attempt, len(_SITE_BACKOFF) - 1)]
                logger.warning(
                    "Worker %s: initial navigation failed (attempt %d/5), waiting %ds: %s",
                    worker_id, _init_attempt + 1, _wait, _e,
                )
                try:
                    await self.cleanup()
                except Exception:
                    pass
                if _init_attempt == 4:
                    raise
                await asyncio.sleep(_wait)

        logger.info("Worker %s: ready, drawing villages from queue %s", worker_id, queue_path)

        _consecutive_failures = 0   # tracks how many villages in a row have failed
        _villages_done = 0          # for periodic browser restart

        while True:
            # ── Spot termination check ─────────────────────────────────────────
            if await self._check_spot_termination():
                logger.critical(
                    "Worker %s: AWS Spot termination notice received — "
                    "releasing any claimed village and exiting cleanly.",
                    worker_id,
                )
                break

            village_info = _claim()
            if village_info is None:
                logger.info("Worker %s: no more pending villages — done.", worker_id)
                break

            v_id      = village_info["id"]
            d_code    = str(village_info["district_code"])
            d_name    = village_info["district_name"]
            tah_code  = str(village_info["tahasil_code"])
            tah_name  = village_info["tahasil_name"]
            vil_code  = str(village_info["village_code"])
            vil_name  = village_info["village_name"]
            resume_kh = village_info.get("last_khatiyan_no")  # None or a khatiyan value

            logger.info(
                "Worker %s: claiming village %s (%s) | tahasil %s | district %s",
                worker_id, vil_name, vil_code, tah_name, d_name,
            )

            village_ok = False
            self._last_khatiyan_no = None   # reset per-village checkpoint tracker
            try:
                # Set up persistent storage for this district
                if self.data_dir:
                    from storage import create_storage
                    storage = create_storage(
                        self.data_dir, d_name, backend=self.storage_backend
                    )
                    self._current_storage = storage
                    self._current_district_value = d_code
                    self._current_district_text = d_name

                # Navigate to page if needed
                if self.page.url != self.start_url:
                    await self.navigate_to_ror_page()

                # Select district → tahasil → village in browser
                await self.select_dropdown(SELECTOR_DISTRICT, d_code)
                await self.select_search_type("Khatiyan")
                if not await self.wait_for_dropdown_populated(SELECTOR_TAHASIL, min_options=1, timeout_ms=25000):
                    raise Exception(f"Tahasil dropdown did not populate for district {d_code}")
                await self.human_delay(0.2, 0.4)

                await self.select_dropdown(SELECTOR_TAHASIL, tah_code)
                if not await self.wait_for_dropdown_populated(SELECTOR_VILLAGE, min_options=1, timeout_ms=25000):
                    raise Exception(f"Village dropdown did not populate for tahasil {tah_code}")
                await self.human_delay(0.2, 0.4)

                # Count khatiyans already done for this village (from previous attempt)
                already_done = village_info.get("khatiyans_fetched", 0) or 0
                expected_kh = village_info.get("khatiyan_count") or 0

                # Corrupt checkpoint: last_khatiyan_no near end but count still low →
                # resume skips all khatiyans, fails threshold, retries forever.
                if resume_kh and _checkpoint_looks_corrupt(resume_kh, already_done, expected_kh):
                    logger.warning(
                        "Worker %s: clearing corrupt checkpoint for %s "
                        "(last_khatiyan=%r, fetched=%d, expected=%d)",
                        worker_id, vil_name, resume_kh, already_done, expected_kh,
                    )
                    _checkpoint(v_id, already_done, "")
                    resume_kh = None

                # Scale per-village timeout for large villages (545 khatiyans need >20 min)
                village_timeout = _VILLAGE_TIMEOUT
                if expected_kh > 100:
                    village_timeout = max(_VILLAGE_TIMEOUT, expected_kh * 4)

                # Heartbeat task so the village isn't reclaimed while we work
                async def _heartbeat_loop():
                    while True:
                        await asyncio.sleep(120)
                        _heartbeat(v_id)

                # Periodic checkpoint task — saves progress every 60s so nothing is lost on timeout
                async def _checkpoint_loop():
                    while True:
                        await asyncio.sleep(60)
                        if self.khatiyans_processed > 0 and self._last_khatiyan_no:
                            kh_so_far = already_done + self.khatiyans_processed
                            _checkpoint(v_id, kh_so_far, self._last_khatiyan_no)
                            logger.debug(
                                "Worker %s: checkpoint %d khatiyans for village %s",
                                worker_id, kh_so_far, vil_name,
                            )

                hb_task = asyncio.create_task(_heartbeat_loop())
                cp_task = asyncio.create_task(_checkpoint_loop())
                try:
                    # Hard per-village timeout — if the site hangs, abort & move on
                    await asyncio.wait_for(
                        self.process_village(
                            village_value=vil_code,
                            village_text=vil_name,
                            district=d_name,
                            tahasil=tah_name,
                            tahasil_value=tah_code,
                            start_after_khatiyan_value=resume_kh,
                            expected_khatiyan_count=expected_kh or None,
                        ),
                        timeout=village_timeout,
                    )
                except asyncio.TimeoutError:
                    raise Exception(
                        f"Village timed out after {village_timeout}s — site likely unresponsive"
                    )
                finally:
                    hb_task.cancel()
                    cp_task.cancel()

                kh_done = already_done + self.khatiyans_processed
                expected = village_info.get("khatiyan_count", 0) or 0
                min_required = int(expected * _COMPLETION_MIN_FRACTION) if expected > 0 else 0

                # Guard: don't mark "done" if we saved too few khatiyans relative
                # to the expected count (catches premature dropdown / empty saves).
                if kh_done == 0 and expected > 0:
                    logger.error(
                        "Worker %s: village %s expected %d khatiyans but saved 0 — "
                        "marking as FAILED so it will be retried.",
                        worker_id, vil_name, expected,
                    )
                    _fail(v_id, f"0 khatiyans saved (expected {expected}) — likely storage error")
                elif expected > 0 and kh_done < min_required:
                    # 0 new khatiyans this run + resume checkpoint → stuck loop; clear it.
                    if self.khatiyans_processed == 0 and resume_kh:
                        logger.warning(
                            "Worker %s: village %s made no progress with resume "
                            "checkpoint %r (%d/%d saved) — clearing checkpoint.",
                            worker_id, vil_name, resume_kh, kh_done, expected,
                        )
                        _checkpoint(v_id, kh_done, "")
                    logger.error(
                        "Worker %s: village %s saved only %d/%d khatiyans (<%d%% threshold) — "
                        "marking as FAILED so it will be retried.",
                        worker_id, vil_name, kh_done, expected,
                        int(_COMPLETION_MIN_FRACTION * 100),
                    )
                    _fail(
                        v_id,
                        f"Only {kh_done}/{expected} khatiyans saved "
                        f"(<{int(_COMPLETION_MIN_FRACTION * 100)}% threshold)",
                    )
                elif kh_done == 0 and expected == 0:
                    logger.info(
                        "Worker %s: village %s has 0 expected khatiyans, marking done",
                        worker_id, vil_name,
                    )
                    _complete(v_id, 0)
                    village_ok = True
                else:
                    _complete(v_id, kh_done)
                    logger.info(
                        "Worker %s: completed village %s (%d khatiyans)", worker_id, vil_name, kh_done
                    )
                    village_ok = True

                # Reset per-village counter
                self.khatiyans_processed = 0
                _villages_done += 1

                # Periodic browser restart to prevent Chromium memory leaks
                if _villages_done % _BROWSER_RESTART_EVERY == 0:
                    logger.info(
                        "Worker %s: periodic browser restart after %d villages (memory hygiene)",
                        worker_id, _villages_done,
                    )
                    await self._restart_browser(headless=headless)

            except Exception as e:
                logger.error("Worker %s: error on village %s: %s", worker_id, vil_name, e)

                # Save partial progress to work queue BEFORE marking failed.
                # Without this, a village that processed 200 khatiyans then timed out
                # would show khatiyans_fetched=0 and burn a retry instead of resuming.
                if self.khatiyans_processed > 0 and self._last_khatiyan_no:
                    kh_partial = already_done + self.khatiyans_processed
                    _checkpoint(v_id, kh_partial, self._last_khatiyan_no)
                    logger.info(
                        "Worker %s: checkpointed %d khatiyans for village %s before failing",
                        worker_id, kh_partial, vil_name,
                    )

                self.khatiyans_processed = 0
                self._last_khatiyan_no = None
                _fail(v_id, str(e))
                # Re-navigate for next village
                try:
                    await self.navigate_to_ror_page()
                except Exception:
                    pass

            # ── Site-down backoff ──────────────────────────────────────────────
            if village_ok:
                _consecutive_failures = 0
            else:
                _consecutive_failures += 1

                # Exit cleanly after exhausting all backoff steps — systemd will
                # restart the service with fresh browser instances.
                if _consecutive_failures >= _SITE_DOWN_EXIT_AFTER:
                    logger.error(
                        "Worker %s: %d consecutive village failures — exhausted all "
                        "backoff attempts. Exiting so systemd can restart cleanly.",
                        worker_id, _consecutive_failures,
                    )
                    break

                if _consecutive_failures >= _SITE_DOWN_THRESHOLD:
                    _idx = min(
                        _consecutive_failures - _SITE_DOWN_THRESHOLD,
                        len(_SITE_BACKOFF) - 1,
                    )
                    # Add per-worker jitter (0–15s) to prevent all 20 workers from
                    # hammering the site simultaneously after backoff.
                    _jitter = random.uniform(0, 15)
                    _wait = _SITE_BACKOFF[_idx] + _jitter
                    logger.warning(
                        "Worker %s: %d consecutive failures (step %d/%d) — "
                        "site appears down. Waiting %.0fs before restarting browser…",
                        worker_id, _consecutive_failures,
                        _idx + 1, len(_SITE_BACKOFF), _wait,
                    )
                    await asyncio.sleep(_wait)
                    ok = await self._restart_browser(headless=headless)
                    if not ok:
                        logger.error(
                            "Worker %s: browser restart failed completely — "
                            "exiting so systemd can restart with a fresh browser.",
                            worker_id,
                        )
                        break

        await self.cleanup()


async def get_district_list(base_url: str = "http://bhulekh.ori.nic.in", headless: bool = True) -> List[Dict[str, str]]:
    """
    Fetch list of districts from the site (value, text). Used by multi-worker runner to partition work.
    """
    scraper = BhulekhScraper(base_url=base_url)
    try:
        await scraper.init_browser(headless=headless)
        await scraper.navigate_to_ror_page()
        options = await scraper.get_dropdown_options(SELECTOR_DISTRICT)
        return options
    finally:
        await scraper.cleanup()


async def main():
    """Main entry point. All options are available as command-line arguments."""
    import argparse
    import sys

    epilog = """
Examples:
  python bhulekh_scraper.py
  python bhulekh_scraper.py --district "4" --dry-run
  python bhulekh_scraper.py --headless --limit-khatiyans 10
  python bhulekh_scraper.py --browser brave --persistent

Full command reference: see MAN.md or README.md
"""
    parser = argparse.ArgumentParser(
        description='Bhulekh RoR Data Scraper — fetches Record of Rights from bhulekh.ori.nic.in.',
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--version', action='store_true', help='Show version and exit')
    parser.add_argument('--district', type=str, metavar='NAME_OR_ID',
                        help='District name or value to start from')
    parser.add_argument('--tahasil', type=str, metavar='NAME_OR_ID',
                        help='Tahasil name or value to start from (use with --district)')
    parser.add_argument('--village', type=str, metavar='NAME_OR_ID',
                        help='Village name or value to start from (use with --district, --tahasil)')
    parser.add_argument('--headless', action='store_true',
                        help='Run browser in headless mode (no GUI)')
    parser.add_argument('--url', type=str, default='http://bhulekh.ori.nic.in', metavar='URL',
                        help='Base URL of the website (default: http://bhulekh.ori.nic.in)')
    parser.add_argument('--browser', type=str, choices=['chromium', 'firefox', 'webkit', 'brave'],
                        default='chromium',
                        help='Browser to use (default: chromium)')
    parser.add_argument('--brave-path', type=str, metavar='PATH',
                        help='Path to Brave executable (e.g. .../brave.exe)')
    parser.add_argument('--connect-browser', type=str, metavar='URL',
                        help='Connect to existing browser via CDP (e.g. http://localhost:9222)')
    parser.add_argument('--persistent', action='store_true',
                        help='Use persistent context (saves cookies/session)')
    parser.add_argument('--user-data-dir', type=str, default='browser_data', metavar='DIR',
                        help='Directory for persistent browser data (default: browser_data)')
    parser.add_argument('--debug', action='store_true',
                        help='Save page content and screenshots on errors')
    parser.add_argument('--dry-run', action='store_true',
                        help='Process only 3 Khatiyans then stop; file updates after each record')
    parser.add_argument('--limit-khatiyans', type=int, metavar='N',
                        help='Stop after N Khatiyans (file updates after each record)')
    parser.add_argument('--data-dir', type=str, default=None, metavar='DIR',
                        help=f'Persistent storage directory (SQLite per district). Default: {DEFAULT_DATA_DIR!r} when --resume or this is set')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from last checkpoint in --data-dir (per district)')
    parser.add_argument('--storage', type=str, choices=['sqlite', 'ndjson'], default='sqlite',
                        help='Storage backend: sqlite (default) or ndjson (plain JSON Lines file, often faster append)')
    parser.add_argument('--fast', action='store_true',
                        help='Use minimal delays (delay_scale=0.15). Faster but may trigger timeouts or blocks on the site.')

    args = parser.parse_args()

    if args.version:
        print(f"bhulekh_scraper {VERSION}")
        sys.exit(0)

    if EXPIRY_DATE is not None and date.today() > EXPIRY_DATE:
        print(f"This program has expired (expiry date: {EXPIRY_DATE}).", file=sys.stderr)
        sys.exit(1)

    limit = args.limit_khatiyans
    if args.dry_run and limit is None:
        limit = 3

    data_dir = args.data_dir
    if (args.resume or data_dir) and data_dir is None:
        data_dir = DEFAULT_DATA_DIR

    delay_scale = 0.15 if args.fast else 1.0
    scraper = BhulekhScraper(
        base_url=args.url,
        browser_type=args.browser,
        use_persistent_context=args.persistent,
        user_data_dir=args.user_data_dir,
        brave_executable_path=args.brave_path,
        connect_to_browser=args.connect_browser,
        debug=args.debug,
        limit_khatiyans=limit,
        data_dir=data_dir,
        resume=args.resume,
        storage_backend=args.storage,
        delay_scale=delay_scale,
    )
    try:
        await scraper.run(
            district=args.district,
            tahasil=args.tahasil,
            village=args.village,
            headless=args.headless
        )
    except KeyboardInterrupt:
        logger.info("Scraping interrupted by user")
        await scraper.cleanup()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        await scraper.cleanup()
        raise


if __name__ == "__main__":
    # Use asyncio.run() which properly handles event loop cleanup on Windows
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Program interrupted by user")
    except Exception as e:
        logger.error(f"Program error: {e}")
        import sys
        sys.exit(1)

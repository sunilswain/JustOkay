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
import pandas as pd
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
import logging
from typing import Optional, List, Dict
import time
import json
from pathlib import Path
import random
from datetime import date

# Expiry: program will refuse to run after this date (useful for .exe builds).
# Set to None to disable expiry check.
EXPIRY_DATE = date(2026, 2, 15)  # YYYY, MM, DD
VERSION = "1.0.0"

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
# Khatiyan radio can be disabled until District/Tahasil are selected
SELECTOR_RADIO_KHATIYAN = 'input#ctl00_ContentPlaceHolder1_rbtnRORSearchtype_0, input[value="Khatiyan"][name*="rbtnRORSearchtype"]'


class BhulekhScraper:
    """Main scraper class for Bhulekh website automation."""
    
    def __init__(self, base_url: str = "http://bhulekh.ori.nic.in", 
                 browser_type: str = "chromium",
                 use_persistent_context: bool = False,
                 user_data_dir: Optional[str] = None,
                 brave_executable_path: Optional[str] = None,
                 connect_to_browser: Optional[str] = None,
                 debug: bool = False,
                 limit_khatiyans: Optional[int] = None):
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
        elif getattr(sys, "frozen", False) and self.browser_type == 'chromium':
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
                    if getattr(sys, "frozen", False) and ("Executable doesn't exist" in err_msg or "doesn't exist at" in err_msg):
                        raise RuntimeError(
                            "Chromium not installed next to this exe.\n"
                            "Run install_everything.exe from this folder (it will create a 'browsers' folder here),\n"
                            f"then run this exe again. Folder used: {_browsers_path}"
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
                    if getattr(sys, "frozen", False) and ("Executable doesn't exist" in err_msg or "doesn't exist at" in err_msg):
                        raise RuntimeError(
                            "Chromium not installed next to this exe.\n"
                            "Run install_everything.exe from this folder (it will create a 'browsers' folder here),\n"
                            f"then run this exe again. Folder used: {_browsers_path}"
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
                if getattr(sys, "frozen", False) and ("Executable doesn't exist" in err_msg or "doesn't exist at" in err_msg):
                    raise RuntimeError(
                        "Chromium not installed next to this exe.\n"
                        "Run install_everything.exe from this folder (it will create a 'browsers' folder here),\n"
                        f"then run this exe again. Folder used: {_browsers_path}"
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
        """Add random human-like delay."""
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
            logger.warning(f"Error checking for timeout: {e}")
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
    
    async def select_dropdown(self, selector: str, value: str, wait_for_update: bool = True):
        """Select a value from a dropdown and wait for dependent fields to update."""
        try:
            # Simulate human behavior: move mouse to dropdown first
            try:
                dropdown = self.page.locator(selector)
                await dropdown.hover()
                await self.human_delay(0.15, 0.35)
            except:
                pass
            
            # Select the option
            await self.page.select_option(selector, value)
            logger.info(f"Selected {value} from {selector}")
            
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
                await asyncio.sleep(0.25)
            logger.warning(f"Dropdown {selector} did not populate within {timeout_ms}ms")
            return False
        except Exception as e:
            logger.warning(f"Error waiting for dropdown: {e}")
            return False

    async def get_dropdown_options(self, selector: str) -> List[Dict[str, str]]:
        """Get all options from a dropdown.
        
        The selector uses 'id*=' which means "contains", so it will match ASP.NET IDs like:
        - ctl00_ContentPlaceHolder1_ddlDistrict
        - ctl00_ContentPlaceHolder1_ddlTahsil
        - ctl00_ContentPlaceHolder1_ddlVillage
        - ctl00_ContentPlaceHolder1_ddlBindData
        
        Args:
            selector: CSS selector for the dropdown (e.g., SELECTOR_DISTRICT)
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
                await asyncio.sleep(random.uniform(0.2, 0.5))
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
    
    async def extract_ror_data(self) -> Dict:
        """Extract data from the RoR page."""
        try:
            data = {}
            
            # Extract front page data
            front_data = await self.extract_front_page_data()
            data.update(front_data)
            
            # Extract back page data (plot details)
            back_data = await self.extract_back_page_data()
            data['plots'] = back_data
            
            return data
        except Exception as e:
            logger.error(f"Error extracting RoR data: {e}")
            return {}
    
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
            
        except Exception as e:
            logger.error(f"Error extracting front page data: {e}")
        
        return data
    
    async def extract_back_page_data(self) -> List[Dict]:
        """Extract plot data from the back page of RoR."""
        plots = []
        
        try:
            # Get all plot rows (excluding header and footer rows)
            plot_rows = await self.page.locator('#gvRorBack tr').all()
            
            for row in plot_rows[2:-1]:  # Skip header rows and footer row
                try:
                    plot_data = {}
                    
                    # Extract plot number and chaka
                    plot_no_elem = row.locator('a[id*="lblPlotNo"]')
                    if await plot_no_elem.count() > 0:
                        plot_data['plot_no'] = await plot_no_elem.inner_text()
                    else:
                        continue  # Skip if no plot number (might be header/footer)
                    
                    chaka_elem = row.locator('span[id*="lblchaka"]')
                    if await chaka_elem.count() > 0:
                        plot_data['chaka'] = await chaka_elem.inner_text()
                    
                    # Extract land type
                    land_type_elem = row.locator('span[id*="lbllType"]')
                    if await land_type_elem.count() > 0:
                        plot_data['land_type'] = await land_type_elem.inner_text()
                    
                    # Extract kisam and occupation details
                    kisam_elem = row.locator('span[id*="lblKisama"]')
                    if await kisam_elem.count() > 0:
                        plot_data['kisam'] = await kisam_elem.inner_text()
                    
                    occu_elems = {
                        'n_occu': row.locator('span[id*="lbln_occu"]'),
                        'e_occu': row.locator('span[id*="lble_occu"]'),
                        's_occu': row.locator('span[id*="lbls_occu"]'),
                        'w_occu': row.locator('span[id*="lblw_occu"]')
                    }
                    
                    for key, elem in occu_elems.items():
                        if await elem.count() > 0:
                            plot_data[key] = await elem.inner_text()
                    
                    # Extract area measurements
                    acre_elem = row.locator('span[id*="lblAcre"]')
                    if await acre_elem.count() > 0:
                        plot_data['acre'] = await acre_elem.inner_text()
                    
                    decimil_elem = row.locator('span[id*="lblDecimil"]')
                    if await decimil_elem.count() > 0:
                        plot_data['decimil'] = await decimil_elem.inner_text()
                    
                    hector_elem = row.locator('span[id*="lblHector"]')
                    if await hector_elem.count() > 0:
                        plot_data['hector'] = await hector_elem.inner_text()
                    
                    # Extract remarks
                    remarks_elem = row.locator('span[id*="lblPlotRemarks"]')
                    if await remarks_elem.count() > 0:
                        plot_data['remarks'] = await remarks_elem.inner_text()
                    
                    if plot_data:
                        plots.append(plot_data)
                        
                except Exception as e:
                    logger.warning(f"Error extracting plot data from row: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error extracting back page data: {e}")
        
        return plots
    
    async def click_view_ror(self):
        """Click the View RoR button with human-like behavior."""
        try:
            button = self.page.locator('input[id*="btnRORFront"]')
            await button.hover()
            await self.human_delay(0.15, 0.4)
            
            # Click the button
            await button.click()
            await self.wait_for_page_load()
            
            # Check for timeout error
            if await self.check_for_timeout_error():
                raise Exception("Timeout error after clicking View RoR")
            
            logger.info("Clicked View RoR button")
        except Exception as e:
            logger.error(f"Error clicking View RoR button: {e}")
            raise
    
    async def click_khatiyan_page(self):
        """Click the Khatiyan Page button to go back."""
        try:
            # Wait for button to be available
            await self.page.wait_for_selector('input[id="btnKhatiyan"]', timeout=10000)
            
            button = self.page.locator('input[id="btnKhatiyan"]')
            await button.hover()
            await self.human_delay(0.15, 0.35)
            
            await button.click()
            await self.wait_for_page_load()
            
            if await self.check_for_timeout_error():
                logger.warning("Timeout error after clicking Khatiyan Page, navigating to main page...")
                await self.navigate_to_ror_page()
                return
            
            await self.human_delay(0.3, 0.6)
            logger.info("Clicked Khatiyan Page button (back)")
        except Exception as e:
            logger.error(f"Error clicking Khatiyan Page button: {e}")
            # Try to navigate back to the main page if button click fails
            try:
                await self.navigate_to_ror_page()
            except:
                raise
    
    async def process_khatiyan(self, khatiyan_value: str, khatiyan_text: str, 
                               district: str, tahasil: str, village: str) -> bool:
        """Process a single Khatiyan: select it, view RoR, extract data, and go back."""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                # Select the Khatiyan
                await self.select_dropdown(SELECTOR_KHATIYAN, khatiyan_value, wait_for_update=False)
                await self.human_delay(0.3, 0.6)
                
                await self.click_view_ror()
                
                await self.wait_for_page_load()
                await self.human_delay(0.4, 0.7)
                
                # Check for timeout error before extracting
                if await self.check_for_timeout_error():
                    if attempt < max_retries - 1:
                        logger.warning(f"Timeout error detected, retrying (attempt {attempt + 1}/{max_retries})...")
                        await self.navigate_to_ror_page()
                        # Re-select district, tahasil, village
                        await self.select_dropdown(SELECTOR_DISTRICT, district, wait_for_update=True)
                        await self.select_dropdown(SELECTOR_TAHASIL, tahasil, wait_for_update=True)
                        await self.select_dropdown(SELECTOR_VILLAGE, village, wait_for_update=True)
                        await self.select_search_type("Khatiyan")
                        await self.human_delay(0.5, 1.0)
                        continue
                    else:
                        raise Exception("Timeout error persists after retries")
                
                # Extract data
                ror_data = await self.extract_ror_data()
                
                # Add metadata
                ror_data['district'] = district
                ror_data['tahasil'] = tahasil
                ror_data['village'] = village
                ror_data['khatiyan_value'] = khatiyan_value
                ror_data['khatiyan_text'] = khatiyan_text
                
                # Store data
                self.data_list.append(ror_data)
                self.khatiyans_processed += 1
                total = len(self.data_list)
                logger.info(f"Processed Khatiyan: {khatiyan_text} (Value: {khatiyan_value}) | Records: {total}")
                # In dry-run/limit mode, save after each record so you see active file changes
                if self.limit_khatiyans is not None:
                    await self.save_data()
                    logger.info(f"File updated: {total} record(s) in bhulekh_data.json / bhulekh_data.csv")
                
                await self.click_khatiyan_page()
                await self.wait_for_page_load()
                await self.human_delay(0.3, 0.6)
                
                return True
                
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Error processing Khatiyan {khatiyan_value}, retrying (attempt {attempt + 1}/{max_retries}): {e}")
                    await self.human_delay(0.8, 1.5)
                    try:
                        await self.navigate_to_ror_page()
                    except:
                        pass
                else:
                    logger.error(f"Error processing Khatiyan {khatiyan_value} after {max_retries} attempts: {e}")
                    return False
        return False
    
    async def process_village(self, village_value: str, village_text: str,
                             district: str, tahasil: str) -> bool:
        """Process all Khatiyans in a village."""
        try:
            # Select village (triggers postback; Khatiyan dropdown will populate)
            await self.select_dropdown(SELECTOR_VILLAGE, village_value)
            
            # Wait for Khatiyan dropdown to be populated by server
            logger.info("Waiting for Khatiyan dropdown to populate...")
            if not await self.wait_for_dropdown_populated(SELECTOR_KHATIYAN, min_options=1, timeout_ms=25000):
                logger.warning(f"Khatiyan dropdown did not populate for village {village_text}, skipping")
                return True
            await self.human_delay(0.2, 0.4)
            
            # Get all Khatiyans
            khatiyan_options = await self.get_dropdown_options(SELECTOR_KHATIYAN)
            
            if not khatiyan_options:
                logger.warning(f"No Khatiyans found for village: {village_text}")
                return True  # Continue to next village
            
            logger.info(f"Processing {len(khatiyan_options)} Khatiyans for village: {village_text}")
            
            # Process each Khatiyan
            for khatiyan in khatiyan_options:
                if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                    break
                # Re-select village if needed (after coming back from RoR page)
                current_village = await self.page.evaluate("document.querySelector('#ctl00_ContentPlaceHolder1_ddlVillage')?.value || document.querySelector('select[id*=\"ddlVillage\"]')?.value || ''")
                if current_village != village_value:
                    await self.select_dropdown(SELECTOR_VILLAGE, village_value)
                    await asyncio.sleep(0.8)
                
                success = await self.process_khatiyan(
                    khatiyan['value'],
                    khatiyan['text'],
                    district,
                    tahasil,
                    village_text
                )
                
                if not success:
                    logger.warning(f"Failed to process Khatiyan: {khatiyan['text']}")
                if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                    break
                await self.human_delay(0.4, 0.8)
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing village {village_value}: {e}")
            return False
    
    async def process_tahasil(self, tahasil_value: str, tahasil_text: str,
                             district: str, start_village: Optional[str] = None) -> bool:
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
            
            # Determine starting point
            start_index = 0
            if start_village:
                for i, village in enumerate(village_options):
                    if village['value'] == start_village or village['text'] == start_village:
                        start_index = i
                        break
            
            logger.info(f"Processing {len(village_options) - start_index} villages for Tahasil: {tahasil_text}")
            
            # Process each village
            for village in village_options[start_index:]:
                # Re-select Tahasil if needed (after coming back from RoR page)
                current_tahasil = await self.page.evaluate("document.querySelector('#ctl00_ContentPlaceHolder1_ddlTahsil')?.value || document.querySelector('select[id*=\"ddlTahsil\"]')?.value || ''")
                if current_tahasil != tahasil_value:
                    await self.select_dropdown(SELECTOR_TAHASIL, tahasil_value)
                    await asyncio.sleep(0.8)
                
                success = await self.process_village(
                    village['value'],
                    village['text'],
                    district,
                    tahasil_text
                )
                
                if not success:
                    logger.warning(f"Failed to process village: {village['text']}")
                if self.limit_khatiyans is not None and self.khatiyans_processed >= self.limit_khatiyans:
                    break
                await self.human_delay(0.5, 1.0)
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing Tahasil {tahasil_value}: {e}")
            return False
    
    async def process_district(self, district_value: str, district_text: str,
                              start_tahasil: Optional[str] = None,
                              start_village: Optional[str] = None) -> bool:
        """Process all Tahasils in a District."""
        try:
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
                
                success = await self.process_tahasil(
                    tahasil['value'],
                    tahasil['text'],
                    district_text,
                    start_village_value
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
            
            # Save data to file (full save if not already saving per-record)
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
    
    scraper = BhulekhScraper(
        base_url=args.url,
        browser_type=args.browser,
        use_persistent_context=args.persistent,
        user_data_dir=args.user_data_dir,
        brave_executable_path=args.brave_path,
        connect_to_browser=args.connect_browser,
        debug=args.debug,
        limit_khatiyans=limit
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

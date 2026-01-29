"""
Bhulekh Scraper — Browser Setup
Run this once (or build as install_browsers.exe) to download Chromium to a shared folder.
The main bhulekh_scraper.exe will then find the browser and run without needing Python.
"""

import os
import sys

# Same path the main exe uses (must be set before any Playwright import)
def get_browsers_path():
    apd = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if not apd:
        return None
    return os.path.join(apd, "BhulekhScraper", "browsers")


def main():
    path = get_browsers_path()
    if not path:
        print("Error: Could not determine AppData path.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(path, exist_ok=True)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path

    print("Bhulekh Scraper — Browser Setup")
    print("=" * 50)
    print("Installing Chromium to:", path)
    print("This may take a few minutes on first run.")
    print()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: Playwright is not installed. Run: pip install playwright", file=sys.stderr)
        sys.exit(1)

    try:
        with sync_playwright() as p:
            # Launching Chromium triggers download if not present at PLAYWRIGHT_BROWSERS_PATH
            browser = p.chromium.launch()
            browser.close()
        print()
        print("Chromium installed successfully.")
        print("You can now run bhulekh_scraper.exe")
    except Exception as e:
        print("Error installing Chromium:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

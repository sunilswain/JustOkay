"""
Bhulekh Scraper — Browser Setup
Run this once (or build as install_browsers.exe) to download Chromium to a shared folder.
The main bhulekh_scraper.exe will then find the browser and run without needing Python.
"""

import os
import shutil
import subprocess
import sys

# Same path the main exe uses: when exe = folder next to exe; when script = AppData
def get_browsers_path():
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "browsers")
    apd = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if not apd and sys.platform == "win32":
        apd = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    if not apd:
        return None
    return os.path.join(apd, "BhulekhScraper", "browsers")


def get_python_executable():
    """When frozen, sys.executable is the .exe; we need real Python for 'playwright install'."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3", "py"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def main():
    path = get_browsers_path()
    if not path:
        print("Error: Could not determine AppData path.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(path, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = path

    print("Bhulekh Scraper — Browser Setup")
    print("=" * 50)
    print("Installing Chromium to:", path)
    print("This may take a few minutes on first run.")
    print()

    python_exe = get_python_executable()
    install_ok = False
    if python_exe and (not getattr(sys, "frozen", False) or python_exe != sys.executable):
        # Use Python to run playwright install (when not frozen, or when frozen and Python is on PATH)
        result = subprocess.run(
            [python_exe, "-m", "playwright", "install", "chromium"],
            env=env,
            capture_output=False,
        )
        install_ok = result.returncode == 0
        if not install_ok:
            print("Error: Playwright install failed. Install Playwright first: pip install playwright", file=sys.stderr)
    elif getattr(sys, "frozen", False):
        # Frozen exe and no Python on PATH: cannot run "playwright install". Tell user to run from Python.
        print("When using install_browsers.exe, Python must be on your PATH so Chromium can be installed.", file=sys.stderr)
        print("Add Python to PATH, or run from a terminal: python install_browsers.py", file=sys.stderr)
        sys.exit(1)
    else:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            env=env,
            capture_output=False,
        )
        install_ok = result.returncode == 0
        if not install_ok:
            print("Error: Playwright install failed. Run: pip install playwright", file=sys.stderr)
            sys.exit(1)

    if not install_ok:
        sys.exit(1)

    # Quick launch test to verify browser works
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
    except Exception as e:
        print("Error verifying Chromium:", e, file=sys.stderr)
        sys.exit(1)

    print()
    print("Chromium installed successfully.")
    print("You can now run bhulekh_scraper.exe")


if __name__ == "__main__":
    main()

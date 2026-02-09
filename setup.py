"""
Bhulekh Scraper — Setup: install Python dependencies and Chromium.
Run once (or build as setup.exe). When exe: installs to a 'browsers' folder next to the exe.
"""

import os
import shutil
import subprocess
import sys


def get_browsers_path():
    """Same path bhulekh_scraper.exe uses: when exe = folder next to exe; when script = AppData."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "browsers")
    apd = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if not apd and sys.platform == "win32":
        apd = os.path.join(os.path.expanduser("~"), "AppData", "Local")
    if not apd:
        return None
    return os.path.join(apd, "BhulekhScraper", "browsers")


def get_python_executable():
    """When frozen, sys.executable is the .exe; we need real Python for pip/playwright."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3", "py"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


def get_script_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def main():
    path = get_browsers_path()
    if not path:
        print("Error: Could not determine browser path.", file=sys.stderr)
        sys.exit(1)

    os.makedirs(path, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = path

    script_dir = get_script_dir()
    requirements = os.path.join(script_dir, "requirements.txt")

    print("Bhulekh Scraper — Setup")
    print("=" * 50)

    python_exe = get_python_executable()
    if not python_exe or (getattr(sys, "frozen", False) and python_exe == sys.executable):
        if getattr(sys, "frozen", False):
            print("Python not found on PATH. Add Python to PATH and run setup.exe again.", file=sys.stderr)
            sys.exit(1)
        python_exe = sys.executable

    # 1. Install Python dependencies
    print("1. Installing Python dependencies...")
    if os.path.isfile(requirements):
        subprocess.run(
            [python_exe, "-m", "pip", "install", "-r", requirements],
            env=env,
            capture_output=False,
        )
    else:
        subprocess.run(
            [python_exe, "-m", "pip", "install", "playwright", "pandas"],
            env=env,
            capture_output=False,
        )
    print()

    # 2. Install Chromium (when exe, ensure playwright is installed for system Python)
    if getattr(sys, "frozen", False):
        print("Ensuring Playwright is installed for Python...", flush=True)
        subprocess.run(
            [python_exe, "-m", "pip", "install", "playwright"],
            env=env,
            capture_output=False,
        )
    print("2. Installing Chromium to:", path)
    result = subprocess.run(
        [python_exe, "-m", "playwright", "install", "chromium"],
        env=env,
        capture_output=False,
    )
    if result.returncode != 0:
        print("Error: Chromium install failed.", file=sys.stderr)
        sys.exit(1)
    print()
    print("Setup complete. You can run bhulekh_scraper.exe")


if __name__ == "__main__":
    main()

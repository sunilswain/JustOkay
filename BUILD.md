# Building Bhulekh Scraper as an Executable (.exe)

This guide explains how to build **two** executables using **PyInstaller**:

1. **install_browsers.exe** — Run **once** on each PC to download Chromium to a shared folder.
2. **bhulekh_scraper.exe** — The main scraper; it finds Chromium in that folder and runs.

End users run **install_browsers.exe** first, then **bhulekh_scraper.exe**. No Python or `playwright install` needed on the target PC.

## Prerequisites

- Python 3.8+ with the project dependencies installed
- Playwright and Chromium installed (you already have this if the script runs)

## 1. Install PyInstaller

From your project folder, in the same environment you use to run the scraper:

```bash
pip install pyinstaller
```

Or with uv:

```bash
uv pip install pyinstaller
```

## 2. Install Playwright Browsers (if not already)

The script uses Playwright’s Chromium. Ensure it’s installed in the environment you use to build:

```bash
playwright install chromium
```

## 3. Build the Executable

### Option A: One-file executable (single .exe)

```bash
pyinstaller bhulekh_scraper.spec
```

If you don’t use the spec file yet (see below), run:

```bash
pyinstaller --onefile --console --name bhulekh_scraper ^
  --hidden-import=playwright._impl._api_types ^
  --hidden-import=playwright.async_api ^
  --hidden-import=pandas ^
  bhulekh_scraper.py
```

On PowerShell, use backticks for line continuation or a single line:

```powershell
pyinstaller --onefile --console --name bhulekh_scraper --hidden-import=playwright._impl._api_types --hidden-import=playwright.async_api --hidden-import=pandas bhulekh_scraper.py
```

### Option B: One-folder (faster startup, easier to debug)

```bash
pyinstaller --onedir --console --name bhulekh_scraper ^
  --hidden-import=playwright._impl._api_types ^
  --hidden-import=playwright.async_api ^
  --hidden-import=pandas ^
  bhulekh_scraper.py
```

Output:

- **One-file:** `dist\bhulekh_scraper.exe`
- **One-folder:** `dist\bhulekh_scraper\` with `bhulekh_scraper.exe` inside

## 4. How It Works on the Target PC

1. **First time:** User runs **install_browsers.exe**. It downloads Chromium to `%LOCALAPPDATA%\BhulekhScraper\browsers` (no Python needed).
2. **Every time:** User runs **bhulekh_scraper.exe**. It uses that same folder via `PLAYWRIGHT_BROWSERS_PATH`, so no "Executable doesn't exist" error.

If the user runs **bhulekh_scraper.exe** without running **install_browsers.exe** first, they'll see: *Chromium not found. Run install_browsers.exe first to download Chromium. Then run bhulekh_scraper.exe again.*

*(Legacy: You can still run the exe where Python + Playwright are already installed; the exe will find the browser.)*

### Option 2: Install browsers once on the target PC

On the target PC, either:

1. Install Python, then run:
   ```bash
   pip install playwright
   playwright install chromium
   ```
   Then run your `bhulekh_scraper.exe` from the same user account, or

2. Use a small helper script you ship next to the exe that calls Python to run `playwright install chromium` (user must have Python installed).

### Option 3: Bundle Chromium with the installer (advanced)

You can build an installer (e.g. with Inno Setup or NSIS) that:

1. Copies the exe and Playwright’s Chromium folder (from `%USERPROFILE%\AppData\Local\ms-playwright\`) into Program Files, and  
2. Sets `PLAYWRIGHT_BROWSERS_PATH` so the exe looks there for Chromium.

This is more work but gives a “single installer” experience.

## 5. Running the Built Executable

**First time on a PC (or for a new user):**

```cmd
cd path\to\dist
install_browsers.exe
```

Wait until it says “Chromium installed successfully.” Then:

```cmd
bhulekh_scraper.exe --help
bhulekh_scraper.exe --version
bhulekh_scraper.exe --dry-run
bhulekh_scraper.exe --district "4" --limit-khatiyans 5
```

All command-line options from MAN.md work the same way.

Output files (`bhulekh_data.json`, `bhulekh_data.csv`, `bhulekh_scraper.log`) are created in the **current working directory** (the folder from which you run the exe).

## 6. What to Ship to Users

Ship both exes (e.g. in one folder):

- **install_browsers.exe** — “Run this once to install Chromium.”
- **bhulekh_scraper.exe** — “Then run this to scrape.”

No Python or `playwright install` required on the user’s machine.

## 7. Expiry Date (for .exe)

The script checks `EXPIRY_DATE` at the top of `bhulekh_scraper.py`. Before building the exe:

- Set the date you want: `EXPIRY_DATE = date(2027, 12, 31)`
- Or disable: `EXPIRY_DATE = None`

Then rebuild the exe.

## 8. Troubleshooting

| Issue | What to try |
|-------|-------------|
| “Chromium not found” / “Executable doesn’t exist” | Run **install_browsers.exe** first on that PC (same user). |
| Missing module / ImportError | Add the missing module as a hidden import: `--hidden-import=module.name` and rebuild. |
| Antivirus blocks .exe | PyInstaller one-file executables are often flagged. Use `--onedir` or add an exception. |
| Slow startup (one-file) | Normal for one-file; use `--onedir` for faster startup. |

## 9. Using the Spec Files

The repo includes `bhulekh_scraper.spec` so you can run:

```bash
pyinstaller bhulekh_scraper.spec
```

and adjust the spec (e.g. add hidden imports, change onefile/onedir) instead of long command lines.

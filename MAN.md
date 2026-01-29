# Bhulekh Scraper — Command Reference (Man Page)

## Name

**bhulekh_scraper** — Scrape RoR (Record of Rights) data from bhulekh.ori.nic.in

## Synopsis

```text
python bhulekh_scraper.py [OPTIONS]
```

When built as an executable:

```text
bhulekh_scraper.exe [OPTIONS]
```

## Description

Bhulekh Scraper automates fetching RoR data by driving a browser through District → Tahasil → Village → Khatiyan and saving results to JSON and CSV. All behaviour is controlled by command-line arguments.

**Expiry:** If an expiry date is set, the program will not run after that date. Use `--version` to check; `--help` and `--version` work even after expiry.

## Options

### Start / scope

| Option | Argument | Description |
|--------|----------|-------------|
| `--district` | NAME_OR_ID | Start from this district (name or value). Omit to process all districts. |
| `--tahasil` | NAME_OR_ID | Start from this tahasil (use with `--district`). |
| `--village` | NAME_OR_ID | Start from this village (use with `--district` and `--tahasil`). |
| `--dry-run` | — | Process only 3 Khatiyans then stop. Output file is updated after each record. |
| `--limit-khatiyans` | N | Stop after processing N Khatiyans. File updates after each record. |

### Browser

| Option | Argument | Description |
|--------|----------|-------------|
| `--browser` | chromium \| firefox \| webkit \| brave | Browser to use (default: **chromium**). |
| `--brave-path` | PATH | Path to Brave executable (e.g. `C:\...\brave.exe`). Used when `--browser brave`. |
| `--connect-browser` | URL | Connect to an existing browser via CDP (e.g. `http://localhost:9222`). |
| `--headless` | — | Run browser without a visible window. |

### Session / persistence

| Option | Argument | Description |
|--------|----------|-------------|
| `--persistent` | — | Use persistent browser context (saves cookies/session). |
| `--user-data-dir` | DIR | Directory for persistent data (default: **browser_data**). |

### Server / debugging

| Option | Argument | Description |
|--------|----------|-------------|
| `--url` | URL | Base URL (default: **http://bhulekh.ori.nic.in**). |
| `--debug` | — | On errors, save page HTML and screenshots for debugging. |

### Informational

| Option | Description |
|--------|-------------|
| `--version` | Print version and exit. |
| `--help` | Print help and exit. |

## Examples

```bash
# Process everything (first district onward)
python bhulekh_scraper.py

# Start from a specific district
python bhulekh_scraper.py --district "4"

# Start from district, tahasil, and village
python bhulekh_scraper.py --district "4" --tahasil "8" --village "87"

# Quick test: only 3 Khatiyans, see file update after each
python bhulekh_scraper.py --dry-run

# Limit to 10 Khatiyans
python bhulekh_scraper.py --limit-khatiyans 10 --district Anugul

# Headless (no window)
python bhulekh_scraper.py --headless

# Use Firefox
python bhulekh_scraper.py --browser firefox

# Use Brave with custom path
python bhulekh_scraper.py --browser brave --brave-path "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

# Connect to existing browser (launch Brave with --remote-debugging-port=9222 first)
python bhulekh_scraper.py --connect-browser "http://localhost:9222"

# Save session to avoid timeouts
python bhulekh_scraper.py --persistent

# Custom data directory for persistent context
python bhulekh_scraper.py --persistent --user-data-dir my_browser_data

# Debug mode (saves page/screenshot on errors)
python bhulekh_scraper.py --debug

# Combined: dry run with Brave and persistent session
python bhulekh_scraper.py --dry-run --browser brave --persistent
```

## Output

- **bhulekh_data.json** — Full data (nested).
- **bhulekh_data.csv** — Flattened table (UTF-8 BOM).
- **bhulekh_scraper.log** — Run log (UTF-8).

## Expiry (for .exe)

Expiry is controlled in the script by `EXPIRY_DATE` (e.g. `date(2027, 12, 31)`). Set to `None` to disable. After expiry, the program prints to stderr and exits with code 1; `--help` and `--version` still work.

## See Also

README.md — Installation, workflow, troubleshooting.

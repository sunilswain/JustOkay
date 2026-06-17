# Bhulekh RoR Automation

Automated scraping of Record of Rights (RoR) data from the Odisha Bhulekh portal (bhulekh.ori.nic.in). This project scraped land records across 21 districts of Odisha, covering ~6.7 million khatiyans with plot-level detail.

## Architecture

The project evolved through three scraping approaches:

1. **Playwright Browser Scraper** (`bhulekh_scraper.py`) — Original approach using headless Chromium to interact with the ASP.NET WebForms portal. Handles ViewState, postbacks, and dropdown cascading.

2. **HTTP Scraper v1** (`http_scraper.py`) — Pure `httpx`-based async scraper that replays ASP.NET postback requests directly without a browser. Significantly faster than Playwright.

3. **HTTP Scraper v3** (`http_scraper_v3.py`) — Production scraper used for the bulk of data collection. File-based progress tracking, concurrent workers, automatic retry with exponential backoff.

## Key Components

| File | Purpose |
|------|---------|
| `http_scraper_v3.py` | Production HTTP scraper with async workers |
| `ror_parser.py` | HTML parser for RoR pages (Type 1, Type 2, Form 20) |
| `storage.py` | SQLite storage layer for scraped khatiyans |
| `work_queue.py` | Distributed work queue for multi-instance coordination |
| `watchdog.py` | Process supervisor — auto-restarts scrapers, detects stalls |
| `export_csv.py` | Export SQLite data to CSV (per village) |
| `export_unified_s3.py` | Final unified export — merges DBs, deduplicates, uploads to S3 |
| `seed_progress.py` | Seeds progress state from `villages.json` hierarchy |
| `auto_verify.py` | Verification daemon — spot-checks scraped data quality |

## Data Model

Each khatiyan record contains:
- **Location**: District, Tahasil, Village, Thana
- **Ownership**: Landlord name, tenant/raiyat details, status
- **Revenue**: Water tax, cess, total tax, publish dates
- **Plots** (array): Plot number, chaka, land type, kisam, boundaries (N/E/S/W), area (acre/decimal/hectare)

Records are stored in SQLite databases (`district_{name}.db`) with a `khatiyans` table keyed by `(tahasil, village, khatiyan_value)`.

## Infrastructure

Scraping ran on AWS EC2 instances (`r5.xlarge`) in `ap-south-1`:
- **Watchdog** supervised multiple scraper processes per instance
- **S3 backups** (`bhulekh-backup` bucket) stored exports
- **Progress tracking** via `.done` marker files per village

## Final Data Output

The cleaned, deduplicated data lives in S3:

```
s3://bhulekh-backup/
  csv/D{code}_{odia_name}/        # 21 district folders
    {tahasil_name}/
      {village_name}.csv          # One CSV per village
  dbs/D{code}_{odia_name}.db      # 21 merged SQLite databases
  zips/
    all_districts_csv.zip         # Complete archive (1.4 GB)
    D3_Cuttack.zip                # Individual district zips
    D14_Anugul.zip
    D18_Jajpur.zip
```

**Coverage**: 21 of 30 Odisha districts with complete or near-complete data.

## Setup

```bash
pip install -r requirements.txt
```

Key dependencies: `httpx`, `beautifulsoup4`, `lxml`

## Usage

```bash
# Run HTTP scraper for a district
python http_scraper_v3.py --district 14

# Run with watchdog supervisor
python watchdog.py --districts 14,18,3

# Export district data to CSV
python export_csv.py --district 14 --output /tmp/exports/

# Unified export to S3
INSTANCE_TAG=r5-2 python export_unified_s3.py
```

## License

Educational and research use. Respect the portal's terms of service.

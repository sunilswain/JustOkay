# Bhulekh RoR Data Scraper

This project automates the process of scraping RoR (Record of Rights) data from the Bhulekh website (http://bhulekh.ori.nic.in).

## Features

- Automated web scraping using Playwright
- Handles ASP.NET form postbacks and ViewState
- Iterates through all Districts → Tahasils → Villages → Khatiyans
- Extracts comprehensive RoR data including:
  - Location information (District, Tahasil, Village, Mouja, Thana)
  - Landlord/Khata information
  - Tenant information
  - Tax and revenue details
  - Plot details (plot number, area, land type, occupation)
- Saves data in both JSON and CSV formats
- Supports starting from a specific District, Tahasil, or Village
- Comprehensive logging

## Installation

1. Install Python 3.8 or higher

2. Install required packages:
```bash
pip install -r requirements.txt
```

3. Install Playwright browsers:
```bash
# Install Chromium (default, recommended)
playwright install chromium

# Or install Firefox
playwright install firefox

# Or install WebKit (Safari engine)
playwright install webkit

# Or install all browsers
playwright install
```

**Note**: Playwright uses its own bundled browsers (not your system's installed browsers). These are downloaded automatically and stored separately.

## Usage

### Basic Usage

Run the scraper to process all districts (starts from first district):
```bash
python bhulekh_scraper.py
```

### Start from a Specific District

```bash
python bhulekh_scraper.py --district "4"
```

or by district name:
```bash
python bhulekh_scraper.py --district "ଢ଼େଙ୍କାନାଳ"
```

### Start from a Specific District and Tahasil

```bash
python bhulekh_scraper.py --district "4" --tahasil "8"
```

### Start from a Specific District, Tahasil, and Village

```bash
python bhulekh_scraper.py --district "4" --tahasil "8" --village "87"
```

### Run in Headless Mode

```bash
python bhulekh_scraper.py --headless
```

### Use Different Browser

```bash
# Use Firefox instead of Chromium
python bhulekh_scraper.py --browser firefox

# Use WebKit (Safari engine)
python bhulekh_scraper.py --browser webkit

# Use Brave browser (auto-detects if installed)
python bhulekh_scraper.py --browser brave

# Use Brave with custom path
python bhulekh_scraper.py --browser brave --brave-path "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

# Connect to existing Brave browser (must be launched with remote debugging)
python bhulekh_scraper.py --connect-browser "http://localhost:9222"
```

### Use Persistent Browser Context (Saves Session/Cookies)

```bash
# This saves cookies and session data, useful to avoid repeated logins/timeouts
python bhulekh_scraper.py --persistent

# Custom directory for browser data
python bhulekh_scraper.py --persistent --user-data-dir "my_browser_data"
```

### Custom Base URL

```bash
python bhulekh_scraper.py --url "http://bhulekh.ori.nic.in"
```

## Command Line Arguments

- `--district`: District name or value to start from (optional)
- `--tahasil`: Tahasil name or value to start from (optional, requires --district)
- `--village`: Village name or value to start from (optional, requires --district and --tahasil)
- `--headless`: Run browser in headless mode (no GUI)
- `--browser`: Browser to use - 'chromium', 'firefox', 'webkit', or 'brave' (default: chromium)
- `--brave-path`: Path to Brave browser executable (required if Brave not in default location)
- `--connect-browser`: CDP endpoint URL to connect to existing browser (e.g., http://localhost:9222)
- `--persistent`: Use persistent browser context (saves cookies/session to avoid timeouts)
- `--user-data-dir`: Directory for persistent browser data (default: browser_data)
- `--debug`: Enable debug mode (saves page content and screenshots on errors)
- `--url`: Base URL of the website (default: http://bhulekh.ori.nic.in)

## Output

The scraper generates two output files:

1. **bhulekh_data.json**: Complete data in JSON format with nested structure
2. **bhulekh_data.csv**: Flattened data in CSV format suitable for Excel/analysis

### Data Structure

Each record contains:
- Location metadata (District, Tahasil, Village, Mouja, Thana, etc.)
- Khatiyan information (Serial number, Landlord name)
- Tenant details (Name, Father's name, Caste, Residence)
- Tax information (Water tax, Khajana, Ses, Total)
- Plot details array with:
  - Plot number and Chaka name
  - Land type and Kisam
  - Occupation details (North, East, South, West)
  - Area measurements (Acre, Decimil, Hector)
  - Remarks

## Workflow

The scraper follows this workflow:

1. Navigate to RoRView.aspx
2. Select District (or start from first if not specified)
3. For each District:
   - Select Tahasil (or start from specified/first)
   - For each Tahasil:
     - Select Village (or start from specified/first)
     - For each Village:
       - Select Khatiyan
       - Click "View RoR" button
       - Extract all data from RoR page
       - Click "Khatiyan Page" button to go back
       - Repeat for all Khatiyans
     - Move to next Village
   - Move to next Tahasil
4. Move to next District
5. Save all collected data to files

## Logging

The scraper creates a log file `bhulekh_scraper.log` with detailed information about:
- Page navigation
- Dropdown selections
- Data extraction
- Errors and warnings
- Progress updates

## Error Handling

The scraper includes comprehensive error handling:
- Timeout handling for page loads
- Retry logic for failed operations
- Graceful continuation on individual record failures
- Detailed error logging

## Notes

- **Browser Installation**: Playwright uses its own bundled browsers (Chromium/Firefox/WebKit), NOT your system's installed browsers. These are automatically downloaded when you run `playwright install`.
- **Brave Browser**: You can use your installed Brave browser with `--browser brave`. The script will auto-detect Brave in common locations, or you can specify the path with `--brave-path`.
- **Connecting to Existing Browser**: You can connect to an already running Brave/Chrome browser by launching it with remote debugging (`brave.exe --remote-debugging-port=9222`) and using `--connect-browser http://localhost:9222`.
- **Persistent Context**: Using `--persistent` flag saves cookies and session data, which can help avoid timeout errors by maintaining your session across runs.
- The scraper includes delays between operations to respect the server and avoid overwhelming it
- ASP.NET ViewState and EventValidation are automatically handled by Playwright
- The scraper waits for postbacks to complete before proceeding
- Large-scale scraping may take significant time - consider running overnight or in batches

## Troubleshooting

### Browser not launching
- Ensure Playwright browsers are installed: `playwright install chromium`
- Check if you have sufficient system resources

### Timeout errors
- The website may be slow - increase timeout values in the code
- Check your internet connection
- The website may be temporarily unavailable

### Missing data
- Some fields may be empty in the source data
- Check the log file for extraction errors
- Verify the HTML structure hasn't changed

### Website blocking automation
If the website is detecting and blocking automation:

1. **Use persistent context**: This maintains your session and cookies
   ```bash
   python bhulekh_scraper.py --persistent
   ```

2. **Use your actual browser (Brave)**: This looks more like a real user
   ```bash
   python bhulekh_scraper.py --browser brave --persistent
   ```

3. **Enable debug mode**: See what the website is showing
   ```bash
   python bhulekh_scraper.py --debug
   ```
   This will save `debug_page_content.html` and `debug_page_screenshot.png` if detection occurs.

4. **Manual CAPTCHA**: If there's a CAPTCHA, solve it manually first, then use `--persistent` to maintain the session.

5. **Slower scraping**: The script already includes delays, but you can increase them in the code if needed.

6. **Check for Cloudflare/DDoS protection**: Some sites use Cloudflare which can block automation. Using persistent context with your real browser profile helps.

## License

This project is for educational and research purposes. Please respect the website's terms of service and robots.txt file.

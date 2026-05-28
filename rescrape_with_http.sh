#!/bin/bash
# Re-scrape problematic villages with http_scraper (no Playwright).
# SOAP khatiyan list + HTTP postbacks; supports Form-20 (e.g. ଅଚ୍ୟୁତପାଲି).
#
# Remove the district from bhulekh.service --districts before running, or stop Playwright.
#
#   bash rescrape_with_http.sh 23        # Subarnapur
#   bash rescrape_with_http.sh 3 10      # Cuttack, 10 workers

set -euo pipefail
DIST="${1:?Usage: rescrape_with_http.sh DISTRICT_CODE [WORKERS]}"
WORKERS="${2:-20}"

cd "$(dirname "$0")"
UV="${UV:-/home/ubuntu/.local/bin/uv}"

echo "=== Reset errors + stuck-at-0 for district $DIST ==="
python3 -c "
from work_queue import reset_errors_for_districts
e, s = reset_errors_for_districts(
    'work_queue.db', [$DIST],
    reset_errors=True,
    reset_zero_progress_in_progress=True,
)
print(f'Reset errors={e}, stuck_0_progress={s}')
"

echo "=== HTTP scraper district $DIST ($WORKERS workers) ==="
exec "$UV" run python http_scraper.py \
    --workers "$WORKERS" \
    --db work_queue.db \
    --data-dir bhulekh_data \
    --districts "$DIST" \
    --request-delay 0.1 \
    --max-inflight 40

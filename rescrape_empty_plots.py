#!/usr/bin/env python3
"""
Re-scrape ONLY khatiyans with empty plots - much faster than re-scraping entire villages.

This script:
1. Finds khatiyans with empty plots in district databases
2. Re-scrapes only those specific khatiyans
3. Updates the records in place (doesn't delete good data)

Usage:
    # Test mode - check a few records first
    uv run python rescrape_empty_plots.py --data-dir bhulekh_data --test --limit 5
    
    # Re-scrape empty plots for a specific district
    uv run python rescrape_empty_plots.py --data-dir bhulekh_data --district "କଟକ" --headless
    
    # Re-scrape all districts
    uv run python rescrape_empty_plots.py --data-dir bhulekh_data --headless
"""

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def find_empty_plot_khatiyans(db_path: Path) -> List[Dict]:
    """
    Find all khatiyans with empty plots in a district database.
    
    Returns list of dicts with: district, tahasil, village, khatiyan_value, khatiyan_text, id
    """
    empty = []
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("""
            SELECT id, district, tahasil, village, khatiyan_value, khatiyan_text, data_json
            FROM khatiyans
        """)
        
        for row in cursor:
            id_, district, tahasil, village, kh_value, kh_text, data_json = row
            try:
                data = json.loads(data_json)
                plots = data.get('plots', [])
                if not plots or len(plots) == 0:
                    empty.append({
                        'id': id_,
                        'district': district,
                        'tahasil': tahasil,
                        'village': village,
                        'khatiyan_value': kh_value,
                        'khatiyan_text': kh_text,
                    })
            except json.JSONDecodeError:
                empty.append({
                    'id': id_,
                    'district': district,
                    'tahasil': tahasil,
                    'village': village,
                    'khatiyan_value': kh_value,
                    'khatiyan_text': kh_text,
                })
        
        conn.close()
    except Exception as e:
        logger.error(f"Error reading {db_path}: {e}")
    
    return empty


def get_district_name_from_db(db_path: Path) -> str:
    """Extract district name from a district database."""
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else ""
    except:
        return ""


def update_khatiyan_in_db(db_path: Path, khatiyan_id: int, new_data: Dict) -> bool:
    """Update a khatiyan's data_json in the database."""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE khatiyans SET data_json = ? WHERE id = ?",
            (json.dumps(new_data, ensure_ascii=False), khatiyan_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error updating khatiyan {khatiyan_id}: {e}")
        return False


async def rescrape_single_khatiyan(
    scraper,
    district: str,
    tahasil: str,
    village: str,
    khatiyan_value: str,
    khatiyan_text: str,
) -> Optional[Dict]:
    """
    Re-scrape a single khatiyan and return the RoR data.
    
    Returns the extracted data dict, or None if failed.
    """
    try:
        # Navigate to the khatiyan
        # First select district
        from bhulekh_scraper import SELECTOR_DISTRICT, SELECTOR_TAHASIL, SELECTOR_VILLAGE, SELECTOR_KHATIYAN
        
        # Get district options and find match
        district_opts = await scraper.get_dropdown_options(SELECTOR_DISTRICT)
        district_match = None
        for opt in district_opts:
            if opt['text'] == district:
                district_match = opt
                break
        
        if not district_match:
            logger.error(f"District not found: {district}")
            return None
        
        await scraper.select_dropdown(SELECTOR_DISTRICT, district_match['value'])
        await scraper.human_delay(0.3, 0.5)
        
        # Select tahasil
        tahasil_opts = await scraper.get_dropdown_options(SELECTOR_TAHASIL)
        tahasil_match = None
        for opt in tahasil_opts:
            if opt['text'] == tahasil:
                tahasil_match = opt
                break
        
        if not tahasil_match:
            logger.error(f"Tahasil not found: {tahasil}")
            return None
        
        await scraper.select_dropdown(SELECTOR_TAHASIL, tahasil_match['value'])
        await scraper.human_delay(0.3, 0.5)
        
        # Select village
        village_opts = await scraper.get_dropdown_options(SELECTOR_VILLAGE)
        village_match = None
        for opt in village_opts:
            if opt['text'] == village:
                village_match = opt
                break
        
        if not village_match:
            logger.error(f"Village not found: {village}")
            return None
        
        await scraper.select_dropdown(SELECTOR_VILLAGE, village_match['value'])
        await scraper.human_delay(0.3, 0.5)
        
        # Select khatiyan - use label for selection (handles padded values)
        await scraper.select_dropdown(SELECTOR_KHATIYAN, khatiyan_value, label=khatiyan_text)
        await scraper.human_delay(0.2, 0.4)
        
        # Click View RoR button
        await scraper.click_view_ror()
        await scraper.human_delay(0.5, 1.0)
        
        # Wait for RoR page and scroll
        try:
            await scraper.page.wait_for_selector("#gvfront_ctl02_lblMouja, #gvfront, #gvRorBack", state="visible", timeout=15000)
        except:
            pass
        
        # Scroll to load back table
        await scraper.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await scraper.human_delay(0.3, 0.5)
        
        try:
            await scraper.page.wait_for_selector("#gvRorBack, #gvRorBack2, #gvplotdetail", state="visible", timeout=5000)
        except:
            pass
        
        # Extract data
        ror_data = await scraper.extract_ror_data()
        
        # Add metadata
        ror_data['district'] = district
        ror_data['tahasil'] = tahasil
        ror_data['village'] = village
        ror_data['khatiyan_value'] = khatiyan_value
        ror_data['khatiyan_text'] = khatiyan_text
        
        # Click back to go to khatiyan list
        await scraper.click_khatiyan_page_button()
        await scraper.human_delay(0.3, 0.5)
        
        return ror_data
        
    except Exception as e:
        logger.error(f"Error re-scraping {district}/{tahasil}/{village}/{khatiyan_text}: {e}")
        return None


async def rescrape_empty_plots(
    data_dir: Path,
    district_filter: Optional[str] = None,
    headless: bool = True,
    limit: Optional[int] = None,
    test_mode: bool = False,
):
    """Main function to re-scrape khatiyans with empty plots."""
    
    # Find all district databases
    db_files = sorted(data_dir.glob("district_*.db"))
    if not db_files:
        logger.error(f"No district databases found in {data_dir}")
        return
    
    # Collect all empty khatiyans
    all_empty = []
    db_map = {}  # district -> db_path
    
    for db_path in db_files:
        district = get_district_name_from_db(db_path)
        if not district:
            continue
        
        if district_filter and district != district_filter:
            continue
        
        db_map[district] = db_path
        empty = find_empty_plot_khatiyans(db_path)
        
        if empty:
            logger.info(f"{district}: {len(empty)} khatiyans with empty plots")
            all_empty.extend(empty)
    
    if not all_empty:
        logger.info("No khatiyans with empty plots found!")
        return
    
    logger.info(f"\nTotal khatiyans to re-scrape: {len(all_empty)}")
    
    if limit:
        all_empty = all_empty[:limit]
        logger.info(f"Limited to {limit} khatiyans")
    
    if test_mode:
        logger.info("\n=== TEST MODE - Just showing what would be re-scraped ===")
        for kh in all_empty[:20]:
            logger.info(f"  {kh['district']}/{kh['tahasil']}/{kh['village']}/Khatiyan {kh['khatiyan_text']}")
        if len(all_empty) > 20:
            logger.info(f"  ... and {len(all_empty) - 20} more")
        return
    
    # Initialize scraper
    from bhulekh_scraper import BhulekhScraper
    
    scraper = BhulekhScraper()
    await scraper.init_browser(headless=headless)
    await scraper.navigate_to_ror_page()
    
    # Group by district/tahasil/village for efficient navigation
    success_count = 0
    fail_count = 0
    still_empty = 0
    
    current_district = None
    current_tahasil = None
    current_village = None
    
    for i, kh in enumerate(all_empty):
        logger.info(f"\n[{i+1}/{len(all_empty)}] Re-scraping: {kh['district']}/{kh['tahasil']}/{kh['village']}/Khatiyan {kh['khatiyan_text']}")
        
        try:
            ror_data = await rescrape_single_khatiyan(
                scraper,
                kh['district'],
                kh['tahasil'],
                kh['village'],
                kh['khatiyan_value'],
                kh['khatiyan_text'],
            )
            
            if ror_data:
                plots = ror_data.get('plots', [])
                if plots:
                    # Update the database
                    db_path = db_map.get(kh['district'])
                    if db_path and update_khatiyan_in_db(db_path, kh['id'], ror_data):
                        logger.info(f"  ✓ Updated with {len(plots)} plots")
                        success_count += 1
                    else:
                        logger.error(f"  ✗ Failed to update database")
                        fail_count += 1
                else:
                    logger.warning(f"  ⚠ Still no plots (may be legitimate)")
                    still_empty += 1
            else:
                logger.error(f"  ✗ Failed to extract data")
                fail_count += 1
                
        except Exception as e:
            logger.error(f"  ✗ Error: {e}")
            fail_count += 1
            
            # Try to recover by restarting browser
            try:
                await scraper.cleanup()
                await scraper.initialize()
                await scraper.navigate_to_ror_page()
            except:
                pass
    
    await scraper.cleanup()
    
    logger.info(f"\n{'='*60}")
    logger.info(f"RE-SCRAPE COMPLETE")
    logger.info(f"  Successfully updated: {success_count}")
    logger.info(f"  Still empty (legitimate?): {still_empty}")
    logger.info(f"  Failed: {fail_count}")
    logger.info(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Re-scrape only khatiyans with empty plots")
    parser.add_argument("--data-dir", default="bhulekh_data", help="Directory containing district databases")
    parser.add_argument("--district", help="Only process specific district (Odia name)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--limit", type=int, help="Limit number of khatiyans to process")
    parser.add_argument("--test", action="store_true", help="Test mode - just show what would be re-scraped")
    args = parser.parse_args()
    
    data_path = Path(args.data_dir)
    if not data_path.exists():
        print(f"ERROR: Data directory not found: {args.data_dir}")
        sys.exit(1)
    
    asyncio.run(rescrape_empty_plots(
        data_path,
        district_filter=args.district,
        headless=args.headless,
        limit=args.limit,
        test_mode=args.test,
    ))


if __name__ == "__main__":
    main()

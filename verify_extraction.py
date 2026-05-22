#!/usr/bin/env python3
"""
Comprehensive verification of data extraction.

This script:
1. Takes sample khatiyans (with and without plots)
2. Captures full page HTML and screenshots
3. Compares what we extracted vs what's actually on the page
4. Identifies any missing data fields
5. Reports all discrepancies

Usage:
    # Verify extraction for specific khatiyans
    uv run python verify_extraction.py --data-dir bhulekh_data --samples 10
    
    # Verify specific village
    uv run python verify_extraction.py --data-dir bhulekh_data --district "କଟକ" --village "ନରସିଂହପୁର"
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_sample_khatiyans(db_path: Path, limit: int = 10, empty_only: bool = False) -> List[Dict]:
    """Get sample khatiyans from database."""
    samples = []
    try:
        conn = sqlite3.connect(str(db_path))
        
        if empty_only:
            query = """
                SELECT id, district, tahasil, village, khatiyan_value, khatiyan_text, data_json
                FROM khatiyans 
                WHERE json_array_length(json_extract(data_json, '$.plots')) = 0
                LIMIT ?
            """
        else:
            # Get mix of empty and non-empty
            query = """
                SELECT id, district, tahasil, village, khatiyan_value, khatiyan_text, data_json
                FROM khatiyans 
                ORDER BY RANDOM()
                LIMIT ?
            """
        
        cursor = conn.execute(query, (limit,))
        for row in cursor:
            samples.append({
                'id': row[0],
                'district': row[1],
                'tahasil': row[2],
                'village': row[3],
                'khatiyan_value': row[4],
                'khatiyan_text': row[5],
                'stored_data': json.loads(row[6]) if row[6] else {},
            })
        conn.close()
    except Exception as e:
        logger.error(f"Error reading database: {e}")
    return samples


def analyze_html_for_data_fields(html: str) -> Dict[str, List[str]]:
    """
    Analyze HTML to find all potential data fields.
    Returns dict of field categories and their values found.
    """
    findings = {
        'tables': [],
        'labels_and_values': [],
        'potential_plot_tables': [],
        'all_element_ids': [],
        'all_spans_with_data': [],
    }
    
    # Find all element IDs
    id_pattern = re.compile(r'id=["\']([^"\']+)["\']', re.IGNORECASE)
    findings['all_element_ids'] = list(set(id_pattern.findall(html)))
    
    # Find tables
    table_pattern = re.compile(r'<table[^>]*id=["\']([^"\']+)["\'][^>]*>', re.IGNORECASE)
    findings['tables'] = list(set(table_pattern.findall(html)))
    
    # Find spans that might contain data (common pattern in ASP.NET)
    span_pattern = re.compile(r'<span[^>]*id=["\']([^"\']+)["\'][^>]*>([^<]*)</span>', re.IGNORECASE)
    for match in span_pattern.finditer(html):
        span_id, span_text = match.groups()
        if span_text.strip():
            findings['all_spans_with_data'].append({
                'id': span_id,
                'value': span_text.strip()[:100]  # Truncate long values
            })
    
    return findings


def compare_extracted_vs_page(stored_data: Dict, page_html: str, page_text: str) -> Dict[str, Any]:
    """
    Compare what we stored vs what's actually on the page.
    """
    comparison = {
        'stored_fields': {},
        'missing_from_stored': [],
        'empty_in_stored': [],
        'page_has_plots_table': False,
        'page_plot_count_estimate': 0,
        'discrepancies': [],
    }
    
    # Check stored fields
    for key, value in stored_data.items():
        if key == 'plots':
            comparison['stored_fields']['plots_count'] = len(value) if isinstance(value, list) else 0
        else:
            comparison['stored_fields'][key] = bool(value) if value else False
            if not value or value == '' or value == []:
                comparison['empty_in_stored'].append(key)
    
    # Check if page has plot tables
    plot_table_patterns = [
        'gvRorBack', 'gvRorBack2', 'gvplotdetail', 'gvRorFrontBack',
        'lblPlotNo', 'lblAcre', 'lblDecimil', 'lblHector'
    ]
    for pattern in plot_table_patterns:
        if pattern.lower() in page_html.lower():
            comparison['page_has_plots_table'] = True
            break
    
    # Estimate plot count from HTML
    plot_no_pattern = re.compile(r'lblPlotNo[^>]*>([^<]+)<', re.IGNORECASE)
    plot_matches = plot_no_pattern.findall(page_html)
    comparison['page_plot_count_estimate'] = len(plot_matches)
    
    # Check for specific data discrepancies
    if comparison['page_has_plots_table'] and comparison['stored_fields'].get('plots_count', 0) == 0:
        comparison['discrepancies'].append("Page has plot table but stored data has 0 plots")
    
    if comparison['page_plot_count_estimate'] > 0 and comparison['stored_fields'].get('plots_count', 0) == 0:
        comparison['discrepancies'].append(f"Page has ~{comparison['page_plot_count_estimate']} plot numbers but stored 0 plots")
    
    return comparison


async def capture_and_verify_khatiyan(
    scraper,
    khatiyan: Dict,
    output_dir: Path,
) -> Dict[str, Any]:
    """
    Navigate to a khatiyan, capture HTML/screenshot, and compare with stored data.
    """
    from bhulekh_scraper import SELECTOR_DISTRICT, SELECTOR_TAHASIL, SELECTOR_VILLAGE, SELECTOR_KHATIYAN
    
    result = {
        'khatiyan': khatiyan,
        'success': False,
        'html_captured': False,
        'screenshot_captured': False,
        'comparison': None,
        'html_analysis': None,
        'error': None,
    }
    
    try:
        district = khatiyan['district']
        tahasil = khatiyan['tahasil']
        village = khatiyan['village']
        kh_value = khatiyan['khatiyan_value']
        kh_text = khatiyan['khatiyan_text']
        
        logger.info(f"Verifying: {district}/{tahasil}/{village}/Khatiyan {kh_text}")
        
        # Navigate to the khatiyan
        # Wait for page to be ready
        await scraper.human_delay(0.5, 1.0)
        
        # Select district
        district_opts = await scraper.get_dropdown_options(SELECTOR_DISTRICT)
        if not district_opts:
            # Try waiting and fetching again
            await scraper.human_delay(1.0, 1.5)
            district_opts = await scraper.get_dropdown_options(SELECTOR_DISTRICT)
        
        logger.debug(f"Available districts: {[o['text'] for o in district_opts[:5]]}...")
        
        # Try exact match first, then partial match
        district_match = next((o for o in district_opts if o['text'] == district), None)
        if not district_match:
            # Try stripping whitespace
            district_match = next((o for o in district_opts if o['text'].strip() == district.strip()), None)
        if not district_match:
            # Try partial match
            district_match = next((o for o in district_opts if district in o['text'] or o['text'] in district), None)
        
        if not district_match:
            result['error'] = f"District not found: {district}. Available: {[o['text'] for o in district_opts[:10]]}"
            return result
        await scraper.select_dropdown(SELECTOR_DISTRICT, district_match['value'])
        await scraper.human_delay(0.5, 0.8)
        
        # Select tahasil
        tahasil_opts = await scraper.get_dropdown_options(SELECTOR_TAHASIL)
        tahasil_match = next((o for o in tahasil_opts if o['text'] == tahasil), None)
        if not tahasil_match:
            result['error'] = f"Tahasil not found: {tahasil}"
            return result
        await scraper.select_dropdown(SELECTOR_TAHASIL, tahasil_match['value'])
        await scraper.human_delay(0.5, 0.8)
        
        # Select village
        village_opts = await scraper.get_dropdown_options(SELECTOR_VILLAGE)
        village_match = next((o for o in village_opts if o['text'] == village), None)
        if not village_match:
            result['error'] = f"Village not found: {village}"
            return result
        await scraper.select_dropdown(SELECTOR_VILLAGE, village_match['value'])
        await scraper.human_delay(0.5, 0.8)
        
        # Select khatiyan
        await scraper.select_dropdown(SELECTOR_KHATIYAN, kh_value, label=kh_text)
        await scraper.human_delay(0.3, 0.5)
        
        # Click View RoR button
        await scraper.click_view_ror()
        await scraper.human_delay(1.0, 1.5)
        
        # Wait for page to load
        try:
            await scraper.page.wait_for_selector("#gvfront, #gvRorFront, #gvRorBack, table", state="visible", timeout=15000)
        except:
            pass
        
        # Scroll to trigger lazy loading
        await scraper.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await scraper.human_delay(0.5, 1.0)
        
        # Scroll back up to capture full page
        await scraper.page.evaluate("window.scrollTo(0, 0)")
        await scraper.human_delay(0.3, 0.5)
        
        # Capture HTML
        page_html = await scraper.page.content()
        safe_name = f"{khatiyan['id']}_{kh_text}".replace('/', '_').replace(' ', '_')
        html_path = output_dir / f"{safe_name}.html"
        html_path.write_text(page_html, encoding='utf-8')
        result['html_captured'] = True
        result['html_path'] = str(html_path)
        
        # Capture screenshot
        screenshot_path = output_dir / f"{safe_name}.png"
        await scraper.page.screenshot(path=str(screenshot_path), full_page=True)
        result['screenshot_captured'] = True
        result['screenshot_path'] = str(screenshot_path)
        
        # Get page text
        page_text = await scraper.page.locator('body').inner_text()
        
        # Analyze HTML
        result['html_analysis'] = analyze_html_for_data_fields(page_html)
        
        # Compare with stored data
        result['comparison'] = compare_extracted_vs_page(
            khatiyan['stored_data'],
            page_html,
            page_text
        )
        
        # Now extract data using our current extraction code
        try:
            fresh_data = await scraper.extract_ror_data()
            result['fresh_extraction'] = {
                'plots_count': len(fresh_data.get('plots', [])),
                'filled_fields': sum(1 for v in fresh_data.values() if v and v != []),
            }
            
            # Compare fresh extraction with stored
            if len(fresh_data.get('plots', [])) != len(khatiyan['stored_data'].get('plots', [])):
                result['comparison']['discrepancies'].append(
                    f"Fresh extraction got {len(fresh_data.get('plots', []))} plots, stored has {len(khatiyan['stored_data'].get('plots', []))}"
                )
        except Exception as e:
            result['fresh_extraction_error'] = str(e)
        
        # Go back
        await scraper.click_khatiyan_page_button()
        await scraper.human_delay(0.5, 0.8)
        
        result['success'] = True
        
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"Error verifying khatiyan: {e}")
    
    return result


async def run_verification(
    data_dir: Path,
    output_dir: Path,
    district_filter: Optional[str] = None,
    sample_count: int = 10,
    empty_only: bool = False,
    headless: bool = False,
):
    """Main verification function."""
    
    # Find district databases
    db_files = sorted(data_dir.glob("district_*.db"))
    if not db_files:
        logger.error(f"No district databases found in {data_dir}")
        return
    
    # Get samples
    all_samples = []
    for db_path in db_files:
        try:
            conn = sqlite3.connect(str(db_path))
            row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
            conn.close()
            district = row[0] if row else ""
            
            if district_filter and district != district_filter:
                continue
            
            samples = get_sample_khatiyans(db_path, sample_count, empty_only)
            all_samples.extend(samples)
            logger.info(f"{district}: Got {len(samples)} samples")
            
            if len(all_samples) >= sample_count:
                break
        except Exception as e:
            logger.error(f"Error reading {db_path}: {e}")
    
    if not all_samples:
        logger.error("No samples found!")
        return
    
    all_samples = all_samples[:sample_count]
    logger.info(f"\nVerifying {len(all_samples)} khatiyans...\n")
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize scraper
    from bhulekh_scraper import BhulekhScraper
    scraper = BhulekhScraper()
    await scraper.init_browser(headless=headless)
    await scraper.navigate_to_ror_page()
    
    # Verify each sample
    results = []
    for i, khatiyan in enumerate(all_samples):
        logger.info(f"\n[{i+1}/{len(all_samples)}] Processing...")
        result = await capture_and_verify_khatiyan(scraper, khatiyan, output_dir)
        results.append(result)
        
        # Log summary
        if result and result.get('success'):
            comp = result.get('comparison') or {}
            discrepancies = comp.get('discrepancies', [])
            if discrepancies:
                logger.warning(f"  DISCREPANCIES FOUND: {discrepancies}")
            else:
                stored = (comp.get('stored_fields') or {}).get('plots_count', '?')
                page_est = comp.get('page_plot_count_estimate', '?')
                logger.info(f"  OK - Stored plots: {stored}, Page estimate: {page_est}")
        else:
            logger.error(f"  FAILED: {result.get('error', 'Unknown error') if result else 'No result'}")
    
    await scraper.cleanup()
    
    # Generate summary report
    report = {
        'timestamp': datetime.now().isoformat(),
        'samples_checked': len(results),
        'successful': sum(1 for r in results if r and r.get('success')),
        'failed': sum(1 for r in results if r and not r.get('success')),
        'with_discrepancies': sum(1 for r in results if r and (r.get('comparison') or {}).get('discrepancies')),
        'results': results,
    }
    
    report_path = output_dir / "verification_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    
    # Print summary
    print("\n" + "="*60)
    print("VERIFICATION SUMMARY")
    print("="*60)
    print(f"Samples checked: {report['samples_checked']}")
    print(f"Successful: {report['successful']}")
    print(f"Failed: {report['failed']}")
    print(f"With discrepancies: {report['with_discrepancies']}")
    print(f"\nOutput directory: {output_dir}")
    print(f"Full report: {report_path}")
    
    # List discrepancies
    if report['with_discrepancies'] > 0:
        print("\nDISCREPANCIES FOUND:")
        for result in results:
            if not result:
                continue
            comp = result.get('comparison') or {}
            if comp.get('discrepancies'):
                kh = result.get('khatiyan', {})
                print(f"\n  {kh.get('district', '?')}/{kh.get('tahasil', '?')}/{kh.get('village', '?')}/Khatiyan {kh.get('khatiyan_text', '?')}:")
                for d in comp['discrepancies']:
                    print(f"    - {d}")
    
    # List all unique table IDs found
    all_tables = set()
    for result in results:
        if not result:
            continue
        html_analysis = result.get('html_analysis') or {}
        if html_analysis.get('tables'):
            all_tables.update(html_analysis['tables'])
    
    if all_tables:
        print(f"\nTable IDs found across all pages: {sorted(all_tables)}")


def main():
    parser = argparse.ArgumentParser(description="Verify data extraction comprehensively")
    parser.add_argument("--data-dir", default="bhulekh_data", help="Directory containing district databases")
    parser.add_argument("--output-dir", default="verification_output", help="Directory to save captured HTML/screenshots")
    parser.add_argument("--district", help="Only check specific district")
    parser.add_argument("--samples", type=int, default=10, help="Number of samples to check")
    parser.add_argument("--empty-only", action="store_true", help="Only check khatiyans with empty plots")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    args = parser.parse_args()
    
    data_path = Path(args.data_dir)
    output_path = Path(args.output_dir)
    
    if not data_path.exists():
        print(f"ERROR: Data directory not found: {args.data_dir}")
        sys.exit(1)
    
    asyncio.run(run_verification(
        data_path,
        output_path,
        district_filter=args.district,
        sample_count=args.samples,
        empty_only=args.empty_only,
        headless=args.headless,
    ))


if __name__ == "__main__":
    main()

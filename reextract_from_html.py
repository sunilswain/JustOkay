#!/usr/bin/env python3
"""
Re-extract data from stored HTML content.

This script processes records that have stored HTML and re-runs extraction
using the current (potentially improved) extraction logic. This allows fixing
data issues without re-scraping from the website.

Usage:
    python reextract_from_html.py --data-dir bhulekh_data
    python reextract_from_html.py --data-dir bhulekh_data --needs-review-only
    python reextract_from_html.py --data-dir bhulekh_data --dry-run
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
import re

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def extract_from_html(html: str) -> Dict:
    """
    Extract RoR data from raw HTML using BeautifulSoup.
    This mirrors the JavaScript extraction logic but in Python.
    """
    soup = BeautifulSoup(html, 'html.parser')
    data = {'plots': []}
    
    # Detect RoR type
    gvfront = soup.find(id='gvfront')
    if gvfront:
        data['ror_type'] = 'type1'
    else:
        data['ror_type'] = 'type2'
    
    # Extract front page data (common fields)
    front_selectors = {
        'mouja': ['#gvfront_ctl02_lblMouja', '#gvRorFront_ctl02_lblMouja', '[id*="lblMouja"]'],
        'tehsil': ['#gvfront_ctl02_lblTehsil', '#gvRorFront_ctl02_lblTehsil', '[id*="lblTehsil"]'],
        'thana': ['#gvfront_ctl02_lblThana', '#gvRorFront_ctl02_lblThana', '[id*="lblThana"]'],
        'tehsil_no': ['#gvfront_ctl02_lblTesilNo', '#gvRorFront_ctl02_lblTesilNo', '[id*="lblTesilNo"]'],
        'thana_no': ['#gvfront_ctl02_lblThanano', '#gvRorFront_ctl02_lblThanano', '[id*="lblThanano"]'],
        'district': ['#gvfront_ctl02_lblDist', '#gvRorFront_ctl02_lblDist', '[id*="lblDist"]'],
        'landlord_name': ['#gvfront_ctl02_lblLandlordName', '#gvRorFront_ctl02_lblLandlordName', '[id*="lblLandlordName"]', '[id*="lblBhuswami"]'],
        'khatiyan_sl_no': ['#gvfront_ctl02_lblKhatiyanslNo', '#gvRorFront_ctl02_lblKhatiyanslNo', '[id*="lblKhatiyanslNo"]'],
        'tenant_name': ['#gvfront_ctl02_lblName', '#gvRorFront_ctl02_lblName', '[id*="lblName"]', '[id*="lblRaiyat"]'],
        'status': ['#gvfront_ctl02_lblStatua', '#gvRorFront_ctl02_lblStatua', '[id*="lblStatua"]'],
        'water_tax': ['#gvfront_ctl02_lblWaterTax', '[id*="lblWaterTax"]'],
        'tax': ['#gvfront_ctl02_lblTax', '[id*="lblTax"]'],
        'ses': ['#gvfront_ctl02_lblSes', '[id*="lblSes"]'],
        'other_ses': ['#gvfront_ctl02_lblOtherses', '[id*="lblOtherses"]'],
        'total': ['#gvfront_ctl02_lblTotal', '[id*="lblTotal"]'],
        'description': ['#gvfront_ctl02_lblDescription', '[id*="lblDescription"]'],
        'special_case': ['#gvfront_ctl02_lblSpecialCase', '[id*="lblSpecialCase"]'],
        'last_publish_date': ['#gvfront_ctl02_lblLastPublishDate', '[id*="lblLastPublishDate"]'],
        'tax_date': ['#gvfront_ctl02_lblTaxDate', '[id*="lblTaxDate"]'],
    }
    
    for field, selectors in front_selectors.items():
        for sel in selectors:
            elem = soup.select_one(sel)
            if elem:
                text = elem.get_text(strip=True)
                if text:
                    data[field] = text
                    break
    
    # Extract plots from back page table
    table_ids = ['gvRorBack', 'gvRorBack2', 'gvRorFrontBack', 'gvplotdetail']
    table = None
    for tid in table_ids:
        table = soup.find(id=tid)
        if table:
            break
    
    # Fallback: find any table with plot-like content
    if not table:
        for t in soup.find_all('table'):
            if t.find(id=lambda x: x and 'lblPlot' in x):
                table = t
                break
    
    if table:
        rows = table.find_all('tr')
        for row in rows:
            plot = {}
            
            # Plot number - look for specific IDs
            plot_link = row.select_one('a[id*="lblPlotcni"], a[id*="lblPlotNo"]')
            plot_span = row.select_one('span[id*="lblPlotcni"], span[id*="lblPlotNo"]')
            plot_no = ''
            if plot_link:
                plot_no = plot_link.get_text(strip=True)
            elif plot_span:
                plot_no = plot_span.get_text(strip=True)
            
            # Skip if no valid plot number (header rows)
            if not plot_no or not any(c.isdigit() for c in plot_no):
                continue
            
            plot['plot_no'] = plot_no
            
            # Extract other fields
            field_selectors = {
                'chaka': ['[id*="lblchaka"]', '[id*="Chaka"]'],
                'land_type': ['[id*="lblCNItype"]', '[id*="lbllType"]', '[id*="LandType"]'],
                'kisam': ['[id*="lblKisama"]', '[id*="Kisam"]'],
                'n_occu': ['[id*="lbln_occu"]', '[id*="n_occu"]'],
                'e_occu': ['[id*="lble_occu"]', '[id*="e_occu"]'],
                's_occu': ['[id*="lbls_occu"]', '[id*="s_occu"]'],
                'w_occu': ['[id*="lblw_occu"]', '[id*="w_occu"]'],
                'acre': ['[id*="lblAcre"]', '[id*="Acre"]'],
                'decimil': ['[id*="lblDecimil"]', '[id*="Decimil"]'],
                'hector': ['[id*="lblHector"]', '[id*="Hector"]'],
                'remarks': ['[id*="lblPlotRemarks"]', '[id*="Remarks"]'],
            }
            
            for field, selectors in field_selectors.items():
                for sel in selectors:
                    elem = row.select_one(sel)
                    if elem:
                        plot[field] = elem.get_text(strip=True)
                        break
                if field not in plot:
                    plot[field] = ''
            
            data['plots'].append(plot)
    
    return data


def process_database(db_path: Path, needs_review_only: bool = False, dry_run: bool = False) -> Dict:
    """Process a single district database and re-extract from stored HTML."""
    stats = {
        'total': 0,
        'with_html': 0,
        'updated': 0,
        'errors': 0,
        'skipped_no_html': 0,
    }
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        
        # Check if html_content column exists
        cursor.execute("PRAGMA table_info(khatiyans)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'html_content' not in columns:
            logger.warning(f"{db_path.name}: No html_content column - skipping")
            return stats
        
        # Build query
        if needs_review_only:
            query = "SELECT id, data_json, html_content FROM khatiyans WHERE needs_review = 1 AND html_content IS NOT NULL"
        else:
            query = "SELECT id, data_json, html_content FROM khatiyans WHERE html_content IS NOT NULL"
        
        cursor.execute(query)
        rows = cursor.fetchall()
        
        for row_id, data_json, html_content in rows:
            stats['total'] += 1
            
            if not html_content:
                stats['skipped_no_html'] += 1
                continue
            
            stats['with_html'] += 1
            
            try:
                old_data = json.loads(data_json)
                old_plots_count = len(old_data.get('plots', []))
                
                # Re-extract from HTML
                new_data = extract_from_html(html_content)
                new_plots_count = len(new_data.get('plots', []))
                
                # Preserve metadata fields that aren't extracted from HTML
                for field in ['district', 'tahasil', 'village', 'khatiyan_value', 'khatiyan_text']:
                    if field in old_data:
                        new_data[field] = old_data[field]
                
                # Check if extraction improved
                improved = new_plots_count > old_plots_count
                
                if improved or (old_plots_count == 0 and new_plots_count > 0):
                    if not dry_run:
                        new_json = json.dumps(new_data, ensure_ascii=False)
                        # Check if needs_review should be cleared
                        needs_review = 0 if new_plots_count > 0 else 1
                        cursor.execute(
                            "UPDATE khatiyans SET data_json = ?, needs_review = ? WHERE id = ?",
                            (new_json, needs_review, row_id)
                        )
                    stats['updated'] += 1
                    logger.debug(f"  Updated id={row_id}: {old_plots_count} -> {new_plots_count} plots")
                    
            except Exception as e:
                stats['errors'] += 1
                logger.debug(f"  Error processing id={row_id}: {e}")
        
        if not dry_run:
            conn.commit()
        conn.close()
        
    except Exception as e:
        logger.error(f"Error processing {db_path.name}: {e}")
        stats['errors'] += 1
    
    return stats


def main():
    parser = argparse.ArgumentParser(description='Re-extract data from stored HTML')
    parser.add_argument('--data-dir', required=True, help='Directory containing district .db files')
    parser.add_argument('--needs-review-only', action='store_true', 
                        help='Only process records flagged as needs_review')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be updated without making changes')
    args = parser.parse_args()
    
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"Error: {data_dir} is not a directory")
        sys.exit(1)
    
    db_files = sorted(data_dir.glob('district_*.db'))
    if not db_files:
        print(f"No district databases found in {data_dir}")
        sys.exit(1)
    
    print(f"Processing {len(db_files)} district databases...")
    if args.dry_run:
        print("(DRY RUN - no changes will be made)")
    print()
    
    total_stats = {
        'total': 0,
        'with_html': 0,
        'updated': 0,
        'errors': 0,
        'skipped_no_html': 0,
    }
    
    for db_path in db_files:
        district_name = db_path.stem.replace('district_', '')
        stats = process_database(db_path, args.needs_review_only, args.dry_run)
        
        if stats['with_html'] > 0 or stats['updated'] > 0:
            print(f"{district_name}: {stats['with_html']} with HTML, {stats['updated']} updated")
        
        for k, v in stats.items():
            total_stats[k] += v
    
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total records checked: {total_stats['total']}")
    print(f"Records with HTML stored: {total_stats['with_html']}")
    print(f"Records updated: {total_stats['updated']}")
    print(f"Errors: {total_stats['errors']}")
    
    if args.dry_run:
        print()
        print("This was a DRY RUN. Run without --dry-run to apply changes.")


if __name__ == '__main__':
    main()

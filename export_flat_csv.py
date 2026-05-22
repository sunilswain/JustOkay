#!/usr/bin/env python3
"""
Export RoR data to flat CSV format (one row per plot).

Each row contains the khatiyan metadata + one plot's details.
Format matches the user's expected output.

Usage:
    python export_flat_csv.py --data-dir bhulekh_data --output-dir export_csv
    python export_flat_csv.py --data-dir bhulekh_data --output-dir export_csv --district କଟକ
"""

import argparse
import csv
import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional


# Regex to remove illegal characters for CSV
ILLEGAL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]')


def clean_value(value: Any) -> str:
    """Clean a value for CSV output."""
    if value is None:
        return ''
    if isinstance(value, str):
        value = ILLEGAL_CHARS_RE.sub('', value)
        value = value.replace('\x00', '')
        value = value.strip()
    return str(value)


def sanitize_filename(name: str) -> str:
    """Make a string safe for use as filename."""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        name = name.replace(char, '_')
    return name.strip() or "unknown"


def read_sqlite_data(db_path: Path) -> List[Dict[str, Any]]:
    """Read all khatiyan data from a SQLite database."""
    records = []
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT district, tahasil, village, khatiyan_value, khatiyan_text, data_json FROM khatiyans"
        )
        for row in cursor:
            district, tahasil, village, kh_value, kh_text, data_json = row
            try:
                data = json.loads(data_json)
                data['_district'] = district
                data['_tahasil'] = tahasil
                data['_village'] = village
                data['_khatiyan_value'] = kh_value
                data['_khatiyan_text'] = kh_text
                records.append(data)
            except json.JSONDecodeError:
                pass
        conn.close()
    except Exception as e:
        print(f"Error reading {db_path}: {e}")
    return records


def get_district_name_from_db(db_path: Path) -> str:
    """Extract district name from a district database."""
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else db_path.stem
    except:
        return db_path.stem


def flatten_to_rows(records: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Flatten khatiyan records to one row per plot.
    Each row contains khatiyan metadata + plot details.
    """
    rows = []
    
    for kh in records:
        plots = kh.get('plots', [])
        
        # Base row with khatiyan metadata
        base_row = {
            'district': clean_value(kh.get('_district', '') or kh.get('district', '')),
            'mouja': clean_value(kh.get('mouja', '')),
            'tehsil': clean_value(kh.get('_tahasil', '') or kh.get('tehsil', '')),
            'thana': clean_value(kh.get('thana', '')),
            'thana_no': clean_value(kh.get('thana_no', '')),
            'khatiyan_sl_no': clean_value(kh.get('khatiyan_sl_no', '') or kh.get('_khatiyan_text', '')),
            'tenant_name': clean_value(kh.get('tenant_name', '')),
            'landlord_name': clean_value(kh.get('landlord_name', '')),
            'status': clean_value(kh.get('status', '')),
            'tax': clean_value(kh.get('tax', '')),
            'water_tax': clean_value(kh.get('water_tax', '')),
            'total': clean_value(kh.get('total', '')),
            'village': clean_value(kh.get('_village', '')),
        }
        
        if plots:
            # One row per plot
            for plot in plots:
                row = base_row.copy()
                row.update({
                    'plot_no': clean_value(plot.get('plot_no', '')),
                    'plot_chaka': clean_value(plot.get('chaka', '')),
                    'plot_kisam': clean_value(plot.get('kisam', '')),
                    'plot_land_type': clean_value(plot.get('land_type', '')),
                    'plot_acre': clean_value(plot.get('acre', '')),
                    'plot_decimil': clean_value(plot.get('decimil', '')),
                    'plot_hector': clean_value(plot.get('hector', '')),
                    'plot_n_occu': clean_value(plot.get('n_occu', '')),
                    'plot_e_occu': clean_value(plot.get('e_occu', '')),
                    'plot_s_occu': clean_value(plot.get('s_occu', '')),
                    'plot_w_occu': clean_value(plot.get('w_occu', '')),
                    'remark': clean_value(plot.get('remarks', '')),
                })
                rows.append(row)
        else:
            # Khatiyan with no plots - still include it
            row = base_row.copy()
            row.update({
                'plot_no': '',
                'plot_chaka': '',
                'plot_kisam': '',
                'plot_land_type': '',
                'plot_acre': '',
                'plot_decimil': '',
                'plot_hector': '',
                'plot_n_occu': '',
                'plot_e_occu': '',
                'plot_s_occu': '',
                'plot_w_occu': '',
                'remark': '',
            })
            rows.append(row)
    
    return rows


# CSV column order (matching user's expected format)
CSV_COLUMNS = [
    'district',
    'mouja', 
    'tehsil',
    'thana',
    'thana_no',
    'khatiyan_sl_no',
    'tenant_name',
    'plot_no',
    'plot_chaka',
    'plot_kisam',
    'plot_acre',
    'plot_decimil',
    'plot_hector',
    'remark',
    # Additional fields
    'landlord_name',
    'status',
    'tax',
    'water_tax',
    'total',
    'village',
    'plot_land_type',
    'plot_n_occu',
    'plot_e_occu',
    'plot_s_occu',
    'plot_w_occu',
]


def export_to_csv(
    data_dir: str,
    output_dir: str,
    filter_district: Optional[str] = None,
    single_file: bool = False,
):
    """Export data to flat CSV files."""
    data_path = Path(data_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    if not data_path.exists():
        print(f"ERROR: Data directory not found: {data_dir}")
        return
    
    # Find all district databases
    db_files = sorted(data_path.glob("district_*.db"))
    if not db_files:
        print(f"No district databases found in {data_dir}")
        return
    
    print(f"Found {len(db_files)} district databases")
    
    all_rows = []
    
    for db_path in db_files:
        district_name = get_district_name_from_db(db_path)
        
        if filter_district and district_name != filter_district:
            continue
        
        print(f"\nProcessing {district_name}...")
        records = read_sqlite_data(db_path)
        print(f"  Loaded {len(records)} khatiyans")
        
        rows = flatten_to_rows(records)
        print(f"  Flattened to {len(rows)} rows")
        
        if single_file:
            all_rows.extend(rows)
        else:
            # Write per-district CSV
            csv_filename = f"{sanitize_filename(district_name)}.csv"
            csv_path = output_path / csv_filename
            
            with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(rows)
            
            print(f"  Wrote {csv_path} ({len(rows)} rows)")
    
    if single_file and all_rows:
        # Write single combined CSV
        csv_path = output_path / "all_districts.csv"
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nWrote combined file: {csv_path} ({len(all_rows)} rows)")
    
    print(f"\n{'='*60}")
    print("EXPORT COMPLETE")
    print(f"Output directory: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Export RoR data to flat CSV format (one row per plot)"
    )
    parser.add_argument(
        "--data-dir", default="bhulekh_data",
        help="Directory containing district SQLite files (default: bhulekh_data)"
    )
    parser.add_argument(
        "--output-dir", default="export_csv",
        help="Output directory for CSV files (default: export_csv)"
    )
    parser.add_argument(
        "--district", default=None,
        help="Filter to specific district name (Odia text)"
    )
    parser.add_argument(
        "--single-file", action="store_true",
        help="Export all districts to a single CSV file"
    )
    
    args = parser.parse_args()
    
    export_to_csv(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        filter_district=args.district,
        single_file=args.single_file,
    )


if __name__ == "__main__":
    main()

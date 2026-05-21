#!/usr/bin/env python3
"""
Export completed RoR data to folder structure with Excel files.

Structure: output_dir / district / tahasil / village.xlsx

Each village Excel file contains:
- Sheet 1 "Summary": Khatiyan-level summary (one row per khatiyan)
- Sheet 2 "Plots": All plots across all khatiyans (detailed data)

Usage:
    python export_to_folders.py --data-dir bhulekh_data --output-dir export
    python export_to_folders.py --data-dir bhulekh_data --output-dir export --district କଟକ
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import openpyxl
    from openpyxl.utils import get_column_letter
    from openpyxl.styles import Font, Alignment, PatternFill
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False
    print("ERROR: openpyxl is required. Install with: pip install openpyxl")
    sys.exit(1)


def sanitize_filename(name: str) -> str:
    """Make a string safe for use as filename."""
    # Remove/replace invalid characters
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
                data['district'] = district
                data['tahasil'] = tahasil
                data['village'] = village
                data['khatiyan_value'] = kh_value
                data['khatiyan_text'] = kh_text
                records.append(data)
            except json.JSONDecodeError:
                pass
        conn.close()
    except Exception as e:
        print(f"Error reading {db_path}: {e}")
    return records


def read_ndjson_data(ndjson_path: Path) -> List[Dict[str, Any]]:
    """Read all khatiyan data from an NDJSON file."""
    records = []
    try:
        with open(ndjson_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        records.append(data)
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        print(f"Error reading {ndjson_path}: {e}")
    return records


def group_by_village(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, List[Dict]]]]:
    """Group records by district > tahasil > village."""
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for record in records:
        district = record.get('district', 'Unknown')
        tahasil = record.get('tahasil', 'Unknown')
        village = record.get('village', 'Unknown')
        grouped[district][tahasil][village].append(record)
    return grouped


def create_village_excel(
    village_name: str,
    khatiyans: List[Dict[str, Any]],
    output_path: Path
) -> int:
    """Create an Excel file for a village with all its khatiyans.
    
    Returns the number of plots written.
    """
    wb = openpyxl.Workbook()
    
    # --- Sheet 1: Summary (one row per khatiyan) ---
    ws_summary = wb.active
    ws_summary.title = "Summary"
    
    summary_headers = [
        'Khatiyan No', 'Mouja', 'Tehsil', 'District', 'Thana',
        'Landlord Name', 'Tenant Name', 'Status', 'Tax', 'Water Tax',
        'Total', 'Plot Count', 'RoR Type'
    ]
    ws_summary.append(summary_headers)
    
    # Style header row
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col, _ in enumerate(summary_headers, 1):
        cell = ws_summary.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
    
    for kh in khatiyans:
        plots = kh.get('plots', [])
        row = [
            kh.get('khatiyan_text', ''),
            kh.get('mouja', ''),
            kh.get('tehsil', ''),
            kh.get('district', ''),
            kh.get('thana', ''),
            kh.get('landlord_name', ''),
            kh.get('tenant_name', ''),
            kh.get('status', ''),
            kh.get('tax', ''),
            kh.get('water_tax', ''),
            kh.get('total', ''),
            len(plots),
            kh.get('ror_type', ''),
        ]
        ws_summary.append(row)
    
    # Auto-width columns
    for col in range(1, len(summary_headers) + 1):
        ws_summary.column_dimensions[get_column_letter(col)].width = 15
    
    # --- Sheet 2: Plots (all plots from all khatiyans) ---
    ws_plots = wb.create_sheet("Plots")
    
    plot_headers = [
        'Khatiyan No', 'Plot No', 'Chaka', 'Land Type', 'Kisam',
        'N Occu', 'E Occu', 'S Occu', 'W Occu',
        'Acre', 'Decimil', 'Hector', 'Remarks'
    ]
    ws_plots.append(plot_headers)
    
    # Style header row
    for col, _ in enumerate(plot_headers, 1):
        cell = ws_plots.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
    
    total_plots = 0
    for kh in khatiyans:
        kh_no = kh.get('khatiyan_text', '')
        plots = kh.get('plots', [])
        for plot in plots:
            row = [
                kh_no,
                plot.get('plot_no', ''),
                plot.get('chaka', ''),
                plot.get('land_type', ''),
                plot.get('kisam', ''),
                plot.get('n_occu', ''),
                plot.get('e_occu', ''),
                plot.get('s_occu', ''),
                plot.get('w_occu', ''),
                plot.get('acre', ''),
                plot.get('decimil', ''),
                plot.get('hector', ''),
                plot.get('remarks', ''),
            ]
            ws_plots.append(row)
            total_plots += 1
    
    # Auto-width columns
    for col in range(1, len(plot_headers) + 1):
        ws_plots.column_dimensions[get_column_letter(col)].width = 12
    
    # Freeze header rows
    ws_summary.freeze_panes = 'A2'
    ws_plots.freeze_panes = 'A2'
    
    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    wb.close()
    
    return total_plots


def export_data(
    data_dir: str,
    output_dir: str,
    filter_district: Optional[str] = None,
    filter_tahasil: Optional[str] = None,
):
    """Export all data to folder structure with Excel files."""
    data_path = Path(data_dir)
    output_path = Path(output_dir)
    
    if not data_path.exists():
        print(f"ERROR: Data directory not found: {data_dir}")
        return
    
    # Find all data files
    db_files = list(data_path.glob("district_*.db"))
    ndjson_files = list(data_path.glob("district_*.ndjson"))
    
    if not db_files and not ndjson_files:
        print(f"No data files found in {data_dir}")
        return
    
    print(f"Found {len(db_files)} SQLite files and {len(ndjson_files)} NDJSON files")
    
    # Read all data
    all_records = []
    for db_file in db_files:
        print(f"Reading {db_file.name}...")
        records = read_sqlite_data(db_file)
        all_records.extend(records)
        print(f"  -> {len(records)} khatiyans")
    
    for ndjson_file in ndjson_files:
        print(f"Reading {ndjson_file.name}...")
        records = read_ndjson_data(ndjson_file)
        all_records.extend(records)
        print(f"  -> {len(records)} khatiyans")
    
    print(f"\nTotal khatiyans loaded: {len(all_records)}")
    
    # Group by district > tahasil > village
    grouped = group_by_village(all_records)
    
    # Apply filters
    if filter_district:
        if filter_district in grouped:
            grouped = {filter_district: grouped[filter_district]}
        else:
            print(f"District '{filter_district}' not found in data")
            return
    
    # Export
    total_villages = 0
    total_khatiyans = 0
    total_plots = 0
    
    for district, tahasils in sorted(grouped.items()):
        district_safe = sanitize_filename(district)
        
        for tahasil, villages in sorted(tahasils.items()):
            if filter_tahasil and tahasil != filter_tahasil:
                continue
                
            tahasil_safe = sanitize_filename(tahasil)
            
            for village, khatiyans in sorted(villages.items()):
                village_safe = sanitize_filename(village)
                
                # Create Excel file path
                excel_path = output_path / district_safe / tahasil_safe / f"{village_safe}.xlsx"
                
                # Create the Excel file
                plots_count = create_village_excel(village, khatiyans, excel_path)
                
                total_villages += 1
                total_khatiyans += len(khatiyans)
                total_plots += plots_count
                
                print(f"  {district}/{tahasil}/{village}: {len(khatiyans)} khatiyans, {plots_count} plots")
    
    print(f"\n{'='*60}")
    print(f"EXPORT COMPLETE")
    print(f"{'='*60}")
    print(f"Output directory: {output_path}")
    print(f"Villages exported: {total_villages}")
    print(f"Total khatiyans: {total_khatiyans}")
    print(f"Total plots: {total_plots}")


def main():
    parser = argparse.ArgumentParser(
        description="Export RoR data to folder structure with Excel files"
    )
    parser.add_argument(
        "--data-dir", default="bhulekh_data",
        help="Directory containing district SQLite/NDJSON files (default: bhulekh_data)"
    )
    parser.add_argument(
        "--output-dir", default="export",
        help="Output directory for Excel files (default: export)"
    )
    parser.add_argument(
        "--district", default=None,
        help="Filter to specific district name (Odia text)"
    )
    parser.add_argument(
        "--tahasil", default=None,
        help="Filter to specific tahasil name (Odia text)"
    )
    
    args = parser.parse_args()
    
    export_data(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        filter_district=args.district,
        filter_tahasil=args.tahasil,
    )


if __name__ == "__main__":
    main()

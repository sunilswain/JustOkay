#!/usr/bin/env python3
"""
Diagnose missing villages between work_queue.db and district data files.

Compares villages marked as 'done' in work_queue.db against actual data
in district databases to find discrepancies.

Usage:
    uv run python diagnose_missing_villages.py --district 3 --tahasil 5
    uv run python diagnose_missing_villages.py --district 3  # All tahasils in district
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple


def get_queue_villages(
    queue_db: str,
    district_code: int,
    tahasil_code: int = None
) -> List[Dict]:
    """Get all villages from work_queue.db for a district/tahasil."""
    conn = sqlite3.connect(queue_db)
    conn.row_factory = sqlite3.Row
    
    if tahasil_code:
        query = """
            SELECT district_code, district_name, tahasil_code, tahasil_name,
                   village_code, village_name, status, khatiyans_fetched, khatiyan_count
            FROM villages
            WHERE district_code = ? AND tahasil_code = ?
            ORDER BY village_name
        """
        rows = conn.execute(query, (district_code, tahasil_code)).fetchall()
    else:
        query = """
            SELECT district_code, district_name, tahasil_code, tahasil_name,
                   village_code, village_name, status, khatiyans_fetched, khatiyan_count
            FROM villages
            WHERE district_code = ?
            ORDER BY tahasil_name, village_name
        """
        rows = conn.execute(query, (district_code,)).fetchall()
    
    conn.close()
    return [dict(r) for r in rows]


def get_data_villages(data_dir: str, district_code: int, district_name: str = None) -> Dict[str, Set[str]]:
    """
    Get villages that have data in the district database.
    Returns: {tahasil_name: {village_name, ...}}
    """
    data_path = Path(data_dir)
    db_path = None
    
    # List all available district DBs
    all_dbs = list(data_path.glob("district_*.db"))
    print(f"Available district databases: {len(all_dbs)}")
    for db in all_dbs[:10]:  # Show first 10
        print(f"  - {db.name}")
    if len(all_dbs) > 10:
        print(f"  ... and {len(all_dbs) - 10} more")
    
    # Strategy 1: If district_name provided, try direct match
    if district_name:
        # Sanitize name same way as storage.py
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in district_name).strip() or "unknown"
        candidate = data_path / f"district_{safe_name}.db"
        if candidate.exists():
            db_path = candidate
            print(f"Found DB by name: {db_path.name}")
    
    # Strategy 2: Scan all DBs and match by district name inside
    if not db_path:
        for f in all_dbs:
            try:
                conn = sqlite3.connect(str(f))
                row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
                conn.close()
                if row and row[0] == district_name:
                    db_path = f
                    print(f"Found DB by content match: {db_path.name}")
                    break
            except:
                pass
    
    if not db_path:
        print(f"WARNING: District database not found for '{district_name}' (code {district_code})")
        print(f"Try running: ls -la {data_dir}/")
        return {}
    
    villages_by_tahasil = defaultdict(set)
    
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("""
            SELECT DISTINCT tahasil, village, COUNT(*) as kh_count
            FROM khatiyans
            GROUP BY tahasil, village
        """).fetchall()
        conn.close()
        
        for tahasil, village, kh_count in rows:
            villages_by_tahasil[tahasil].add(village)
        
        print(f"Loaded {sum(len(v) for v in villages_by_tahasil.values())} villages from {db_path.name}")
    except Exception as e:
        print(f"Error reading district DB: {e}")
    
    return dict(villages_by_tahasil)


def get_village_khatiyan_count(data_dir: str, district_code: int, tahasil: str, village: str) -> int:
    """Get actual khatiyan count for a village from data DB."""
    data_path = Path(data_dir)
    db_path = data_path / f"district_{district_code}.db"
    
    if not db_path.exists():
        return 0
    
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM khatiyans WHERE tahasil = ? AND village = ?",
            (tahasil, village)
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except:
        return 0


def diagnose(
    queue_db: str,
    data_dir: str,
    district_code: int,
    tahasil_code: int = None
):
    """Run diagnosis comparing work_queue vs actual data."""
    
    print(f"\n{'='*70}")
    print(f"DIAGNOSIS: District {district_code}" + (f", Tahasil {tahasil_code}" if tahasil_code else ""))
    print(f"{'='*70}\n")
    
    # Get villages from work_queue
    queue_villages = get_queue_villages(queue_db, district_code, tahasil_code)
    print(f"Villages in work_queue.db: {len(queue_villages)}")
    
    # Get district name from first village
    district_name = None
    if queue_villages:
        district_name = queue_villages[0].get('district_name')
        print(f"District name: {district_name}")
    
    # Get villages from data DB
    data_villages = get_data_villages(data_dir, district_code, district_name)
    total_data_villages = sum(len(v) for v in data_villages.values())
    print(f"Villages with data in district DB: {total_data_villages}")
    
    # Group queue villages by tahasil
    queue_by_tahasil = defaultdict(list)
    for v in queue_villages:
        queue_by_tahasil[v['tahasil_name']].append(v)
    
    # Analyze discrepancies
    missing_from_data = []  # In queue but not in data
    zero_khatiyans = []     # In queue, marked done, but 0 khatiyans fetched
    
    for tahasil_name, villages in queue_by_tahasil.items():
        data_village_set = data_villages.get(tahasil_name, set())
        
        for v in villages:
            village_name = v['village_name']
            status = v['status']
            fetched = v['khatiyans_fetched'] or 0
            
            in_data = village_name in data_village_set
            
            if status == 'done' and not in_data:
                missing_from_data.append(v)
            
            if status == 'done' and fetched == 0:
                zero_khatiyans.append(v)
    
    # Report
    print(f"\n--- Status Breakdown ---")
    status_counts = defaultdict(int)
    for v in queue_villages:
        status_counts[v['status']] += 1
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")
    
    print(f"\n--- Issues Found ---")
    print(f"Villages marked 'done' but NOT in data DB: {len(missing_from_data)}")
    print(f"Villages marked 'done' with 0 khatiyans fetched: {len(zero_khatiyans)}")
    
    if missing_from_data:
        print(f"\n--- Villages Missing from Data DB (first 20) ---")
        for v in missing_from_data[:20]:
            print(f"  {v['tahasil_name']}/{v['village_name']} - status={v['status']}, fetched={v['khatiyans_fetched']}")
        if len(missing_from_data) > 20:
            print(f"  ... and {len(missing_from_data) - 20} more")
    
    if zero_khatiyans:
        print(f"\n--- Villages with 0 Khatiyans (first 20) ---")
        for v in zero_khatiyans[:20]:
            print(f"  {v['tahasil_name']}/{v['village_name']} - expected={v['khatiyan_count']}")
        if len(zero_khatiyans) > 20:
            print(f"  ... and {len(zero_khatiyans) - 20} more")
    
    # Tahasil-level summary
    if not tahasil_code:
        print(f"\n--- Per-Tahasil Summary ---")
        print(f"{'Tahasil':<30} {'Queue':>8} {'Data':>8} {'Missing':>8}")
        print("-" * 60)
        
        for tahasil_name in sorted(queue_by_tahasil.keys()):
            queue_count = len(queue_by_tahasil[tahasil_name])
            data_count = len(data_villages.get(tahasil_name, set()))
            missing = queue_count - data_count
            
            flag = " ⚠️" if missing > 0 else ""
            print(f"{tahasil_name:<30} {queue_count:>8} {data_count:>8} {missing:>8}{flag}")
    
    print(f"\n{'='*70}")
    print("RECOMMENDATIONS:")
    if missing_from_data:
        print("  - Some villages were marked 'done' but have no data in the DB")
        print("  - This could be because:")
        print("    1. The village had 0 khatiyans on the website")
        print("    2. There was an error during scraping")
        print("    3. The data was saved to a different district DB")
        print("  - To re-scrape these villages, run:")
        print(f"    uv run python work_queue.py --db {queue_db} reset-errors")
    print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="Diagnose missing villages between queue and data")
    parser.add_argument("--queue-db", default="work_queue.db", help="Path to work_queue.db")
    parser.add_argument("--data-dir", default="bhulekh_data", help="Directory with district databases")
    parser.add_argument("--district", "-d", type=int, required=True, help="District code")
    parser.add_argument("--tahasil", "-t", type=int, default=None, help="Tahasil code (optional)")
    
    args = parser.parse_args()
    
    diagnose(
        queue_db=args.queue_db,
        data_dir=args.data_dir,
        district_code=args.district,
        tahasil_code=args.tahasil,
    )


if __name__ == "__main__":
    main()

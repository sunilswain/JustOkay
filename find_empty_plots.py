#!/usr/bin/env python3
"""
Find villages with empty plots across all districts and optionally reset them for re-scraping.

Usage:
    # Just report which villages have empty plots
    python find_empty_plots.py --data-dir bhulekh_data
    
    # Reset affected villages in work queue for re-scraping
    python find_empty_plots.py --data-dir bhulekh_data --queue-db work_queue.db --reset
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple


def scan_district_db(db_path: Path) -> Dict[str, Dict[str, List[str]]]:
    """
    Scan a district database and find khatiyans with empty plots.
    
    Returns: {tahasil: {village: [list of empty khatiyan_texts]}}
    """
    empty_by_village = defaultdict(lambda: defaultdict(list))
    total_khatiyans = 0
    empty_khatiyans = 0
    
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT tahasil, village, khatiyan_text, data_json FROM khatiyans"
        )
        
        for tahasil, village, kh_text, data_json in cursor:
            total_khatiyans += 1
            try:
                data = json.loads(data_json)
                plots = data.get('plots', [])
                if not plots or len(plots) == 0:
                    empty_khatiyans += 1
                    empty_by_village[tahasil][village].append(kh_text)
            except json.JSONDecodeError:
                empty_khatiyans += 1
                empty_by_village[tahasil][village].append(kh_text)
        
        conn.close()
    except Exception as e:
        print(f"  Error reading {db_path}: {e}")
    
    return dict(empty_by_village), total_khatiyans, empty_khatiyans


def get_district_name_from_db(db_path: Path) -> str:
    """Extract district name from a district database."""
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else db_path.stem
    except:
        return db_path.stem


def find_village_ids(queue_db: str, district: str, tahasil: str, villages: Set[str]) -> List[Tuple[int, str]]:
    """Find village IDs in the work queue for given villages."""
    results = []
    try:
        conn = sqlite3.connect(queue_db)
        for village in villages:
            row = conn.execute(
                "SELECT id, village_name FROM villages WHERE district_name = ? AND tahasil_name = ? AND village_name = ?",
                (district, tahasil, village)
            ).fetchone()
            if row:
                results.append((row[0], row[1]))
        conn.close()
    except Exception as e:
        print(f"Error querying work queue: {e}")
    return results


def reset_villages_for_rescrape(queue_db: str, village_ids: List[int]) -> int:
    """Reset villages to pending status and clear their data for re-scraping."""
    try:
        conn = sqlite3.connect(queue_db)
        for vid in village_ids:
            conn.execute("""
                UPDATE villages 
                SET status = 'pending', 
                    khatiyans_fetched = 0,
                    last_khatiyan_no = NULL,
                    worker_id = NULL,
                    claimed_at = NULL,
                    retries = 0
                WHERE id = ?
            """, (vid,))
        conn.commit()
        conn.close()
        return len(village_ids)
    except Exception as e:
        print(f"Error resetting villages: {e}")
        return 0


def delete_village_khatiyans(data_dir: Path, district: str, villages_by_tahasil: Dict[str, Set[str]]) -> int:
    """Delete khatiyans for specific villages from the district database."""
    # Find the district DB file
    db_files = list(data_dir.glob("district_*.db"))
    target_db = None
    
    for db_path in db_files:
        dist_name = get_district_name_from_db(db_path)
        if dist_name == district:
            target_db = db_path
            break
    
    if not target_db:
        print(f"  Could not find database for district: {district}")
        return 0
    
    deleted = 0
    try:
        conn = sqlite3.connect(str(target_db))
        for tahasil, villages in villages_by_tahasil.items():
            for village in villages:
                cursor = conn.execute(
                    "DELETE FROM khatiyans WHERE tahasil = ? AND village = ?",
                    (tahasil, village)
                )
                deleted += cursor.rowcount
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  Error deleting from {target_db}: {e}")
    
    return deleted


def main():
    parser = argparse.ArgumentParser(description="Find and optionally reset villages with empty plots")
    parser.add_argument("--data-dir", default="bhulekh_data", help="Directory containing district databases")
    parser.add_argument("--queue-db", default="work_queue.db", help="Path to work queue database")
    parser.add_argument("--reset", action="store_true", help="Reset affected villages for re-scraping")
    parser.add_argument("--min-empty", type=int, default=1, 
                        help="Minimum empty khatiyans to consider a village affected (default: 1)")
    args = parser.parse_args()
    
    data_path = Path(args.data_dir)
    if not data_path.exists():
        print(f"ERROR: Data directory not found: {args.data_dir}")
        return
    
    # Find all district databases
    db_files = sorted(data_path.glob("district_*.db"))
    if not db_files:
        print(f"No district databases found in {args.data_dir}")
        return
    
    print(f"Scanning {len(db_files)} district databases...\n")
    
    # Scan all districts
    all_affected = {}  # {district: {tahasil: {village: [khatiyans]}}}
    grand_total = 0
    grand_empty = 0
    
    for db_path in db_files:
        district = get_district_name_from_db(db_path)
        empty_data, total, empty = scan_district_db(db_path)
        grand_total += total
        grand_empty += empty
        
        if empty_data:
            all_affected[district] = empty_data
            # Count affected villages
            affected_villages = sum(
                1 for tahasil_data in empty_data.values() 
                for village, khs in tahasil_data.items() 
                if len(khs) >= args.min_empty
            )
            print(f"{district}: {empty}/{total} khatiyans empty across {affected_villages} villages")
        else:
            print(f"{district}: {total} khatiyans, all have plots ✓")
    
    print(f"\n{'='*60}")
    print(f"SUMMARY: {grand_empty}/{grand_total} khatiyans ({100*grand_empty/grand_total:.1f}%) have empty plots")
    print(f"{'='*60}")
    
    if not all_affected:
        print("\nNo villages with empty plots found!")
        return
    
    # Detailed report
    print("\nAFFECTED VILLAGES (with empty plot counts):\n")
    
    villages_to_reset = []  # [(district, tahasil, village, empty_count)]
    
    for district, tahasils in sorted(all_affected.items()):
        print(f"\n{district}:")
        for tahasil, villages in sorted(tahasils.items()):
            for village, empty_khs in sorted(villages.items()):
                if len(empty_khs) >= args.min_empty:
                    print(f"  {tahasil}/{village}: {len(empty_khs)} empty khatiyans")
                    villages_to_reset.append((district, tahasil, village, len(empty_khs)))
    
    print(f"\nTotal villages to reset: {len(villages_to_reset)}")
    
    if args.reset and villages_to_reset:
        print(f"\n{'='*60}")
        print("RESETTING VILLAGES FOR RE-SCRAPING...")
        print(f"{'='*60}")
        
        # Group by district for efficient processing
        by_district = defaultdict(lambda: defaultdict(set))
        for district, tahasil, village, _ in villages_to_reset:
            by_district[district][tahasil].add(village)
        
        total_reset = 0
        total_deleted = 0
        
        for district, tahasils_data in by_district.items():
            print(f"\n{district}:")
            
            # Find village IDs and reset in work queue
            for tahasil, villages in tahasils_data.items():
                village_ids = find_village_ids(args.queue_db, district, tahasil, villages)
                if village_ids:
                    ids = [v[0] for v in village_ids]
                    reset_count = reset_villages_for_rescrape(args.queue_db, ids)
                    total_reset += reset_count
                    print(f"  {tahasil}: Reset {reset_count} villages in work queue")
            
            # Delete old khatiyans from district database
            deleted = delete_village_khatiyans(data_path, district, dict(tahasils_data))
            total_deleted += deleted
            print(f"  Deleted {deleted} old khatiyans from database")
        
        print(f"\n{'='*60}")
        print(f"RESET COMPLETE")
        print(f"  Villages reset in queue: {total_reset}")
        print(f"  Old khatiyans deleted: {total_deleted}")
        print(f"\nRun the scraper to re-fetch these villages with the fixed extraction code.")
    elif villages_to_reset:
        print(f"\nTo reset these villages for re-scraping, run:")
        print(f"  python find_empty_plots.py --data-dir {args.data_dir} --queue-db {args.queue_db} --reset")


if __name__ == "__main__":
    main()

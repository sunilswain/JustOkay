#!/usr/bin/env python3
"""
Reset villages that are marked 'done' but have no data in the district database.

This allows re-scraping only the missing villages without touching ones that have data.

Usage:
    # Preview what would be reset (dry run)
    uv run python reset_missing_villages.py --district 3

    # Actually reset the villages
    uv run python reset_missing_villages.py --district 3 --execute

    # Reset only a specific tahasil
    uv run python reset_missing_villages.py --district 3 --tahasil 5 --execute
"""

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Optional


def get_queue_villages(queue_db: str, district_code: int, tahasil_code: int = None) -> List[Dict]:
    """Get all 'done' villages from work_queue.db."""
    conn = sqlite3.connect(queue_db)
    conn.row_factory = sqlite3.Row
    
    if tahasil_code:
        query = """
            SELECT id, district_code, district_name, tahasil_code, tahasil_name,
                   village_code, village_name, status, khatiyans_fetched
            FROM villages
            WHERE district_code = ? AND tahasil_code = ? AND status = 'done'
        """
        rows = conn.execute(query, (district_code, tahasil_code)).fetchall()
    else:
        query = """
            SELECT id, district_code, district_name, tahasil_code, tahasil_name,
                   village_code, village_name, status, khatiyans_fetched
            FROM villages
            WHERE district_code = ? AND status = 'done'
        """
        rows = conn.execute(query, (district_code,)).fetchall()
    
    conn.close()
    return [dict(r) for r in rows]


def get_data_villages(data_dir: str, district_name: str) -> Set[str]:
    """Get set of 'tahasil/village' keys that have data in district DB."""
    data_path = Path(data_dir)
    
    # Find the district DB
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in district_name).strip()
    db_path = data_path / f"district_{safe_name}.db"
    
    if not db_path.exists():
        # Try scanning
        for f in data_path.glob("district_*.db"):
            try:
                conn = sqlite3.connect(str(f))
                row = conn.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
                conn.close()
                if row and row[0] == district_name:
                    db_path = f
                    break
            except:
                pass
    
    if not db_path.exists():
        print(f"WARNING: No district DB found for {district_name}")
        return set()
    
    villages = set()
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT DISTINCT tahasil, village FROM khatiyans").fetchall()
        conn.close()
        for tahasil, village in rows:
            villages.add(f"{tahasil}/{village}")
    except Exception as e:
        print(f"Error reading district DB: {e}")
    
    return villages


def reset_villages(queue_db: str, village_ids: List[int]) -> int:
    """Reset villages to pending status."""
    conn = sqlite3.connect(queue_db)
    
    for vid in village_ids:
        conn.execute("""
            UPDATE villages 
            SET status = 'pending',
                khatiyans_fetched = 0,
                last_khatiyan_no = NULL,
                worker_id = NULL,
                claimed_at = NULL,
                retries = 0,
                error_msg = NULL
            WHERE id = ?
        """, (vid,))
    
    conn.commit()
    conn.close()
    return len(village_ids)


def main():
    parser = argparse.ArgumentParser(description="Reset villages missing data for re-scraping")
    parser.add_argument("--queue-db", default="work_queue.db", help="Path to work_queue.db")
    parser.add_argument("--data-dir", default="bhulekh_data", help="Directory with district databases")
    parser.add_argument("--district", "-d", type=int, required=True, help="District code")
    parser.add_argument("--tahasil", "-t", type=int, default=None, help="Tahasil code (optional)")
    parser.add_argument("--execute", action="store_true", help="Actually perform the reset (default is dry-run)")
    
    args = parser.parse_args()
    
    print(f"\n{'='*70}")
    print(f"RESET MISSING VILLAGES: District {args.district}" + 
          (f", Tahasil {args.tahasil}" if args.tahasil else ""))
    print(f"{'='*70}\n")
    
    # Get villages from queue
    queue_villages = get_queue_villages(args.queue_db, args.district, args.tahasil)
    print(f"Villages marked 'done' in queue: {len(queue_villages)}")
    
    if not queue_villages:
        print("No villages to process.")
        return
    
    # Get district name
    district_name = queue_villages[0]['district_name']
    print(f"District name: {district_name}")
    
    # Get villages with data
    data_villages = get_data_villages(args.data_dir, district_name)
    print(f"Villages with data in DB: {len(data_villages)}")
    
    # Find missing villages
    missing = []
    for v in queue_villages:
        key = f"{v['tahasil_name']}/{v['village_name']}"
        if key not in data_villages:
            missing.append(v)
    
    print(f"\nVillages to reset (missing data): {len(missing)}")
    
    if not missing:
        print("All villages have data. Nothing to reset.")
        return
    
    # Group by tahasil for display
    by_tahasil = defaultdict(list)
    for v in missing:
        by_tahasil[v['tahasil_name']].append(v)
    
    print(f"\nBreakdown by tahasil:")
    for tahasil in sorted(by_tahasil.keys()):
        villages = by_tahasil[tahasil]
        total_fetched = sum(v['khatiyans_fetched'] or 0 for v in villages)
        print(f"  {tahasil}: {len(villages)} villages (claimed {total_fetched} khatiyans)")
    
    if args.execute:
        print(f"\n{'='*70}")
        print("EXECUTING RESET...")
        print(f"{'='*70}")
        
        village_ids = [v['id'] for v in missing]
        count = reset_villages(args.queue_db, village_ids)
        
        print(f"\nReset {count} villages to 'pending' status.")
        print("Restart the scraper to re-fetch these villages.")
    else:
        print(f"\n{'='*70}")
        print("DRY RUN - No changes made")
        print(f"{'='*70}")
        print(f"\nTo actually reset these {len(missing)} villages, run:")
        print(f"  uv run python reset_missing_villages.py --district {args.district}" +
              (f" --tahasil {args.tahasil}" if args.tahasil else "") + " --execute")


if __name__ == "__main__":
    main()

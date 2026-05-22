#!/usr/bin/env python3
"""
Rebuild work_queue.db status from bhulekh_data.

This script scans the district databases in bhulekh_data/ and marks
villages as 'completed' in work_queue.db if they have scraped data.

Usage:
    python rebuild_queue_status.py --data-dir bhulekh_data --queue-db work_queue.db
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from collections import defaultdict


def get_completed_villages_from_data(data_dir: Path) -> dict:
    """
    Scan all district DBs and return villages with their khatiyan counts.
    Returns: {(district, tahasil, village): khatiyan_count}
    """
    villages = defaultdict(int)
    
    for db_file in data_dir.glob("district_*.db"):
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()
            
            # Count khatiyans per village
            cursor.execute("""
                SELECT district, tahasil, village, COUNT(*) as cnt
                FROM khatiyans
                GROUP BY district, tahasil, village
            """)
            
            for district, tahasil, village, count in cursor.fetchall():
                villages[(district, tahasil, village)] = count
            
            conn.close()
            print(f"  Scanned {db_file.name}: found {len(villages)} villages so far")
        except Exception as e:
            print(f"  Error reading {db_file.name}: {e}")
    
    return villages


def update_queue_status(queue_db: Path, completed_villages: dict) -> tuple:
    """
    Update work_queue.db to mark villages as completed if they have data.
    Returns: (matched, updated, not_found)
    """
    conn = sqlite3.connect(str(queue_db))
    cursor = conn.cursor()
    
    matched = 0
    updated = 0
    not_found = []
    
    for (district, tahasil, village), khatiyan_count in completed_villages.items():
        # Try to find the village in work_queue
        cursor.execute("""
            SELECT id, status, khatiyans_fetched 
            FROM villages 
            WHERE district_name = ? AND tahasil_name = ? AND village_name = ?
        """, (district, tahasil, village))
        
        row = cursor.fetchone()
        if row:
            matched += 1
            vid, status, current_fetched = row
            
            # Update if not already done or if we have more khatiyans
            if status != 'done' or (current_fetched or 0) < khatiyan_count:
                cursor.execute("""
                    UPDATE villages 
                    SET status = 'done', 
                        khatiyans_fetched = ?,
                        completed_at = datetime('now')
                    WHERE id = ?
                """, (khatiyan_count, vid))
                updated += 1
        else:
            not_found.append((district, tahasil, village, khatiyan_count))
    
    conn.commit()
    conn.close()
    
    return matched, updated, not_found


def main():
    parser = argparse.ArgumentParser(description='Rebuild queue status from scraped data')
    parser.add_argument('--data-dir', required=True, help='Directory with district .db files')
    parser.add_argument('--queue-db', required=True, help='Path to work_queue.db')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be updated')
    args = parser.parse_args()
    
    data_dir = Path(args.data_dir)
    queue_db = Path(args.queue_db)
    
    if not data_dir.is_dir():
        print(f"Error: {data_dir} is not a directory")
        sys.exit(1)
    
    if not queue_db.exists():
        print(f"Error: {queue_db} does not exist. Run 'python work_queue.py create' first.")
        sys.exit(1)
    
    print(f"Scanning {data_dir} for completed villages...")
    completed = get_completed_villages_from_data(data_dir)
    
    print(f"\nFound {len(completed)} villages with data")
    total_khatiyans = sum(completed.values())
    print(f"Total khatiyans in data: {total_khatiyans:,}")
    
    if args.dry_run:
        print("\n(DRY RUN - no changes made)")
        return
    
    print(f"\nUpdating {queue_db}...")
    matched, updated, not_found = update_queue_status(queue_db, completed)
    
    print(f"\nResults:")
    print(f"  Villages matched in queue: {matched}")
    print(f"  Villages updated to completed: {updated}")
    print(f"  Villages not found in queue: {len(not_found)}")
    
    if not_found and len(not_found) <= 20:
        print("\n  Not found villages (data exists but not in queue):")
        for d, t, v, c in not_found[:20]:
            print(f"    {d}/{t}/{v}: {c} khatiyans")
    
    print("\nDone! Run 'python work_queue.py stats' to verify.")


if __name__ == '__main__':
    main()

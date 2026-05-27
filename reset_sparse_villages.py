#!/usr/bin/env python3
"""
Find villages where every saved khatiyan is empty (no plots, landlord, or tenant)
and reset them for re-scraping.

Usage:
    # Preview affected villages
    python reset_sparse_villages.py --data-dir bhulekh_data --queue-db work_queue.db

    # Reset queue status and delete bad records
    python reset_sparse_villages.py --data-dir bhulekh_data --queue-db work_queue.db --execute
"""

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def is_empty_khatiyan(data_json: str) -> bool:
    """True when extraction has no plots, landlord, or tenant."""
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError:
        return True

    plots = data.get("plots") or []
    landlord = (data.get("landlord_name") or "").strip()
    tenant = (data.get("tenant_name") or "").strip()
    return not plots and not landlord and not tenant


def scan_district_db(db_path: Path) -> Tuple[str, List[Tuple[str, str, int]]]:
    """
    Scan a district DB for villages where ALL khatiyans are empty.

    Returns (district_name, [(tahasil, village, khatiyan_count), ...]).
    """
    sparse_villages: List[Tuple[str, str, int]] = []
    district_name = db_path.stem.removeprefix("district_")

    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT district, tahasil, village, data_json FROM khatiyans"
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"  Error reading {db_path}: {e}")
        return district_name, sparse_villages

    if not rows:
        return district_name, sparse_villages

    district_name = rows[0][0] or district_name
    by_village: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for _, tahasil, village, data_json in rows:
        by_village[(tahasil, village)].append(data_json)

    for (tahasil, village), jsons in by_village.items():
        if jsons and all(is_empty_khatiyan(j) for j in jsons):
            sparse_villages.append((tahasil, village, len(jsons)))

    return district_name, sparse_villages


def find_village_row(
    queue_db: str, district: str, tahasil: str, village: str
) -> Optional[Tuple[int, str]]:
    """Return (id, status) for a village in the work queue, if present."""
    try:
        conn = sqlite3.connect(queue_db)
        row = conn.execute(
            """
            SELECT id, status FROM villages
            WHERE district_name = ? AND tahasil_name = ? AND village_name = ?
            """,
            (district, tahasil, village),
        ).fetchone()
        conn.close()
        return row if row else None
    except Exception as e:
        print(f"Error querying work queue: {e}")
        return None


def reset_villages_in_queue(queue_db: str, village_ids: List[int]) -> int:
    """Reset villages from done/error to pending for re-scraping."""
    if not village_ids:
        return 0
    try:
        conn = sqlite3.connect(queue_db)
        for vid in village_ids:
            conn.execute(
                """
                UPDATE villages
                SET status = 'pending',
                    khatiyans_fetched = 0,
                    last_khatiyan_no = NULL,
                    worker_id = NULL,
                    claimed_at = NULL,
                    retries = 0,
                    error_msg = NULL
                WHERE id = ?
                """,
                (vid,),
            )
        conn.commit()
        conn.close()
        return len(village_ids)
    except Exception as e:
        print(f"Error resetting villages: {e}")
        return 0


def delete_village_khatiyans(
    db_path: Path, tahasil: str, village: str
) -> int:
    """Delete all khatiyans for one village from a district DB."""
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "DELETE FROM khatiyans WHERE tahasil = ? AND village = ?",
            (tahasil, village),
        )
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception as e:
        print(f"  Error deleting from {db_path}: {e}")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset villages where all khatiyans have empty extractions"
    )
    parser.add_argument("--data-dir", default="bhulekh_data", help="District DB directory")
    parser.add_argument("--queue-db", default="work_queue.db", help="Work queue database")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Reset queue rows and delete bad khatiyans (default is dry run)",
    )
    args = parser.parse_args()

    data_path = Path(args.data_dir)
    if not data_path.exists():
        print(f"ERROR: Data directory not found: {args.data_dir}")
        return

    db_files = sorted(data_path.glob("district_*.db"))
    if not db_files:
        print(f"No district databases found in {args.data_dir}")
        return

    print(f"Scanning {len(db_files)} district databases for all-empty villages...\n")

    all_sparse: List[Tuple[str, str, str, int, Optional[str]]] = []
    for db_path in db_files:
        district, sparse = scan_district_db(db_path)
        if sparse:
            print(f"{district}: {len(sparse)} all-empty villages")
            for tahasil, village, count in sparse:
                row = find_village_row(args.queue_db, district, tahasil, village)
                status = row[1] if row else None
                all_sparse.append((district, tahasil, village, count, status))
        else:
            print(f"{district}: no all-empty villages")

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {len(all_sparse)} villages with only empty khatiyans")
    print(f"{'=' * 60}")

    if not all_sparse:
        print("\nNothing to reset.")
        return

    done_count = sum(1 for *_, status in all_sparse if status == "done")
    print(f"  In queue as 'done': {done_count}")
    print(f"  Other / not in queue: {len(all_sparse) - done_count}")

    print("\nAFFECTED VILLAGES:\n")
    for district, tahasil, village, count, status in sorted(all_sparse):
        queue_note = status or "not in queue"
        print(f"  {district} / {tahasil} / {village}: {count} empty khatiyans [{queue_note}]")

    if not args.execute:
        print(f"\nDry run — no changes made.")
        print(
            f"To reset, run:\n"
            f"  python reset_sparse_villages.py "
            f"--data-dir {args.data_dir} --queue-db {args.queue_db} --execute"
        )
        return

    print(f"\n{'=' * 60}")
    print("RESETTING SPARSE VILLAGES...")
    print(f"{'=' * 60}")

    db_by_district: Dict[str, Path] = {}
    for db_path in db_files:
        district, _ = scan_district_db(db_path)
        db_by_district[district] = db_path

    reset_ids: List[int] = []
    total_deleted = 0

    for district, tahasil, village, count, status in all_sparse:
        row = find_village_row(args.queue_db, district, tahasil, village)
        if row and row[1] == "done":
            reset_ids.append(row[0])

        db_path = db_by_district.get(district)
        if db_path:
            deleted = delete_village_khatiyans(db_path, tahasil, village)
            total_deleted += deleted
            print(f"  {district}/{tahasil}/{village}: deleted {deleted} records")

    reset_count = reset_villages_in_queue(args.queue_db, reset_ids)

    print(f"\n{'=' * 60}")
    print("RESET COMPLETE")
    print(f"  Villages reset in queue: {reset_count}")
    print(f"  Khatiyan records deleted: {total_deleted}")
    print(f"\nRun the scraper to re-fetch these villages with the fixed code.")


if __name__ == "__main__":
    main()

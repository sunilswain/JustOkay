#!/usr/bin/env python3
"""
Reset villages that are marked 'done' but have no data in the district database.

Re-queued villages keep their existing priority, so high-priority districts
are still processed first.

Usage:
    # Preview ALL districts (dry run)
    uv run python reset_missing_villages.py

    # Actually reset ALL missing villages
    uv run python reset_missing_villages.py --execute

    # Preview/reset a specific district
    uv run python reset_missing_villages.py --district 3
    uv run python reset_missing_villages.py --district 3 --execute

    # Preview/reset a specific tahasil
    uv run python reset_missing_villages.py --district 3 --tahasil 5 --execute
"""

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple


def get_all_district_codes(queue_db: str) -> List[Tuple[int, str]]:
    """Get all distinct (code, name) pairs from the queue."""
    conn = sqlite3.connect(queue_db)
    rows = conn.execute(
        "SELECT DISTINCT district_code, district_name FROM villages ORDER BY district_code"
    ).fetchall()
    conn.close()
    return rows


def get_done_villages(queue_db: str, district_code: int, tahasil_code: int = None) -> List[Dict]:
    """Get all 'done' villages from work_queue.db for a district."""
    conn = sqlite3.connect(queue_db)
    conn.row_factory = sqlite3.Row

    params: list = [district_code]
    tahasil_filter = ""
    if tahasil_code is not None:
        tahasil_filter = "AND tahasil_code = ?"
        params.append(tahasil_code)

    rows = conn.execute(f"""
        SELECT id, district_code, district_name, tahasil_code, tahasil_name,
               village_code, village_name, status, khatiyans_fetched, priority
        FROM villages
        WHERE district_code = ? AND status = 'done' {tahasil_filter}
    """, params).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_data_villages(data_dir: str, district_name: str) -> Set[str]:
    """Get set of 'tahasil/village' keys that have data in district DB."""
    data_path = Path(data_dir)

    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in district_name).strip()
    db_path = data_path / f"district_{safe_name}.db"

    if not db_path.exists():
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
        return set()

    villages = set()
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT DISTINCT tahasil, village FROM khatiyans").fetchall()
        conn.close()
        for tahasil, village in rows:
            villages.add(f"{tahasil}/{village}")
    except Exception:
        pass

    return villages


def reset_villages(queue_db: str, village_ids: List[int]) -> int:
    """Reset villages to pending status (keeps existing priority!)."""
    if not village_ids:
        return 0
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


def scan_district(queue_db: str, data_dir: str, district_code: int,
                  district_name: str, tahasil_code: int = None) -> List[Dict]:
    """Find villages marked done but missing from data DB."""
    done_villages = get_done_villages(queue_db, district_code, tahasil_code)
    if not done_villages:
        return []

    data_villages = get_data_villages(data_dir, district_name)

    missing = []
    for v in done_villages:
        key = f"{v['tahasil_name']}/{v['village_name']}"
        if key not in data_villages:
            missing.append(v)

    return missing


def main():
    parser = argparse.ArgumentParser(
        description="Reset villages marked 'done' but missing data (respects priority)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  uv run python reset_missing_villages.py                         # Preview ALL districts
  uv run python reset_missing_villages.py --execute               # Reset ALL missing villages
  uv run python reset_missing_villages.py -d 3                    # Preview district 3
  uv run python reset_missing_villages.py -d 3 --execute          # Reset district 3
  uv run python reset_missing_villages.py -d 3 -t 5 --execute     # Reset district 3 tahasil 5
""")
    parser.add_argument("--queue-db", default="work_queue.db")
    parser.add_argument("--data-dir", default="bhulekh_data")
    parser.add_argument("--district", "-d", type=int, default=None,
                        help="District code (omit to scan ALL)")
    parser.add_argument("--tahasil", "-t", type=int, default=None)
    parser.add_argument("--execute", action="store_true",
                        help="Actually reset (default is dry-run preview)")
    args = parser.parse_args()

    print(f"\n{'='*80}")
    if args.district:
        title = f"RESET MISSING VILLAGES: District {args.district}"
        if args.tahasil:
            title += f", Tahasil {args.tahasil}"
    else:
        title = "RESET MISSING VILLAGES: ALL DISTRICTS"
    print(f"  {title}")
    print(f"{'='*80}")

    # Determine which districts to scan
    if args.district:
        # Single district
        conn = sqlite3.connect(args.queue_db)
        row = conn.execute(
            "SELECT DISTINCT district_name FROM villages WHERE district_code = ? LIMIT 1",
            (args.district,)
        ).fetchone()
        conn.close()
        if not row:
            print(f"\nDistrict code {args.district} not found in queue.")
            return
        districts = [(args.district, row[0])]
    else:
        districts = get_all_district_codes(args.queue_db)

    # Scan each district
    all_missing = []
    print(f"\n  {'Code':>4}  {'Pri':>4}  {'District':<25}  {'Done':>6}  {'In DB':>6}  {'Missing':>7}")
    print(f"  {'-'*4}  {'-'*4}  {'-'*25}  {'-'*6}  {'-'*6}  {'-'*7}")

    for d_code, d_name in districts:
        done = get_done_villages(args.queue_db, d_code, args.tahasil)
        data = get_data_villages(args.data_dir, d_name)
        missing = scan_district(args.queue_db, args.data_dir, d_code, d_name, args.tahasil)

        pri = done[0]['priority'] if done else 0
        flag = " ***" if missing else ""
        print(f"  {d_code:>4}  {pri:>4}  {d_name:<25}  {len(done):>6}  {len(data):>6}  {len(missing):>7}{flag}")

        all_missing.extend(missing)

    print(f"\n  TOTAL missing: {len(all_missing)} villages across {len(districts)} districts")

    if not all_missing:
        print("\n  All done villages have data. Nothing to reset!")
        return

    # Breakdown by district+tahasil
    by_dist_tah = defaultdict(list)
    for v in all_missing:
        by_dist_tah[(v['district_name'], v['tahasil_name'])].append(v)

    print(f"\n  Breakdown:")
    for (dist, tah), vils in sorted(by_dist_tah.items()):
        pri = vils[0]['priority'] if vils else 0
        print(f"    [{pri:>3}] {dist} / {tah}: {len(vils)} villages")

    if args.execute:
        print(f"\n{'='*80}")
        print("  EXECUTING RESET...")
        print(f"{'='*80}")

        village_ids = [v['id'] for v in all_missing]
        count = reset_villages(args.queue_db, village_ids)

        print(f"\n  Reset {count} villages to 'pending'.")
        print("  Priority is preserved — high-priority districts will be picked first.")
        print("  Restart the scraper service to begin re-fetching.")
    else:
        print(f"\n{'='*80}")
        print("  DRY RUN — no changes made")
        print(f"{'='*80}")
        cmd = "  uv run python reset_missing_villages.py"
        if args.district:
            cmd += f" -d {args.district}"
        if args.tahasil:
            cmd += f" -t {args.tahasil}"
        cmd += " --execute"
        print(f"\n  To reset, run:\n  {cmd}")


if __name__ == "__main__":
    main()

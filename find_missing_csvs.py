#!/usr/bin/env python3
"""
Find villages marked 'done' in work_queue but missing from the district DB.
These are villages that would NOT produce a CSV on export.

Usage:
    python find_missing_csvs.py --district 18
    python find_missing_csvs.py --district 18 --tahasil 2
"""
import argparse
import sqlite3
import json
import sys
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Find done villages missing from DB")
    parser.add_argument("--district", type=int, required=True, help="District code")
    parser.add_argument("--tahasil", type=int, help="Tahasil code (optional)")
    parser.add_argument("--queue-db", default="work_queue.db")
    parser.add_argument("--data-dir", default="bhulekh_data")
    args = parser.parse_args()

    qconn = sqlite3.connect(args.queue_db)

    filter_sql = "AND tahasil_code = ?" if args.tahasil else ""
    params = [args.district]
    if args.tahasil:
        params.append(args.tahasil)

    done_rows = qconn.execute(f"""
        SELECT tahasil_code, tahasil_name, village_name, khatiyan_count, khatiyans_fetched
        FROM villages
        WHERE district_code = ? AND status = 'done'
        {filter_sql}
        ORDER BY tahasil_code, village_name
    """, params).fetchall()

    district_name = qconn.execute(
        "SELECT district_name FROM villages WHERE district_code = ? LIMIT 1",
        (args.district,),
    ).fetchone()
    district_name = district_name[0] if district_name else f"code_{args.district}"
    qconn.close()

    # Find the district DB
    data_dir = Path(args.data_dir)
    district_db = None
    for f in data_dir.glob("district_*.db"):
        try:
            c = sqlite3.connect(str(f))
            r = c.execute("SELECT DISTINCT district FROM khatiyans LIMIT 1").fetchone()
            c.close()
            if r and district_name in (r[0] or ""):
                district_db = f
                break
        except Exception:
            continue

    if not district_db:
        print(f"ERROR: No district DB found for {district_name}")
        return

    dconn = sqlite3.connect(str(district_db))
    db_village_kh = defaultdict(int)
    for tah, vil in dconn.execute("SELECT tahasil, village FROM khatiyans").fetchall():
        db_village_kh[(tah, vil)] += 1
    dconn.close()

    by_tahasil = defaultdict(list)
    for tcode, tname, vname, expected, fetched in done_rows:
        by_tahasil[(tcode, tname)].append((vname, expected, fetched))

    total_missing = 0
    total_empty = 0

    for (tcode, tname), villages in sorted(by_tahasil.items()):
        missing = []
        empty_data = []
        for vname, expected, fetched in villages:
            kh_in_db = db_village_kh.get((tname, vname), 0)
            if kh_in_db == 0:
                missing.append((vname, expected, fetched))
            elif fetched == 0 and kh_in_db == 0:
                empty_data.append((vname, expected, fetched, kh_in_db))

        db_count = sum(1 for v, _, _ in villages if db_village_kh.get((tname, v), 0) > 0)

        print(f"\n{'='*60}")
        print(f"Tahasil: {tname} (code {tcode})")
        print(f"  Queue done: {len(villages)} | DB has data: {db_count} | Missing: {len(missing)}")

        if missing:
            print(f"\n  Villages done but NO data in DB:")
            for vname, expected, fetched in missing:
                print(f"    {vname}  (expected={expected}, queue_fetched={fetched})")
            total_missing += len(missing)

    print(f"\n{'='*60}")
    print(f"TOTAL: {total_missing} villages marked done but missing from DB")
    if total_missing > 0:
        print(f"\nTo reset these, run reset_sparse_villages.py or manually update work_queue.db")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Seed progress/*.done markers from work_queue.db (one-time migration).

Reads villages with status='done' and creates the corresponding
progress/{district_code}/{tahasil_code}/{village_code}.done files
so scraper_v3 skips already-scraped villages.
"""

import argparse
import os
import sqlite3
import time


def seed_progress(db_path: str, progress_dir: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT district_code, tahasil_code, village_code, "
        "khatiyans_fetched, khatiyan_count "
        "FROM villages WHERE status = 'done'"
    ).fetchall()
    conn.close()

    created = 0
    skipped = 0

    for row in rows:
        d_code = row["district_code"]
        t_code = row["tahasil_code"]
        v_code = row["village_code"]
        done_path = os.path.join(
            progress_dir, str(d_code), str(t_code), f"{v_code}.done"
        )

        if os.path.exists(done_path):
            skipped += 1
            continue

        khatiyans = row["khatiyans_fetched"] or row["khatiyan_count"] or 0
        os.makedirs(os.path.dirname(done_path), exist_ok=True)
        with open(done_path, "w") as f:
            f.write(f"{khatiyans}\n{time.time()}\n")
        created += 1

    print(f"Seeded progress from {db_path}")
    print(f"  Total done villages in DB: {len(rows)}")
    print(f"  .done files created:     {created}")
    print(f"  Already existed (skip):  {skipped}")


def main():
    parser = argparse.ArgumentParser(description="Seed .done files from work_queue.db")
    parser.add_argument("--db", default="work_queue.db", help="Path to work_queue.db")
    parser.add_argument("--progress-dir", default="progress", help="Progress directory")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}")
        raise SystemExit(1)

    os.makedirs(args.progress_dir, exist_ok=True)
    seed_progress(args.db, args.progress_dir)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Show what workers are currently working on.

Displays all in-progress villages with their worker, district, tahasil, and priority.

Usage:
    uv run python show_workers.py
    uv run python show_workers.py --watch          # Auto-refresh every 5 seconds
    uv run python show_workers.py --watch --interval 10
"""

import argparse
import sqlite3
import time
import os
from datetime import datetime
from pathlib import Path


def clear_screen():
    """Clear terminal screen."""
    os.system('clear' if os.name != 'nt' else 'cls')


def get_in_progress_villages(queue_db: str) -> list:
    """Get all villages currently being processed."""
    conn = sqlite3.connect(queue_db)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("""
        SELECT 
            id,
            district_code,
            district_name,
            tahasil_code,
            tahasil_name,
            village_code,
            village_name,
            worker_id,
            claimed_at,
            khatiyans_fetched,
            khatiyan_count,
            priority,
            retries
        FROM villages
        WHERE status = 'in_progress'
        ORDER BY priority DESC, claimed_at ASC
    """).fetchall()
    
    conn.close()
    return [dict(r) for r in rows]


def get_queue_summary(queue_db: str) -> dict:
    """Get overall queue status."""
    conn = sqlite3.connect(queue_db)
    
    row = conn.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END) as in_progress,
            SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) as done,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error
        FROM villages
    """).fetchone()
    
    conn.close()
    return {
        'total': row[0],
        'pending': row[1],
        'in_progress': row[2],
        'done': row[3],
        'error': row[4],
    }


def get_pending_by_priority(queue_db: str, limit: int = 10) -> list:
    """Get pending villages ordered by priority (what will be picked next)."""
    conn = sqlite3.connect(queue_db)
    conn.row_factory = sqlite3.Row
    
    rows = conn.execute("""
        SELECT 
            district_code,
            district_name,
            tahasil_code,
            tahasil_name,
            village_name,
            priority,
            khatiyan_count
        FROM villages
        WHERE status = 'pending'
        ORDER BY priority DESC, id ASC
        LIMIT ?
    """, (limit,)).fetchall()
    
    conn.close()
    return [dict(r) for r in rows]


def format_time_ago(iso_time: str) -> str:
    """Format ISO timestamp as 'X min ago'."""
    if not iso_time:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_time.replace('Z', '+00:00'))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        diff = now - dt
        minutes = int(diff.total_seconds() / 60)
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        return f"{hours}h {minutes % 60}m ago"
    except:
        return "?"


def show_status(queue_db: str):
    """Display current worker status."""
    summary = get_queue_summary(queue_db)
    in_progress = get_in_progress_villages(queue_db)
    pending_next = get_pending_by_priority(queue_db, limit=10)
    
    print(f"\n{'='*100}")
    print(f"  WORKER STATUS  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*100}")
    
    # Summary
    print(f"\n  Queue: {summary['total']} total | "
          f"{summary['pending']} pending | "
          f"{summary['in_progress']} in_progress | "
          f"{summary['done']} done | "
          f"{summary['error']} error")
    
    # In-progress villages
    print(f"\n{'─'*100}")
    print(f"  WORKERS ACTIVE: {len(in_progress)}")
    print(f"{'─'*100}")
    
    if in_progress:
        print(f"\n  {'Worker':<25} {'Pri':>4} {'D':>3} {'T':>3} {'District':<15} {'Tahasil':<20} {'Village':<25} {'Progress':<12}")
        print(f"  {'-'*25} {'-'*4} {'-'*3} {'-'*3} {'-'*15} {'-'*20} {'-'*25} {'-'*12}")
        
        for v in in_progress:
            worker = v['worker_id'] or '?'
            # Shorten worker ID for display
            if len(worker) > 25:
                worker = worker[-25:]
            
            fetched = v['khatiyans_fetched'] or 0
            total = v['khatiyan_count'] or 0
            progress = f"{fetched}/{total}" if total else f"{fetched}/?"
            
            print(f"  {worker:<25} {v['priority'] or 0:>4} {v['district_code']:>3} {v['tahasil_code']:>3} "
                  f"{v['district_name'][:15]:<15} {v['tahasil_name'][:20]:<20} "
                  f"{v['village_name'][:25]:<25} {progress:<12}")
    else:
        print("\n  No workers currently active!")
    
    # Next in queue (what will be picked)
    print(f"\n{'─'*100}")
    print(f"  NEXT IN QUEUE (by priority):")
    print(f"{'─'*100}")
    
    if pending_next:
        print(f"\n  {'Pri':>4} {'D':>3} {'T':>3} {'District':<15} {'Tahasil':<25} {'Village':<30} {'Est.Kh':>8}")
        print(f"  {'-'*4} {'-'*3} {'-'*3} {'-'*15} {'-'*25} {'-'*30} {'-'*8}")
        
        for v in pending_next:
            print(f"  {v['priority'] or 0:>4} {v['district_code']:>3} {v['tahasil_code']:>3} "
                  f"{v['district_name'][:15]:<15} {v['tahasil_name'][:25]:<25} "
                  f"{v['village_name'][:30]:<30} {v['khatiyan_count'] or 0:>8}")
    else:
        print("\n  No pending villages!")
    
    print(f"\n{'='*100}\n")


def main():
    parser = argparse.ArgumentParser(description="Show current worker status")
    parser.add_argument("--db", default="work_queue.db", help="Path to work_queue.db")
    parser.add_argument("--watch", "-w", action="store_true", help="Auto-refresh")
    parser.add_argument("--interval", "-i", type=int, default=5, help="Refresh interval in seconds (default: 5)")
    
    args = parser.parse_args()
    
    if not Path(args.db).exists():
        print(f"ERROR: Database not found: {args.db}")
        return
    
    if args.watch:
        try:
            while True:
                clear_screen()
                show_status(args.db)
                print(f"  (Refreshing every {args.interval}s. Press Ctrl+C to stop)")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        show_status(args.db)


if __name__ == "__main__":
    main()

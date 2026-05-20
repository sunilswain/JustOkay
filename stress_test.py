#!/usr/bin/env python3
"""
Stress test to find optimal worker count.

Tests different worker configurations and measures:
- Throughput (khatiyans processed per minute)
- CPU usage
- Memory usage
- Error rate

Usage:
    python stress_test.py --db work_queue.db --data-dir bhulekh_data --duration 300

This will test worker counts: 16, 20, 24, 28, 32, 36, 40
Each test runs for --duration seconds (default 5 minutes).
Results are printed at the end with a recommendation.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

# Check for psutil
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("WARNING: psutil not installed. CPU/memory metrics will be limited.")
    print("Install with: pip install psutil")


@dataclass
class TestResult:
    workers: int
    duration_sec: float
    khatiyans_start: int
    khatiyans_end: int
    khatiyans_processed: int
    throughput_per_min: float
    avg_cpu_percent: float
    max_cpu_percent: float
    avg_memory_gb: float
    max_memory_gb: float
    errors_start: int
    errors_end: int
    new_errors: int


def get_queue_stats(db_path: str) -> dict:
    """Get current stats from work queue."""
    try:
        result = subprocess.run(
            [sys.executable, "work_queue.py", "stats", "--db", db_path],
            capture_output=True, text=True, timeout=30
        )
        # Parse the output to get counts
        lines = result.stdout.strip().split('\n')
        stats = {}
        for line in lines:
            if 'Pending:' in line:
                stats['pending'] = int(line.split(':')[1].strip().split()[0])
            elif 'In-progress:' in line:
                stats['in_progress'] = int(line.split(':')[1].strip().split()[0])
            elif 'Done:' in line:
                stats['done'] = int(line.split(':')[1].strip().split()[0])
            elif 'Error:' in line:
                stats['error'] = int(line.split(':')[1].strip().split()[0])
            elif 'Khatiyans fetched:' in line:
                stats['khatiyans_fetched'] = int(line.split(':')[1].strip().replace(',', ''))
        return stats
    except Exception as e:
        print(f"Error getting stats: {e}")
        return {}


def get_system_metrics() -> dict:
    """Get current CPU and memory usage."""
    if not HAS_PSUTIL:
        return {'cpu_percent': 0, 'memory_gb': 0}
    
    return {
        'cpu_percent': psutil.cpu_percent(interval=0.1),
        'memory_gb': psutil.virtual_memory().used / (1024 ** 3),
    }


def run_test(
    workers: int,
    db_path: str,
    data_dir: str,
    duration_sec: int,
    districts: Optional[List[str]] = None,
) -> TestResult:
    """Run a single test with the specified worker count."""
    print(f"\n{'='*60}")
    print(f"TESTING {workers} WORKERS for {duration_sec} seconds")
    print(f"{'='*60}")
    
    # Get initial stats
    stats_start = get_queue_stats(db_path)
    khatiyans_start = stats_start.get('khatiyans_fetched', 0)
    errors_start = stats_start.get('error', 0)
    
    # Build command
    cmd = [
        sys.executable, "run_village_workers.py",
        "--workers", str(workers),
        "--db", db_path,
        "--data-dir", data_dir,
        "--headless",
    ]
    if districts:
        cmd.extend(["--districts"] + districts)
    
    # Start the worker process
    print(f"Starting: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    
    # Monitor metrics while running
    cpu_samples = []
    memory_samples = []
    start_time = time.time()
    
    # Metrics collection thread
    stop_metrics = threading.Event()
    
    def collect_metrics():
        while not stop_metrics.wait(5):  # Sample every 5 seconds
            metrics = get_system_metrics()
            cpu_samples.append(metrics['cpu_percent'])
            memory_samples.append(metrics['memory_gb'])
    
    metrics_thread = threading.Thread(target=collect_metrics, daemon=True)
    metrics_thread.start()
    
    # Print progress while waiting
    try:
        elapsed = 0
        while elapsed < duration_sec:
            time.sleep(min(30, duration_sec - elapsed))
            elapsed = time.time() - start_time
            
            # Get current stats
            stats_now = get_queue_stats(db_path)
            kh_now = stats_now.get('khatiyans_fetched', 0)
            kh_processed = kh_now - khatiyans_start
            rate = kh_processed / (elapsed / 60) if elapsed > 0 else 0
            
            print(f"  [{int(elapsed)}s] Khatiyans: {kh_processed}, Rate: {rate:.1f}/min, "
                  f"CPU: {cpu_samples[-1] if cpu_samples else 0:.1f}%, "
                  f"Mem: {memory_samples[-1] if memory_samples else 0:.1f}GB")
    
    finally:
        # Stop metrics collection
        stop_metrics.set()
        
        # Terminate the worker process gracefully
        print(f"\nStopping workers (SIGTERM)...")
        if sys.platform == 'win32':
            proc.terminate()
        else:
            proc.send_signal(signal.SIGTERM)
        
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            print("Force killing workers...")
            proc.kill()
            proc.wait()
    
    # Get final stats
    time.sleep(5)  # Wait for DB to settle
    stats_end = get_queue_stats(db_path)
    khatiyans_end = stats_end.get('khatiyans_fetched', 0)
    errors_end = stats_end.get('error', 0)
    
    # Calculate results
    actual_duration = time.time() - start_time
    khatiyans_processed = khatiyans_end - khatiyans_start
    throughput = khatiyans_processed / (actual_duration / 60) if actual_duration > 0 else 0
    
    result = TestResult(
        workers=workers,
        duration_sec=actual_duration,
        khatiyans_start=khatiyans_start,
        khatiyans_end=khatiyans_end,
        khatiyans_processed=khatiyans_processed,
        throughput_per_min=throughput,
        avg_cpu_percent=sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0,
        max_cpu_percent=max(cpu_samples) if cpu_samples else 0,
        avg_memory_gb=sum(memory_samples) / len(memory_samples) if memory_samples else 0,
        max_memory_gb=max(memory_samples) if memory_samples else 0,
        errors_start=errors_start,
        errors_end=errors_end,
        new_errors=errors_end - errors_start,
    )
    
    print(f"\nResult: {khatiyans_processed} khatiyans in {actual_duration:.0f}s "
          f"= {throughput:.1f}/min")
    
    return result


def print_results(results: List[TestResult]):
    """Print formatted results table."""
    print("\n")
    print("=" * 100)
    print("STRESS TEST RESULTS")
    print("=" * 100)
    
    # Header
    print(f"{'Workers':>8} | {'Khatiyans':>10} | {'Rate/min':>10} | "
          f"{'Avg CPU%':>9} | {'Max CPU%':>9} | {'Avg Mem':>8} | {'Max Mem':>8} | {'Errors':>7}")
    print("-" * 100)
    
    # Data rows
    best_throughput = 0
    best_workers = 0
    
    for r in results:
        print(f"{r.workers:>8} | {r.khatiyans_processed:>10} | {r.throughput_per_min:>10.1f} | "
              f"{r.avg_cpu_percent:>8.1f}% | {r.max_cpu_percent:>8.1f}% | "
              f"{r.avg_memory_gb:>7.1f}GB | {r.max_memory_gb:>7.1f}GB | {r.new_errors:>7}")
        
        # Track best (highest throughput with acceptable CPU)
        if r.throughput_per_min > best_throughput and r.avg_cpu_percent < 90:
            best_throughput = r.throughput_per_min
            best_workers = r.workers
    
    print("-" * 100)
    
    # Recommendation
    print(f"\n>>> RECOMMENDATION: {best_workers} workers")
    print(f"    (Best throughput: {best_throughput:.1f} khatiyans/min with reasonable CPU usage)")
    
    # Additional analysis
    print("\nANALYSIS:")
    for i, r in enumerate(results[1:], 1):
        prev = results[i-1]
        throughput_gain = r.throughput_per_min - prev.throughput_per_min
        cpu_increase = r.avg_cpu_percent - prev.avg_cpu_percent
        efficiency = throughput_gain / (r.workers - prev.workers) if r.workers != prev.workers else 0
        
        print(f"  {prev.workers} -> {r.workers} workers: "
              f"{'+' if throughput_gain >= 0 else ''}{throughput_gain:.1f}/min throughput, "
              f"{'+' if cpu_increase >= 0 else ''}{cpu_increase:.1f}% CPU, "
              f"efficiency: {efficiency:.2f}/worker")


def main():
    parser = argparse.ArgumentParser(description="Stress test to find optimal worker count")
    parser.add_argument("--db", default="work_queue.db", help="Path to work queue database")
    parser.add_argument("--data-dir", default="bhulekh_data", help="Data output directory")
    parser.add_argument("--duration", type=int, default=300, 
                        help="Test duration per worker count in seconds (default: 300 = 5 min)")
    parser.add_argument("--workers", type=int, nargs="+", 
                        default=[16, 20, 24, 28, 32, 36, 40],
                        help="Worker counts to test (default: 16 20 24 28 32 36 40)")
    parser.add_argument("--districts", nargs="+", help="Filter to specific district codes")
    parser.add_argument("--cooldown", type=int, default=60,
                        help="Cooldown between tests in seconds (default: 60)")
    args = parser.parse_args()
    
    # Validate
    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}")
        sys.exit(1)
    
    if not HAS_PSUTIL:
        print("\nWARNING: Install psutil for accurate CPU/memory metrics:")
        print("  pip install psutil\n")
    
    # Get initial system info
    print("SYSTEM INFO:")
    if HAS_PSUTIL:
        print(f"  CPU cores: {psutil.cpu_count(logical=False)} physical, {psutil.cpu_count()} logical")
        mem = psutil.virtual_memory()
        print(f"  Memory: {mem.total / (1024**3):.1f} GB total, {mem.available / (1024**3):.1f} GB available")
    disk = shutil.disk_usage(".")
    print(f"  Disk: {disk.free / (1024**3):.1f} GB free")
    
    # Run tests
    results = []
    for i, worker_count in enumerate(args.workers):
        if i > 0:
            print(f"\nCooling down for {args.cooldown}s before next test...")
            time.sleep(args.cooldown)
        
        result = run_test(
            workers=worker_count,
            db_path=args.db,
            data_dir=args.data_dir,
            duration_sec=args.duration,
            districts=args.districts,
        )
        results.append(result)
        
        # Save intermediate results
        with open("stress_test_results.json", "w") as f:
            json.dump([asdict(r) for r in results], f, indent=2)
    
    # Print final results
    print_results(results)
    
    # Save final results
    with open("stress_test_results.json", "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print("\nResults saved to: stress_test_results.json")


if __name__ == "__main__":
    main()

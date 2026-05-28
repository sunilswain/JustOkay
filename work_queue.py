"""
Village-level work queue backed by SQLite.

Architecture:
  - Each VILLAGE is one work unit (51,727 total for all Odisha).
  - Multiple workers (processes or machines) claim villages atomically.
  - No two workers ever process the same village.
  - Every completed khatiyan is written to storage.py immediately.
  - If a worker dies mid-village, the village is re-queued and
    the scraper resumes from last_khatiyan_no inside that village.

Status lifecycle:
  pending  →  in_progress  →  done
                            →  error   (auto-retried up to max_retries)
"""

import sqlite3
import time
import socket
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Tuple

# ── Remote queue client (used when --queue-url is passed to workers) ──────────

class RemoteQueue:
    """
    Drop-in replacement for the local SQLite functions when workers run
    against a central queue_server.py over HTTP.

    Usage:
        q = RemoteQueue("http://1.2.3.4:8000", api_key="mysecret")
        village = q.claim_village(worker_id="host-pid-w0")
        q.complete_village(village["id"], 250)
    """

    def __init__(self, base_url: str, api_key: Optional[str] = None):
        import httpx
        self._base = base_url.rstrip("/")
        self._headers = {"X-Api-Key": api_key} if api_key else {}
        self._client = httpx.Client(headers=self._headers, timeout=30)

    def _post(self, path: str, data: dict) -> dict:
        r = self._client.post(f"{self._base}{path}", json=data)
        if r.status_code == 204:
            return {}
        r.raise_for_status()
        return r.json()

    def _get(self, path: str) -> dict:
        r = self._client.get(f"{self._base}{path}")
        r.raise_for_status()
        return r.json()

    def claim_village(
        self,
        worker_id: Optional[str] = None,
        district_codes: Optional[list] = None,
    ) -> Optional[dict]:
        if worker_id is None:
            worker_id = f"{socket.gethostname()}-{os.getpid()}"
        result = self._post("/claim", {"worker_id": worker_id, "district_codes": district_codes})
        return result if result else None

    def heartbeat(self, village_id: int) -> None:
        self._post("/heartbeat", {"village_id": village_id})

    def checkpoint_village(self, village_id: int, khatiyans_fetched: int, last_khatiyan_no: str) -> None:
        self._post("/checkpoint", {
            "village_id": village_id,
            "khatiyans_fetched": khatiyans_fetched,
            "last_khatiyan_no": last_khatiyan_no,
        })

    def complete_village(self, village_id: int, khatiyans_fetched: int) -> None:
        self._post("/complete", {"village_id": village_id, "khatiyans_fetched": khatiyans_fetched})

    def fail_village(self, village_id: int, error_msg: str) -> None:
        self._post("/fail", {"village_id": village_id, "error_msg": error_msg})

    def get_stats(self) -> dict:
        return self._get("/stats")

    def set_priority(self, district_codes: list, priority: int) -> int:
        r = self._post("/priority", {"district_codes": district_codes, "priority": priority})
        return r.get("villages_updated", 0)

    def health(self) -> dict:
        return self._get("/health")


def make_queue(db_or_url: str, api_key: Optional[str] = None):
    """
    Factory: returns either a RemoteQueue (if db_or_url starts with http)
    or a string path (local SQLite, used by the existing functions below).
    """
    if db_or_url.startswith("http://") or db_or_url.startswith("https://"):
        return RemoteQueue(db_or_url, api_key=api_key)
    return db_or_url  # local path — use existing functions directly

DEFAULT_QUEUE_PATH = "work_queue.db"
# If a worker dies mid-village, the village is reclaimed after this many seconds.
# Must be longer than _VILLAGE_TIMEOUT (600s) + some buffer.
CLAIM_TIMEOUT_SECONDS = 1800  # 30 min (was 1 hour — faster recovery when a worker crashes)


@contextmanager
def _conn(db_path: str):
    con = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=10000")
    try:
        yield con
    finally:
        con.close()


def create_queue(db_path: str = DEFAULT_QUEUE_PATH) -> None:
    """Create the work queue database schema (idempotent)."""
    with _conn(db_path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS villages (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            district_code       INTEGER NOT NULL,
            district_name       TEXT    NOT NULL DEFAULT '',
            tahasil_code        INTEGER NOT NULL,
            tahasil_name        TEXT    NOT NULL DEFAULT '',
            village_code        INTEGER NOT NULL,
            village_name        TEXT    NOT NULL DEFAULT '',
            khatiyan_count      INTEGER DEFAULT 0,
            priority            INTEGER DEFAULT 0,

            -- Phase-2 execution tracking
            status              TEXT    NOT NULL DEFAULT 'pending',
            worker_id           TEXT,
            claimed_at          TEXT,
            started_at          TEXT,
            completed_at        TEXT,
            khatiyans_fetched   INTEGER DEFAULT 0,
            last_khatiyan_no    TEXT,

            retries             INTEGER DEFAULT 0,
            max_retries         INTEGER DEFAULT 3,
            error_msg           TEXT,

            UNIQUE(district_code, tahasil_code, village_code)
        );

        CREATE INDEX IF NOT EXISTS idx_villages_status
            ON villages(status, priority DESC, id ASC);

        CREATE INDEX IF NOT EXISTS idx_villages_district
            ON villages(district_code, tahasil_code, status);

        -- Summary view for progress reporting
        CREATE VIEW IF NOT EXISTS queue_summary AS
        SELECT
            status,
            COUNT(*)                    AS villages,
            SUM(khatiyan_count)         AS est_khatiyans,
            SUM(khatiyans_fetched)      AS khatiyans_fetched
        FROM villages
        GROUP BY status;
        """)


def upsert_village(
    db_path: str,
    district_code: int,
    district_name: str,
    tahasil_code: int,
    tahasil_name: str,
    village_code: int,
    village_name: str,
    khatiyan_count: int = 0,
    priority: int = 0,
) -> None:
    """Insert a village into the queue; ignore if already present."""
    with _conn(db_path) as con:
        con.execute("""
            INSERT INTO villages
                (district_code, district_name, tahasil_code, tahasil_name,
                 village_code, village_name, khatiyan_count, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(district_code, tahasil_code, village_code)
            DO UPDATE SET
                district_name  = excluded.district_name,
                tahasil_name   = excluded.tahasil_name,
                village_name   = excluded.village_name,
                khatiyan_count = MAX(villages.khatiyan_count, excluded.khatiyan_count),
                priority       = MAX(villages.priority, excluded.priority)
        """, (district_code, district_name, tahasil_code, tahasil_name,
              village_code, village_name, khatiyan_count, priority))


def claim_village(
    db_path: str,
    worker_id: Optional[str] = None,
    district_codes: Optional[list] = None,
) -> Optional[dict]:
    """
    Atomically claim the next pending village for this worker.
    Also reclaims villages stuck in_progress beyond CLAIM_TIMEOUT_SECONDS.
    Returns dict with village info, or None if no work available.
    """
    if worker_id is None:
        worker_id = f"{socket.gethostname()}-{os.getpid()}"

    now_iso = _now()
    timeout_cutoff = _now_minus(CLAIM_TIMEOUT_SECONDS)

    with _conn(db_path) as con:
        # First: reclaim timed-out in_progress villages (worker died)
        con.execute("""
            UPDATE villages
            SET    status    = 'pending',
                   worker_id = NULL,
                   claimed_at = NULL
            WHERE  status     = 'in_progress'
            AND    claimed_at < ?
        """, (timeout_cutoff,))

        # Claim next pending village (highest priority first, then oldest id)
        filter_sql = ""
        params: list = []
        if district_codes:
            placeholders = ",".join("?" * len(district_codes))
            filter_sql = f"AND district_code IN ({placeholders})"
            params = list(district_codes)

        row = con.execute(f"""
            SELECT id, district_code, district_name,
                   tahasil_code, tahasil_name,
                   village_code, village_name,
                   khatiyan_count, khatiyans_fetched, last_khatiyan_no
            FROM   villages
            WHERE  status = 'pending'
            {filter_sql}
            ORDER BY priority DESC, id ASC
            LIMIT  1
        """, params).fetchone()

        if row is None:
            return None

        village_id = row[0]
        con.execute("""
            UPDATE villages
            SET    status     = 'in_progress',
                   worker_id  = ?,
                   claimed_at = ?,
                   started_at = COALESCE(started_at, ?)
            WHERE  id = ?
        """, (worker_id, now_iso, now_iso, village_id))

        cols = ("id", "district_code", "district_name",
                "tahasil_code", "tahasil_name",
                "village_code", "village_name",
                "khatiyan_count", "khatiyans_fetched", "last_khatiyan_no")
        return dict(zip(cols, row))


def heartbeat(db_path: str, village_id: int) -> None:
    """Refresh claimed_at so the village isn't reclaimed by another worker."""
    with _conn(db_path) as con:
        con.execute(
            "UPDATE villages SET claimed_at = ? WHERE id = ?",
            (_now(), village_id),
        )


def checkpoint_village(
    db_path: str,
    village_id: int,
    khatiyans_fetched: int,
    last_khatiyan_no: str,
) -> None:
    """Save progress inside a village (called after each khatiyan batch)."""
    with _conn(db_path) as con:
        con.execute("""
            UPDATE villages
            SET    khatiyans_fetched = ?,
                   last_khatiyan_no  = ?,
                   claimed_at        = ?
            WHERE  id = ?
        """, (khatiyans_fetched, last_khatiyan_no, _now(), village_id))


def complete_village(db_path: str, village_id: int, khatiyans_fetched: int) -> None:
    """Mark a village as fully done."""
    with _conn(db_path) as con:
        con.execute("""
            UPDATE villages
            SET    status           = 'done',
                   khatiyans_fetched = ?,
                   completed_at     = ?
            WHERE  id = ?
        """, (khatiyans_fetched, _now(), village_id))


def fail_village(db_path: str, village_id: int, error_msg: str) -> None:
    """
    Mark a village as failed.

    Key rule: if the worker made *any* progress (khatiyans_fetched > 0 and a
    checkpoint exists), reset retries to 0 and put it back to pending.  This
    prevents large villages from being permanently errored just because they
    timed out mid-way — they will keep resuming from last_khatiyan_no until
    fully done.  Only villages that fail from the very start (no progress)
    consume retry budget.
    """
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT retries, max_retries, khatiyans_fetched, last_khatiyan_no FROM villages WHERE id = ?",
            (village_id,),
        ).fetchone()
        if row is None:
            return
        retries, max_retries, kh_fetched, last_kh = row

        # Timeout errors = site was slow, not a village bug. Always retry, never permanently error.
        is_timeout = "timed out" in error_msg.lower() or "timeout" in error_msg.lower()

        # Progress checkpoint means the village was partially done — resume, don't count as failure.
        made_progress = (kh_fetched or 0) > 0 and last_kh

        if is_timeout or made_progress:
            con.execute("""
                UPDATE villages
                SET    status     = 'pending',
                       retries    = 0,
                       error_msg  = ?,
                       worker_id  = NULL,
                       claimed_at = NULL
                WHERE  id = ?
            """, (error_msg[:500], village_id))
        else:
            new_retries = retries + 1
            if new_retries < max_retries:
                con.execute("""
                    UPDATE villages
                    SET    status     = 'pending',
                           retries    = ?,
                           error_msg  = ?,
                           worker_id  = NULL,
                           claimed_at = NULL
                    WHERE  id = ?
                """, (new_retries, error_msg[:500], village_id))
            else:
                con.execute("""
                    UPDATE villages
                    SET    status    = 'error',
                           retries   = ?,
                           error_msg = ?
                    WHERE  id = ?
                """, (new_retries, error_msg[:500], village_id))


def get_stats(db_path: str) -> dict:
    """Return progress summary."""
    with _conn(db_path) as con:
        rows = con.execute("""
            SELECT status, COUNT(*), SUM(khatiyan_count), SUM(khatiyans_fetched)
            FROM   villages
            GROUP BY status
        """).fetchall()
        totals = con.execute("""
            SELECT COUNT(*), SUM(khatiyan_count), SUM(khatiyans_fetched)
            FROM   villages
        """).fetchone()

    by_status = {}
    for status, villages, est_kh, done_kh in rows:
        by_status[status] = {
            "villages": villages,
            "est_khatiyans": est_kh or 0,
            "khatiyans_fetched": done_kh or 0,
        }

    total_v, total_kh_est, total_kh_done = totals or (0, 0, 0)
    return {
        "by_status": by_status,
        "total_villages": total_v,
        "total_khatiyans_est": total_kh_est or 0,
        "total_khatiyans_fetched": total_kh_done or 0,
    }


def set_priority(db_path: str, district_codes: list, priority: int) -> int:
    """Boost priority for specific districts (returns rows updated)."""
    if not district_codes:
        return 0
    placeholders = ",".join("?" * len(district_codes))
    with _conn(db_path) as con:
        cur = con.execute(
            f"UPDATE villages SET priority = ? WHERE district_code IN ({placeholders})",
            [priority] + list(district_codes),
        )
        return cur.rowcount


def set_priority_tahasils(
    db_path: str,
    tahasil_names: list,
    priority: int,
    district_codes: Optional[list] = None,
) -> int:
    """
    Boost priority for specific tahasils by name.
    Optionally restrict to certain district codes.
    Returns the number of villages updated.
    """
    if not tahasil_names:
        return 0
    tahasil_placeholders = ",".join("?" * len(tahasil_names))
    params: list = [priority] + list(tahasil_names)

    district_filter = ""
    if district_codes:
        district_placeholders = ",".join("?" * len(district_codes))
        district_filter = f"AND district_code IN ({district_placeholders})"
        params += list(district_codes)

    with _conn(db_path) as con:
        cur = con.execute(
            f"""UPDATE villages SET priority = ?
                WHERE tahasil_name IN ({tahasil_placeholders})
                {district_filter}""",
            params,
        )
        return cur.rowcount


def reclaim_stuck_villages(db_path: str) -> int:
    """
    Immediately release ALL in_progress villages back to pending.

    Call once at startup before workers begin.  Without this, villages from
    a previous crashed run are locked for CLAIM_TIMEOUT_SECONDS (30 min)
    before workers can pick them up again.
    """
    with _conn(db_path) as con:
        cur = con.execute("""
            UPDATE villages
            SET    status     = 'pending',
                   worker_id  = NULL,
                   claimed_at = NULL
            WHERE  status = 'in_progress'
        """)
        return cur.rowcount


def reset_errors(db_path: str) -> int:
    """Reset all error villages back to pending for a fresh retry."""
    return reset_errors_for_districts(db_path, district_codes=None)


def reset_errors_for_districts(
    db_path: str,
    district_codes: Optional[list] = None,
    *,
    reset_errors: bool = True,
    reset_zero_progress_in_progress: bool = False,
) -> Tuple[int, int]:
    """
    Reset error villages to pending (optionally filtered by district).

    If reset_zero_progress_in_progress is True, also release in_progress villages
    that still have khatiyans_fetched=0 (stuck at 0/N) back to pending.

    Returns (errors_reset, in_progress_reset).
    """
    filter_sql = ""
    params: list = []
    if district_codes:
        placeholders = ",".join("?" * len(district_codes))
        filter_sql = f" AND district_code IN ({placeholders})"
        params = list(district_codes)

    errors_reset = 0
    in_progress_reset = 0

    with _conn(db_path) as con:
        if reset_errors:
            cur = con.execute(
                f"""
                UPDATE villages
                SET    status = 'pending',
                       retries = 0,
                       error_msg = NULL,
                       worker_id = NULL,
                       claimed_at = NULL
                WHERE  status = 'error'
                {filter_sql}
                """,
                params,
            )
            errors_reset = cur.rowcount

        if reset_zero_progress_in_progress:
            cur2 = con.execute(
                f"""
                UPDATE villages
                SET    status = 'pending',
                       worker_id = NULL,
                       claimed_at = NULL
                WHERE  status = 'in_progress'
                AND    COALESCE(khatiyans_fetched, 0) = 0
                {filter_sql}
                """,
                params,
            )
            in_progress_reset = cur2.rowcount

    return errors_reset, in_progress_reset


def list_districts(db_path: str) -> list:
    """Return all distinct districts in the queue."""
    with _conn(db_path) as con:
        return con.execute("""
            SELECT district_code, district_name,
                   COUNT(*) AS villages,
                   SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                   MAX(priority) AS priority
            FROM   villages
            GROUP BY district_code
            ORDER BY priority DESC, district_code
        """).fetchall()


def list_tahasils(db_path: str, district_codes: Optional[list] = None) -> list:
    """Return all distinct tahasils with full status breakdown, optionally filtered by district."""
    filter_sql = ""
    params: list = []
    if district_codes:
        placeholders = ",".join("?" * len(district_codes))
        filter_sql = f"WHERE district_code IN ({placeholders})"
        params = list(district_codes)
    with _conn(db_path) as con:
        return con.execute(f"""
            SELECT district_code, district_name, tahasil_code, tahasil_name,
                   COUNT(*) AS villages,
                   SUM(CASE WHEN status='done'        THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
                   SUM(CASE WHEN status='pending'     THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN status='error'       THEN 1 ELSE 0 END) AS errors,
                   SUM(khatiyans_fetched)                                 AS fetched,
                   SUM(khatiyan_count)                                    AS est_khatiyans,
                   MAX(priority)                                          AS priority
            FROM   villages
            {filter_sql}
            GROUP BY district_code, tahasil_code
            ORDER BY district_name, priority DESC, tahasil_name
        """, params).fetchall()


def set_priority_tahasil_codes(
    db_path: str,
    tahasil_codes: list,
    priority: int,
    district_codes: Optional[list] = None,
) -> int:
    """
    Boost priority for specific tahasils by their numeric code.
    Easier to type than Odia names. Use `tahasils` subcommand to find codes.
    Optionally restrict to certain district codes.
    Returns the number of villages updated.
    """
    if not tahasil_codes:
        return 0
    t_placeholders = ",".join("?" * len(tahasil_codes))
    params: list = [priority] + list(tahasil_codes)

    district_filter = ""
    if district_codes:
        d_placeholders = ",".join("?" * len(district_codes))
        district_filter = f"AND district_code IN ({d_placeholders})"
        params += list(district_codes)

    with _conn(db_path) as con:
        cur = con.execute(
            f"""UPDATE villages SET priority = ?
                WHERE tahasil_code IN ({t_placeholders})
                {district_filter}""",
            params,
        )
        return cur.rowcount


# ── internal helpers ────────────────────────────────────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _now_minus(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds))


# ── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys

    # Shared --db parent so every subcommand accepts it after the subcommand name.
    # e.g.  work_queue.py tahasils --districts 3 --db /path/to/work_queue.db
    _db_parent = argparse.ArgumentParser(add_help=False)
    _db_parent.add_argument(
        "--db", default=DEFAULT_QUEUE_PATH, metavar="PATH",
        help=f"Queue DB path (default: {DEFAULT_QUEUE_PATH})",
    )

    parser = argparse.ArgumentParser(
        description="Work queue inspection/management tool",
        parents=[_db_parent],
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("stats",     parents=[_db_parent], add_help=False,
                   help="Show overall progress statistics")
    sub.add_parser("districts", parents=[_db_parent], add_help=False,
                   help="List all districts with completion %")
    sub.add_parser("create",    parents=[_db_parent], add_help=False,
                   help="Create (or verify) the queue database schema")
    sub.add_parser("reset-errors", parents=[_db_parent], add_help=False,
                   help="Reset all errored villages back to pending")
    sub.add_parser("reclaim",      parents=[_db_parent], add_help=False,
                   help="Release all stuck in_progress villages back to pending (run after a crash)")

    ta = sub.add_parser("tahasils", parents=[_db_parent], add_help=False,
                        help="Show tahasil-level completion breakdown")
    ta.add_argument("--districts", nargs="+", type=int, default=[], metavar="CODE",
                    help="Filter by district code(s), e.g. --districts 3")

    pr = sub.add_parser("priority", parents=[_db_parent], add_help=False,
                        help="Boost priority for districts / tahasils")
    pr.add_argument("--districts", nargs="+", type=int, default=[],
                    help="District codes to boost (e.g. --districts 3 14)")
    pr.add_argument("--tahasils", nargs="+", default=[],
                    help="Tahasil names to boost (Odia text, e.g. --tahasils ବଡ଼ମ୍ବା)")
    pr.add_argument("--tahasil-codes", nargs="+", type=int, default=[],
                    help="Tahasil codes to boost — easier than Odia names "
                         "(see 'Code' column in: work_queue.py tahasils)")
    pr.add_argument("--level", type=int, default=10,
                    help="Priority value (higher = processed first, default: 10)")

    args = parser.parse_args()

    # Resolve --db: subparser arg wins if present, else fall back to parent
    db = getattr(args, "db", DEFAULT_QUEUE_PATH)

    if args.cmd == "create":
        create_queue(db)
        print(f"Queue created: {db}")

    elif args.cmd == "stats":
        s = get_stats(db)
        print(f"Total villages : {s['total_villages']:,}")
        print(f"Est. khatiyans : {s['total_khatiyans_est']:,}")
        print(f"Fetched so far : {s['total_khatiyans_fetched']:,}")
        print()
        for status, info in sorted(s["by_status"].items()):
            print(f"  {status:15s}: {info['villages']:6,} villages  "
                  f"~{info['est_khatiyans']:>10,} khatiyans")

    elif args.cmd == "districts":
        rows = list_districts(db)
        print(f"{'Code':>6}  {'Pri':>4}  {'District':<30}  {'Total':>6}  {'Done':>6}  {'%':>5}")
        print("-" * 72)
        for code, name, vils, done, priority in rows:
            pct = f"{100*done//vils}%" if vils else "-"
            pri = str(priority) if priority > 0 else "-"
            print(f"{code:>6}  {pri:>4}  {name:<30}  {vils:>6,}  {done:>6,}  {pct:>5}")

    elif args.cmd == "tahasils":
        d_filter = args.districts if args.districts else None
        rows = list_tahasils(db, district_codes=d_filter)
        if not rows:
            print("No tahasils found (check --districts filter or --db path).")
            sys.exit(0)

        hdr = (f"{'D':>3}  {'T':>4}  {'District':<18}  {'Tahasil':<22}  "
               f"{'Vil':>5}  {'Done':>5}  {'Active':>6}  {'Pend':>5}  {'Err':>4}  "
               f"{'Fetched':>8}  {'Est.Kh':>8}  {'Vil%':>5}  {'Kh%':>5}  {'Pri':>4}")
        print(hdr)
        print("-" * len(hdr))
        prev_dist = None
        for d_code, dist, t_code, t_name, vils, done, active, pend, errors, fetched, est, priority in rows:
            if prev_dist and dist != prev_dist:
                print()
            prev_dist = dist
            vil_pct = f"{100*done//vils}%"  if vils               else "-"
            kh_pct  = f"{100*fetched//est}%" if (est and fetched)  else "-"
            pri = str(priority) if priority > 0 else "-"
            print(
                f"{d_code:>3}  {t_code:>4}  {dist:<18}  {t_name:<22}  "
                f"{vils:>5,}  {done:>5,}  {active:>6,}  {pend:>5,}  {errors:>4,}  "
                f"{fetched or 0:>8,}  {est or 0:>8,}  {vil_pct:>5}  {kh_pct:>5}  {pri:>4}"
            )

    elif args.cmd == "priority":
        total = 0
        tahasil_codes = getattr(args, "tahasil_codes", [])

        if args.tahasils or tahasil_codes:
            # --districts is a FILTER only, not a separate boost operation.
            d_filter = args.districts if args.districts else None
            scope = f" (within districts {args.districts})" if d_filter else ""

            if args.tahasils:
                n = set_priority_tahasils(db, args.tahasils, args.level, district_codes=d_filter)
                print(f"Set priority={args.level} for {n} villages in tahasils {args.tahasils}{scope}")
                total += n

            if tahasil_codes:
                n = set_priority_tahasil_codes(db, tahasil_codes, args.level, district_codes=d_filter)
                print(f"Set priority={args.level} for {n} villages in tahasil codes {tahasil_codes}{scope}")
                total += n

        elif args.districts:
            # No tahasil filter: boost the entire district(s)
            n = set_priority(db, args.districts, args.level)
            print(f"Set priority={args.level} for {n} villages in districts {args.districts}")
            total += n
        else:
            print("ERROR: provide --districts, --tahasils, and/or --tahasil-codes")
            sys.exit(1)
        print(f"Total villages updated: {total}")

    elif args.cmd == "reset-errors":
        n = reset_errors(db)
        print(f"Reset {n} errored villages back to pending")

    elif args.cmd == "reclaim":
        n = reclaim_stuck_villages(db)
        print(f"Released {n} stuck in_progress villages back to pending")

    else:
        parser.print_help()

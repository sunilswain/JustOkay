"""
Persistent, resumable storage for Bhulekh RoR data.

- Data is written to FILES immediately after each khatiyan — if the script fails
  mid-district, all data fetched so far for that district is already on disk.
- Two backends:
  - sqlite: one .db file per district (default). Queryable, checkpoint inside DB.
  - ndjson: one .ndjson file per district (plain JSON Lines) + small checkpoint .json.
    Often faster for raw append (no SQL overhead); human-readable and easy to stream.
"""

import json
import logging
import os
import random
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

try:
    import fcntl  # Unix-only; fleet instances run Linux
except ImportError:  # pragma: no cover - Windows dev
    fcntl = None  # type: ignore

F = TypeVar("F", bound=Callable[..., Any])

SQLITE_CONNECT_TIMEOUT_S = 30
SQLITE_BUSY_TIMEOUT_MS = 30000
_LOCK_RETRY_ATTEMPTS = 8
_LOCK_RETRY_BASE_S = 0.05
_LOCK_RETRY_MAX_S = 2.0


def _is_locked_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _retry_on_locked(func: F) -> F:
    """Retry SQLite writes on 'database is locked' with exponential backoff + jitter."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_exc: Optional[BaseException] = None
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc):
                    raise
                last_exc = exc
                if attempt == _LOCK_RETRY_ATTEMPTS - 1:
                    break
                delay = min(_LOCK_RETRY_BASE_S * (2 ** attempt), _LOCK_RETRY_MAX_S)
                delay += random.uniform(0, delay * 0.25)
                district = getattr(args[0], "district_name", "?") if args else "?"
                logger.warning(
                    "SQLite locked on %s.%s (district=%s), retry %d/%d in %.2fs",
                    type(args[0]).__name__ if args else "?",
                    func.__name__,
                    district,
                    attempt + 1,
                    _LOCK_RETRY_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    return wrapper  # type: ignore[return-value]


class _DistrictFileLock:
    """Serialize writers per district DB across worker processes (Linux flock)."""

    def __init__(self, db_path: Path):
        self._lock_path = Path(str(db_path) + ".write.lock")
        self._fd: Optional[int] = None

    def __enter__(self) -> "_DistrictFileLock":
        if fcntl is None:
            return self
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args: Any) -> None:
        if fcntl is None or self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

# Default directory for all district data and checkpoints
DEFAULT_DATA_DIR = "bhulekh_data"

# Storage backend: "sqlite" or "ndjson"
DEFAULT_STORAGE_BACKEND = "sqlite"


def _sanitize_district_for_filename(name: str) -> str:
    """Make district name safe for use in file/dir names."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip() or "unknown"


def _resolve_district_db_path(
    data_dir: Path,
    district_name: str,
    district_code: Optional[int] = None,
) -> Path:
    """Pick the canonical on-disk DB for a district (handles legacy filenames)."""
    safe = _sanitize_district_for_filename(district_name)
    primary = data_dir / f"district_{safe}.db"
    candidates = [primary]

    if district_code is not None:
        candidates.append(data_dir / f"district_District-{district_code}.db")
        if district_code == 10:
            candidates.append(data_dir / "district_kandhamal.db")

    import re

    for db in data_dir.glob("district_*.db"):
        if db in candidates or db.stat().st_size <= 4096:
            continue
        if district_code is not None:
            m = re.search(r"District-(\d+)", db.name)
            if m and int(m.group(1)) == district_code:
                candidates.append(db)
                continue
        try:
            import sqlite3

            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT DISTINCT district FROM khatiyans LIMIT 1"
            ).fetchone()
            conn.close()
            if row and row[0] == district_name:
                candidates.append(db)
        except Exception:
            pass

    existing = [
        p for p in candidates if p.is_file() and p.stat().st_size > 4096
    ]
    if existing:
        return max(existing, key=lambda p: p.stat().st_size)
    return primary


def create_storage(
    data_dir: str = DEFAULT_DATA_DIR,
    district_name: str = "",
    backend: str = DEFAULT_STORAGE_BACKEND,
    district_code: Optional[int] = None,
    **kwargs: Any,
) -> "BhulekhStorageBase":
    """Create a storage instance (SQLite or NDJSON) for the given district."""
    if backend.lower() == "ndjson":
        return BhulekhStorageNDJSON(data_dir=data_dir, district_name=district_name, **kwargs)
    return BhulekhStorage(
        data_dir=data_dir, district_name=district_name, district_code=district_code, **kwargs
    )


class BhulekhStorageBase:
    """Base interface for per-district storage: append khatiyan, checkpoint, resume."""

    def append_khatiyan(self, ror_data: Dict[str, Any]) -> None:
        raise NotImplementedError

    def set_checkpoint(
        self,
        district_value: str,
        district_text: str,
        tahasil_value: str,
        tahasil_text: str,
        village_value: str,
        village_text: str,
        last_khatiyan_value: str,
        last_khatiyan_text: str,
        khatiyan_count: int,
    ) -> None:
        raise NotImplementedError

    def get_checkpoint(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def get_khatiyan_count(self) -> int:
        raise NotImplementedError

    def increment_layout_stat(self, ror_type: str) -> None:
        """Increment Type-1 / Type-2 layout counter (optional per backend)."""
        pass

    def get_layout_stats(self) -> Dict[str, int]:
        return {'type1': 0, 'type2': 0}

    def close(self) -> None:
        pass


class BhulekhStorageNDJSON(BhulekhStorageBase):
    """
    Append-only JSON Lines file per district + small checkpoint JSON file.
    No SQL overhead — just append one line per khatiyan and flush. Data is in
    plain .ndjson files (one JSON object per line); if the script fails mid-district,
    every khatiyan written so far is already in the file.
    """

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR, district_name: str = ""):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.district_name = district_name or "default"
        self.safe_name = _sanitize_district_for_filename(self.district_name)
        self._ndjson_path = self.data_dir / f"district_{self.safe_name}.ndjson"
        self._checkpoint_path = self.data_dir / f"district_{self.safe_name}_checkpoint.json"
        self._layout_stats_path = self.data_dir / f"district_{self.safe_name}_layout_stats.json"
        self._file = None
        self._count = 0
        self._lock = threading.Lock()

    def _open(self):
        if self._file is None or self._file.closed:
            self._file = open(self._ndjson_path, "a", encoding="utf-8")
            if self._count == 0 and self._ndjson_path.exists():
                self._file.seek(0)
                self._count = sum(1 for _ in self._file)
                self._file.seek(0, 2)  # back to end for appends
        return self._file

    def append_khatiyan(self, ror_data: Dict[str, Any]) -> None:
        with self._lock:
            f = self._open()
            line = json.dumps(ror_data, ensure_ascii=False) + "\n"
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
            self._count += 1

    def set_checkpoint(
        self,
        district_value: str,
        district_text: str,
        tahasil_value: str,
        tahasil_text: str,
        village_value: str,
        village_text: str,
        last_khatiyan_value: str,
        last_khatiyan_text: str,
        khatiyan_count: int,
    ) -> None:
        data = {
            "district_value": district_value,
            "district_text": district_text,
            "tahasil_value": tahasil_value,
            "tahasil_text": tahasil_text,
            "village_value": village_value,
            "village_text": village_text,
            "last_khatiyan_value": last_khatiyan_value,
            "last_khatiyan_text": last_khatiyan_text,
            "khatiyan_count": khatiyan_count,
        }
        with self._lock:
            tmp = self._checkpoint_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=0)
            tmp.replace(self._checkpoint_path)

    def get_checkpoint(self) -> Optional[Dict[str, Any]]:
        if not self._checkpoint_path.exists():
            return None
        try:
            with open(self._checkpoint_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def get_khatiyan_count(self) -> int:
        if self._file is not None and not self._file.closed:
            return self._count
        if not self._ndjson_path.exists():
            return 0
        with open(self._ndjson_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)

    def increment_layout_stat(self, ror_type: str) -> None:
        key = ror_type if ror_type in ('type1', 'type2') else 'type1'
        with self._lock:
            stats = self.get_layout_stats()
            stats[key] = stats.get(key, 0) + 1
            tmp = self._layout_stats_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(stats, f, ensure_ascii=False, indent=0)
            tmp.replace(self._layout_stats_path)

    def get_layout_stats(self) -> Dict[str, int]:
        if not self._layout_stats_path.exists():
            return {'type1': 0, 'type2': 0}
        try:
            with open(self._layout_stats_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                'type1': int(data.get('type1', 0)),
                'type2': int(data.get('type2', 0)),
            }
        except Exception:
            return {'type1': 0, 'type2': 0}

    def close(self) -> None:
        with self._lock:
            if self._file is not None and not self._file.closed:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None


class BhulekhStorage(BhulekhStorageBase):
    """
    Per-district SQLite storage with checkpoint for resume.

    Thread-safe for single-district use (one writer per district).
    Each district gets its own DB file, so multiple processes can write
    to different districts without locking conflicts.
    """

    def __init__(
        self,
        data_dir: str = DEFAULT_DATA_DIR,
        district_name: str = "",
        sync_mode: str = "NORMAL",
        district_code: Optional[int] = None,
    ):
        """
        Args:
            data_dir: Root directory for DB files (e.g. bhulekh_data).
            district_name: Display name of district (used for DB filename).
            sync_mode: SQLite sync: NORMAL (fast, good durability), FULL (safest, slower).
            district_code: Numeric district code — used to resolve legacy DB filenames.
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.district_name = district_name or "default"
        self.safe_name = _sanitize_district_for_filename(self.district_name)
        self.sync_mode = sync_mode
        self._db_path = _resolve_district_db_path(
            self.data_dir, self.district_name, district_code
        )
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self._db_path),
                timeout=SQLITE_CONNECT_TIMEOUT_S,
                check_same_thread=True,
            )
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute(f"PRAGMA synchronous={self.sync_mode}")
            self._local.conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            self._local.conn.execute("PRAGMA foreign_keys=OFF")
            self._create_tables(self._local.conn)
        return self._local.conn

    def _write_lock(self) -> _DistrictFileLock:
        return _DistrictFileLock(self._db_path)

    def _create_tables(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS khatiyans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                district TEXT NOT NULL,
                tahasil TEXT NOT NULL,
                village TEXT NOT NULL,
                khatiyan_value TEXT NOT NULL,
                khatiyan_text TEXT NOT NULL,
                data_json TEXT NOT NULL,
                html_content TEXT,
                needs_review INTEGER DEFAULT 0,
                fetched_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS ix_khatiyans_district ON khatiyans(district);
            CREATE INDEX IF NOT EXISTS ix_khatiyans_tahasil_village ON khatiyans(tahasil, village);
            CREATE INDEX IF NOT EXISTS ix_khatiyans_needs_review ON khatiyans(needs_review) WHERE needs_review = 1;

            CREATE TABLE IF NOT EXISTS checkpoint (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                district_value TEXT,
                district_text TEXT,
                tahasil_value TEXT,
                tahasil_text TEXT,
                village_value TEXT,
                village_text TEXT,
                last_khatiyan_value TEXT,
                last_khatiyan_text TEXT,
                khatiyan_count INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS layout_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                type1_count INTEGER DEFAULT 0,
                type2_count INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        # Add columns to existing tables if they don't exist (migration)
        for col_def in [
            "ALTER TABLE khatiyans ADD COLUMN html_content TEXT",
            "ALTER TABLE khatiyans ADD COLUMN needs_review INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(col_def)
            except sqlite3.OperationalError as e:
                err_msg = str(e).lower()
                if "duplicate column" in err_msg or "already exists" in err_msg:
                    pass
                else:
                    logger.error("Migration failed (%s): %s — data writes will fail!", col_def, e)
                    raise
        # Ensure dedup index exists on older DBs (skip if legacy duplicates remain)
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_khatiyans_dedup "
                "ON khatiyans(district, tahasil, village, khatiyan_value)"
            )
        except (sqlite3.OperationalError, sqlite3.IntegrityError):
            pass
        conn.commit()

    @_retry_on_locked
    def update_khatiyan(
        self,
        khatiyan_id: int,
        ror_data: Dict[str, Any],
        *,
        needs_review: Optional[int] = None,
        html_content: Optional[str] = None,
    ) -> bool:
        """Update an existing khatiyan record in place."""
        with self._write_lock():
            conn = self._conn()
            if needs_review is None:
                needs_review = 1 if html_content else 0
            try:
                conn.execute(
                    """UPDATE khatiyans
                       SET data_json = ?, html_content = COALESCE(?, html_content), needs_review = ?
                       WHERE id = ?""",
                    (
                        json.dumps(ror_data, ensure_ascii=False),
                        html_content,
                        needs_review,
                        khatiyan_id,
                    ),
                )
                conn.commit()
                return conn.total_changes > 0
            except sqlite3.OperationalError:
                raise
            except Exception as exc:
                logger.error("Failed to update khatiyan %s: %s", khatiyan_id, exc)
                return False

    @_retry_on_locked
    def append_khatiyan(self, ror_data: Dict[str, Any], html_content: Optional[str] = None) -> None:
        """
        Upsert one khatiyan record. Uses INSERT OR REPLACE on the unique index
        (district, tahasil, village, khatiyan_value) so re-scrapes update in place
        rather than creating duplicates. Commits immediately for durability.
        """
        with self._write_lock():
            conn = self._conn()
            district = ror_data.get("district", "")
            tahasil = ror_data.get("tahasil", "")
            village = ror_data.get("village", "")
            khatiyan_value = ror_data.get("khatiyan_value", "")
            khatiyan_text = ror_data.get("khatiyan_text", "")
            data_json = json.dumps(ror_data, ensure_ascii=False)

            needs_review = 1 if html_content else 0

            conn.execute(
                """INSERT OR REPLACE INTO khatiyans
                   (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, html_content, needs_review, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, html_content, needs_review),
            )
            conn.commit()

    @_retry_on_locked
    def append_khatiyans_batch(self, rows: list) -> int:
        """
        Upsert a list of (ror_data, html_content) tuples in a single transaction.
        Dramatically reduces lock contention vs calling append_khatiyan per row.
        Returns number of rows written.
        """
        if not rows:
            return 0
        with self._write_lock():
            conn = self._conn()
            params = []
            for ror_data, html_content in rows:
                params.append((
                    ror_data.get("district", ""),
                    ror_data.get("tahasil", ""),
                    ror_data.get("village", ""),
                    ror_data.get("khatiyan_value", ""),
                    ror_data.get("khatiyan_text", ""),
                    json.dumps(ror_data, ensure_ascii=False),
                    html_content,
                    1 if html_content else 0,
                ))
            conn.executemany(
                """INSERT OR REPLACE INTO khatiyans
                   (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, html_content, needs_review, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                params,
            )
            conn.commit()
            return len(params)

    @_retry_on_locked
    def set_checkpoint(
        self,
        district_value: str,
        district_text: str,
        tahasil_value: str,
        tahasil_text: str,
        village_value: str,
        village_text: str,
        last_khatiyan_value: str,
        last_khatiyan_text: str,
        khatiyan_count: int,
    ) -> None:
        """Update resume checkpoint for this district."""
        with self._write_lock():
            conn = self._conn()
            conn.execute(
                """INSERT INTO checkpoint (id, district_value, district_text, tahasil_value, tahasil_text,
                   village_value, village_text, last_khatiyan_value, last_khatiyan_text, khatiyan_count, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                     district_value=excluded.district_value,
                     district_text=excluded.district_text,
                     tahasil_value=excluded.tahasil_value,
                     tahasil_text=excluded.tahasil_text,
                     village_value=excluded.village_value,
                     village_text=excluded.village_text,
                     last_khatiyan_value=excluded.last_khatiyan_value,
                     last_khatiyan_text=excluded.last_khatiyan_text,
                     khatiyan_count=excluded.khatiyan_count,
                     updated_at=excluded.updated_at""",
                (
                    district_value,
                    district_text,
                    tahasil_value,
                    tahasil_text,
                    village_value,
                    village_text,
                    last_khatiyan_value,
                    last_khatiyan_text,
                    khatiyan_count,
                ),
            )
            conn.commit()

    def get_checkpoint(self) -> Optional[Dict[str, Any]]:
        """
        Return checkpoint for resume, or None if no checkpoint.
        Keys: district_value, district_text, tahasil_value, tahasil_text,
              village_value, village_text, last_khatiyan_value, last_khatiyan_text, khatiyan_count.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT district_value, district_text, tahasil_value, tahasil_text, village_value, village_text, "
            "last_khatiyan_value, last_khatiyan_text, khatiyan_count FROM checkpoint WHERE id = 1"
        ).fetchone()
        if not row or row[0] is None:
            return None
        return {
            "district_value": row[0],
            "district_text": row[1],
            "tahasil_value": row[2],
            "tahasil_text": row[3],
            "village_value": row[4],
            "village_text": row[5],
            "last_khatiyan_value": row[6],
            "last_khatiyan_text": row[7],
            "khatiyan_count": row[8],
        }

    def get_khatiyan_count(self) -> int:
        """Return total khatiyans stored for this district."""
        conn = self._conn()
        r = conn.execute("SELECT COUNT(*) FROM khatiyans").fetchone()
        return r[0] if r else 0

    def get_existing_khatiyans(self, tahasil: str, village: str) -> set:
        """Return set of khatiyan_value strings already successfully stored.

        Excludes needs_review=1 records so the scraper will re-fetch them.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT khatiyan_value FROM khatiyans WHERE tahasil = ? AND village = ? AND needs_review = 0",
            (tahasil, village),
        ).fetchall()
        return {r[0] for r in rows}

    @_retry_on_locked
    def increment_layout_stat(self, ror_type: str) -> None:
        key = ror_type if ror_type in ('type1', 'type2') else 'type1'
        col = 'type1_count' if key == 'type1' else 'type2_count'
        with self._write_lock():
            conn = self._conn()
            conn.execute(
                f"""INSERT INTO layout_stats (id, type1_count, type2_count, updated_at)
                    VALUES (1, {1 if key == 'type1' else 0}, {1 if key == 'type2' else 0}, datetime('now'))
                    ON CONFLICT(id) DO UPDATE SET
                      {col} = {col} + 1,
                      updated_at = datetime('now')"""
            )
            conn.commit()

    def get_layout_stats(self) -> Dict[str, int]:
        conn = self._conn()
        row = conn.execute(
            "SELECT type1_count, type2_count FROM layout_stats WHERE id = 1"
        ).fetchone()
        if not row:
            return {'type1': 0, 'type2': 0}
        return {'type1': row[0] or 0, 'type2': row[1] or 0}

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    def __enter__(self) -> "BhulekhStorage":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def get_storage_manager(
    data_dir: str = DEFAULT_DATA_DIR,
    district_name: str = "",
    backend: str = DEFAULT_STORAGE_BACKEND,
    **kwargs: Any,
) -> BhulekhStorageBase:
    """Return a storage manager for the given district."""
    return create_storage(data_dir=data_dir, district_name=district_name, backend=backend, **kwargs)


def list_district_db_paths(data_dir: str = DEFAULT_DATA_DIR) -> List[Path]:
    """List all district DB files in data_dir."""
    root = Path(data_dir)
    if not root.is_dir():
        return []
    return sorted(root.glob("district_*.db"))


def list_district_data_paths(data_dir: str = DEFAULT_DATA_DIR) -> List[Path]:
    """List all district data files (.db or .ndjson) in data_dir."""
    root = Path(data_dir)
    if not root.is_dir():
        return []
    return sorted(root.glob("district_*.db")) + sorted(root.glob("district_*.ndjson"))

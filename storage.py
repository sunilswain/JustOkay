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
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default directory for all district data and checkpoints
DEFAULT_DATA_DIR = "bhulekh_data"

# Storage backend: "sqlite" or "ndjson"
DEFAULT_STORAGE_BACKEND = "sqlite"


def _sanitize_district_for_filename(name: str) -> str:
    """Make district name safe for use in file/dir names."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip() or "unknown"


def create_storage(
    data_dir: str = DEFAULT_DATA_DIR,
    district_name: str = "",
    backend: str = DEFAULT_STORAGE_BACKEND,
    **kwargs: Any,
) -> "BhulekhStorageBase":
    """Create a storage instance (SQLite or NDJSON) for the given district."""
    if backend.lower() == "ndjson":
        return BhulekhStorageNDJSON(data_dir=data_dir, district_name=district_name, **kwargs)
    return BhulekhStorage(data_dir=data_dir, district_name=district_name, **kwargs)


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
    ):
        """
        Args:
            data_dir: Root directory for DB files (e.g. bhulekh_data).
            district_name: Display name of district (used for DB filename).
            sync_mode: SQLite sync: NORMAL (fast, good durability), FULL (safest, slower).
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.district_name = district_name or "default"
        self.safe_name = _sanitize_district_for_filename(self.district_name)
        self.sync_mode = sync_mode
        self._db_path = self.data_dir / f"district_{self.safe_name}.db"
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(str(self._db_path), check_same_thread=True)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute(f"PRAGMA synchronous={self.sync_mode}")
            self._local.conn.execute("PRAGMA foreign_keys=OFF")
            self._create_tables(self._local.conn)
        return self._local.conn

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
        """)
        # Add columns to existing tables if they don't exist (migration)
        try:
            conn.execute("ALTER TABLE khatiyans ADD COLUMN html_content TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE khatiyans ADD COLUMN needs_review INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()

    def append_khatiyan(self, ror_data: Dict[str, Any], html_content: Optional[str] = None) -> None:
        """
        Append one khatiyan record and its plots. Commits immediately for durability.
        
        Args:
            ror_data: Extracted RoR data dictionary
            html_content: Raw HTML of the RoR page (only stored when extraction has issues)
        """
        conn = self._conn()
        district = ror_data.get("district", "")
        tahasil = ror_data.get("tahasil", "")
        village = ror_data.get("village", "")
        khatiyan_value = ror_data.get("khatiyan_value", "")
        khatiyan_text = ror_data.get("khatiyan_text", "")
        data_json = json.dumps(ror_data, ensure_ascii=False)
        
        # needs_review is 1 if HTML was captured (means extraction had issues)
        needs_review = 1 if html_content else 0
        
        conn.execute(
            """INSERT INTO khatiyans (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, html_content, needs_review)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, html_content, needs_review),
        )
        conn.commit()
    

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

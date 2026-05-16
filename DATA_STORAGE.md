# Bhulekh data storage and multi-worker design

## Goals

- **Reliable**: Data is written to **files** as soon as each khatiyan is fetched; a crash does **not** lose already-fetched data.
- **Resumable**: Each district has a checkpoint (tahasil, village, last khatiyan); re-run with `--resume` to continue.
- **Fast**: Multiple workers run in parallel; optional **NDJSON** backend for plain JSON files and faster appends.
- **Scalable**: One file per district (~630K khatiyans per district); Excel export can be done per district later.

---

## If the script fails mid-district, is data lost?

**No.** Every khatiyan is written to a **file** immediately after it is fetched:

- **SQLite backend**: Each district has one `.db` file. Every `append` is committed to that file right away.
- **NDJSON backend**: Each district has one `.ndjson` file (one JSON object per line). Each khatiyan is appended and flushed to disk.

So if the script or machine dies in the middle of a district, **all data fetched so far for that district is already on disk**. When you run again with `--resume`, it continues from the last checkpoint (same district, same tahasil/village, next khatiyan). Nothing is lost.

---

## Are we writing to files? JSON?

**Yes.** You choose the file format:

| Backend | What is written | File(s) per district |
|--------|------------------|------------------------|
| **sqlite** (default) | One SQLite database | `district_<Name>.db` (queryable, compact) |
| **ndjson** | Plain JSON Lines (one JSON object per line) | `district_<Name>.ndjson` + `district_<Name>_checkpoint.json` |

- **NDJSON** is plain JSON in a file: each line is one full khatiyan record. Often **faster** for raw append (no SQL overhead; just append line + flush). Use `--storage ndjson`.
- **SQLite** is also a file (`.db`); it’s queryable and good for later Excel/analysis.

---

## Storage layout

- **Directory**: `--data-dir` (default: `bhulekh_data/`).
- **Per district** (~630K khatiyans each):
  - **SQLite**: `bhulekh_data/district_<SafeName>.db` (tables: `khatiyans`, `checkpoint`). WAL mode.
  - **NDJSON**: `bhulekh_data/district_<SafeName>.ndjson` (one JSON line per khatiyan) + `district_<SafeName>_checkpoint.json` (for resume).

## 19 million khatiyans in one week

- **19M khatiyans in 7 days** ≈ **2.7M/day** ≈ **113K/hour** ≈ **~31 per second** sustained.
- Each khatiyan needs a browser round-trip (select → View RoR → extract → back). Realistically **~5–15 seconds per khatiyan** per browser (depending on site speed and delays).
  - So **one browser** ≈ **~300–700 khatiyans/hour**.
  - To reach **113K/hour** you need on the order of **~150–400 parallel workers** (browsers).
- Practical approach: run as many workers as your machine(s) and the site can handle (e.g. **50–100+**), use **`--storage ndjson`** for faster writes, and **`--resume`** so any crash or stop doesn’t lose progress. Run 24/7; if you fall short in a week, resume and keep going—no data loss.

---

## Single process (one district or all)

```bash
# Persistent storage + resume (recommended for long runs)
python bhulekh_scraper.py --data-dir bhulekh_data --resume

# Prefer plain JSON files (often faster append)
python bhulekh_scraper.py --data-dir bhulekh_data --resume --storage ndjson

# One district only
python bhulekh_scraper.py --district "Angul" --data-dir bhulekh_data --resume
```

- Data is written to a **file** after **each** khatiyan; checkpoint is updated so resume is fine-grained.
- If the program or machine crashes, run the same command again with `--resume`; it continues from the last checkpoint per district.

## Multi-worker (parallel by district)

```bash
# Many workers (scale up for 19M in a week), NDJSON for faster file writes
python run_workers.py --workers 50 --data-dir bhulekh_data --resume --storage ndjson

# Or SQLite (default)
python run_workers.py --workers 50 --data-dir bhulekh_data --resume

# Testing: headless, limit khatiyans per worker
python run_workers.py --workers 2 --data-dir bhulekh_data --resume --headless --limit-khatiyans 100
```

- The main process fetches the district list once, then starts **N** worker processes.
- Worker `i` gets districts at indices `i, i+N, i+2*N, ...` (no shared files: each district has one DB file).
- Each worker uses `--data-dir` and `--resume` so all data is persistent and resumable.

## Exporting to Excel later

- Excel has ~1M row limit per sheet; a single district can still be huge.
- Options:
  1. **Per-district Excel**: One workbook per district, with multiple sheets (e.g. by tahasil or chunks of 500k rows).
  2. **Query SQLite**: Use the per-district DBs with `sqlite3` or pandas to generate CSV/Excel in chunks.

Example — **SQLite**:

```python
import sqlite3, json
conn = sqlite3.connect("bhulekh_data/district_Angul.db")
for row in conn.execute("SELECT data_json FROM khatiyans"):
    record = json.loads(row[0])
    # flatten and write to CSV/Excel in batches
```

Example — **NDJSON** (plain file, one JSON per line):

```python
import json
with open("bhulekh_data/district_Angul.ndjson", "r", encoding="utf-8") as f:
    for line in f:
        record = json.loads(line)
        # flatten and write to CSV/Excel in batches
```

## Summary

| Concern            | Solution                                                                 |
|--------------------|--------------------------------------------------------------------------|
| No data loss       | Append each khatiyan to SQLite immediately; checkpoint updated every time |
| Resume on crash    | `--resume` + per-district checkpoint (tahasil, village, last khatiyan)   |
| Speed              | `run_workers.py --workers N` (N browsers in parallel, district partition) |
| Storage format     | SQLite per district (queryable, one file per district)                    |

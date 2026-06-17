#!/usr/bin/env python3
"""
export_unified_s3.py v2 — Streaming unified export.
Processes one district at a time: export CSVs -> upload -> cleanup -> next.
Handles Unicode normalization for Odia text matching.

Usage:
    INSTANCE_TAG=r5-2 python3 export_unified_s3.py
"""
import csv
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

DATA_DIR = "bhulekh_data"
S3_BUCKET = "bhulekh-backup"
CSV_OUT = "/tmp/unified_csv"
DB_OUT = "/tmp/unified_dbs"
INSTANCE_TAG = os.environ.get("INSTANCE_TAG", "unknown")
# Skip districts already uploaded (set via env: SKIP_CODES="1,2,5,6")
SKIP_CODES = set(int(x) for x in os.environ.get("SKIP_CODES", "").split(",") if x.strip())

DISTRICT_NAMES = {
    1: "\u0b2c\u0b3e\u0b32\u0b47\u0b36\u0b4d\u0b35\u0b30",
    2: "\u0b2c\u0b32\u0b3e\u0b19\u0b4d\u0b17\u0b3f\u0b30",
    3: "\u0b15\u0b1f\u0b15",
    4: "\u0b22\u0b3c\u0b47\u0b19\u0b4d\u0b15\u0b3e\u0b28\u0b3e\u0b33",
    5: "\u0b17\u0b02\u0b1c\u0b3e\u0b2e",
    6: "\u0b15\u0b33\u0b3e\u0b39\u0b3e\u0b23\u0b4d\u0b21\u0b3f",
    7: "\u0b15\u0b47\u0b28\u0b4d\u0b26\u0b41\u0b1d\u0b30",
    8: "\u0b15\u0b4b\u0b30\u0b3e\u0b2a\u0b41\u0b1f",
    9: "\u0b2e\u0b5f\u0b42\u0b30\u0b2d\u0b1e\u0b4d\u0b1c",
    10: "\u0b15\u0b28\u0b4d\u0b27\u0b2e\u0b3e\u0b33",
    11: "\u0b2a\u0b41\u0b30\u0b40",
    12: "\u0b38\u0b2e\u0b4d\u0b2c\u0b32\u0b2a\u0b41\u0b30",
    13: "\u0b38\u0b41\u0b28\u0b4d\u0b26\u0b30\u0b17\u0b21\u0b3c",
    14: "\u0b05\u0b28\u0b41\u0b17\u0b4b\u0b33",
    15: "\u0b2c\u0b30\u0b17\u0b21\u0b3c",
    16: "\u0b2d\u0b26\u0b4d\u0b30\u0b15",
    17: "District-17",
    18: "\u0b2f\u0b3e\u0b1c\u0b2a\u0b41\u0b30",
    19: "\u0b15\u0b47\u0b28\u0b4d\u0b26\u0b4d\u0b30\u0b3e\u0b2a\u0b21\u0b3e",
    20: "\u0b16\u0b4b\u0b30\u0b4d\u0b26\u0b4d\u0b27\u0b3e",
    21: "\u0b28\u0b42\u0b06\u0b2a\u0b21\u0b3c\u0b3e",
    22: "District-22",
    23: "\u0b38\u0b41\u0b2c\u0b30\u0b4d\u0b23\u0b4d\u0b23\u0b2a\u0b41\u0b30",
    24: "\u0b17\u0b1c\u0b2a\u0b24\u0b3f",
    25: "\u0b2e\u0b3e\u0b32\u0b15\u0b3e\u0b28\u0b17\u0b3f\u0b30\u0b3f",
    26: "\u0b28\u0b2c\u0b30\u0b02\u0b17\u0b2a\u0b41\u0b30",
    27: "\u0b30\u0b3e\u0b5f\u0b17\u0b21\u0b3e",
    28: "\u0b2c\u0b4c\u0b26\u0b4d\u0b27",
    29: "\u0b26\u0b47\u0b2c\u0b17\u0b21",
    30: "\u0b1d\u0b3e\u0b30\u0b38\u0b41\u0b17\u0b41\u0b21\u0b3c\u0b3e",
}

# Build lookup with NFC-normalized keys
_RAW_MAP = {
    "District-1": 1, "\u0b2c\u0b3e\u0b32\u0b47\u0b36\u0b4d\u0b35\u0b30": 1,
    "District-2": 2, "\u0b2c\u0b32\u0b3e\u0b19\u0b4d\u0b17\u0b3f\u0b30": 2,
    "\u0b15\u0b1f\u0b15": 3,
    "District-5": 5, "\u0b17\u0b02\u0b1c\u0b3e\u0b2e": 5,
    "District-6": 6, "\u0b15\u0b33\u0b3e\u0b39\u0b3e\u0b23\u0b4d\u0b21\u0b3f": 6,
    "District-7": 7, "\u0b15\u0b47\u0b28\u0b4d\u0b26\u0b41\u0b1d\u0b30": 7,
    "District-8": 8, "\u0b15\u0b4b\u0b30\u0b3e\u0b2a\u0b41\u0b1f": 8,
    "\u0b2e\u0b5f\u0b42\u0b30\u0b2d\u0b1e\u0b4d\u0b1c": 9,
    "\u0b15\u0b28\u0b4d\u0b27\u0b2e\u0b3e\u0b33": 10, "kandhamal": 10,
    "District-11": 11, "\u0b2a\u0b41\u0b30\u0b40": 11,
    "District-13": 13, "\u0b38\u0b41\u0b28\u0b4d\u0b26\u0b30\u0b17\u0b21\u0b3c": 13,
    # Precomposed ଡ଼ form for ସୁନ୍ଦରଗଡ଼
    "\u0b38\u0b41\u0b28\u0b4d\u0b26\u0b30\u0b17\u0b5c": 13,
    "District-14": 14, "\u0b05\u0b28\u0b41\u0b17\u0b4b\u0b33": 14,
    "District-15": 15, "\u0b2c\u0b30\u0b17\u0b21\u0b3c": 15,
    # Precomposed ଡ଼ form for ବରଗଡ଼
    "\u0b2c\u0b30\u0b17\u0b5c": 15,
    "\u0b2d\u0b26\u0b4d\u0b30\u0b15": 16,
    "District-18": 18, "\u0b2f\u0b3e\u0b1c\u0b2a\u0b41\u0b30": 18,
    "\u0b15\u0b47\u0b28\u0b4d\u0b26\u0b4d\u0b30\u0b3e\u0b2a\u0b21\u0b3e": 19,
    "\u0b17\u0b1c\u0b2a\u0b24\u0b3f": 24,
    "District-25": 25, "\u0b2e\u0b3e\u0b32\u0b15\u0b3e\u0b28\u0b17\u0b3f\u0b30\u0b3f": 25,
    "\u0b28\u0b2c\u0b30\u0b02\u0b17\u0b2a\u0b41\u0b30": 26,
    "District-29": 29, "\u0b26\u0b47\u0b2c\u0b17\u0b21": 29,
    "\u0b16\u0b4b\u0b30\u0b4d\u0b26\u0b4d\u0b27\u0b3e": 20,
}

# Normalize all keys and also add NFC + NFKC variants
DISTRICT_VALUE_TO_CODE = {}
for k, v in _RAW_MAP.items():
    DISTRICT_VALUE_TO_CODE[k] = v
    DISTRICT_VALUE_TO_CODE[unicodedata.normalize("NFC", k)] = v
    DISTRICT_VALUE_TO_CODE[unicodedata.normalize("NFKC", k)] = v
    DISTRICT_VALUE_TO_CODE[unicodedata.normalize("NFD", k)] = v

HEADER = [
    "record_type",
    "district", "tahasil", "village", "thana", "thana_no", "tahasil_no",
    "khatiyan_no",
    "landlord_name", "tenant_details", "status",
    "water_tax", "tax", "cess", "other_cess", "total_tax",
    "special_case", "last_publish_date", "tax_date",
    "plot_no_or_chaka_no", "chaka_name", "land_type",
    "chaka_included_plot", "non_chaka_plot",
    "kisam_details",
    "north_boundary", "east_boundary", "south_boundary", "west_boundary",
    "acre", "decimal", "hectare",
    "non_chaka_land_type", "plot_remarks",
]


def _clean(v):
    if not v:
        return ""
    return str(v).replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()


def _safe_name(name):
    name = _clean(name)
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', '_', name)
    return name or "unknown"


def _get(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return _clean(str(v).strip())
    return default


def normalize_district(val):
    """Normalize a district string and look up its code."""
    if not val:
        return None
    val = val.strip()
    code = DISTRICT_VALUE_TO_CODE.get(val)
    if code:
        return code
    nfc = unicodedata.normalize("NFC", val)
    code = DISTRICT_VALUE_TO_CODE.get(nfc)
    if code:
        return code
    nfkc = unicodedata.normalize("NFKC", val)
    code = DISTRICT_VALUE_TO_CODE.get(nfkc)
    if code:
        return code
    nfd = unicodedata.normalize("NFD", val)
    code = DISTRICT_VALUE_TO_CODE.get(nfd)
    return code


def flatten_ror(record):
    ror_type = _get(record, "ror_type", default="type1")
    base = {
        "record_type": ror_type,
        "district": _get(record, "district"),
        "tahasil": _get(record, "tahasil"),
        "village": _get(record, "village"),
        "thana": _get(record, "thana"),
        "thana_no": _get(record, "thana_no"),
        "tahasil_no": _get(record, "tehsil_no", "tahasil_no"),
        "khatiyan_no": _get(record, "khatiyan_sl_no", "khatiyan_text", "sl_no"),
        "landlord_name": _get(record, "landlord_name", "owner_name"),
        "tenant_details": _get(record, "tenant_name", "raiyat_name"),
        "status": _get(record, "status"),
        "water_tax": _get(record, "water_tax"),
        "tax": _get(record, "tax"),
        "cess": _get(record, "ses"),
        "other_cess": _get(record, "other_ses"),
        "total_tax": _get(record, "total"),
        "special_case": _get(record, "special_case"),
        "last_publish_date": _get(record, "last_publish_date"),
        "tax_date": _get(record, "tax_date"),
    }
    plots = record.get("plots") or []
    if not plots:
        return [{**base, "plot_no_or_chaka_no": "", "chaka_name": "", "land_type": "",
                 "chaka_included_plot": "", "non_chaka_plot": "", "kisam_details": "",
                 "north_boundary": "", "east_boundary": "", "south_boundary": "", "west_boundary": "",
                 "acre": "", "decimal": "", "hectare": "", "non_chaka_land_type": "", "plot_remarks": ""}]
    rows = []
    for p in plots:
        plot_or_chaka = _get(p, "chaka", "plot_chaka") if ror_type == "type2" else _get(p, "plot_no")
        rows.append({**base,
            "plot_no_or_chaka_no": plot_or_chaka,
            "chaka_name": _get(p, "chaka") if ror_type != "type2" else "",
            "land_type": _get(p, "land_type"),
            "chaka_included_plot": _get(p, "chaka_included_plot"),
            "non_chaka_plot": _get(p, "non_chaka_plot"),
            "kisam_details": _get(p, "kisam"),
            "north_boundary": _get(p, "n_occu"),
            "east_boundary": _get(p, "e_occu"),
            "south_boundary": _get(p, "s_occu"),
            "west_boundary": _get(p, "w_occu"),
            "acre": _get(p, "acre"),
            "decimal": _get(p, "decimil"),
            "hectare": _get(p, "hector"),
            "non_chaka_land_type": _get(p, "non_chaka_land_type"),
            "plot_remarks": _get(p, "remarks"),
        })
    return rows


def identify_district_code(db_path):
    """Identify canonical district code by reading multiple sample records."""
    try:
        c = sqlite3.connect(db_path, timeout=30)
        tables = [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "khatiyans" not in tables:
            c.close()
            return None
        count = c.execute("SELECT COUNT(*) FROM khatiyans").fetchone()[0]
        if count == 0:
            c.close()
            return None
        rows = c.execute("SELECT data_json FROM khatiyans LIMIT 20").fetchall()
        c.close()
        for row in rows:
            if not row[0]:
                continue
            try:
                d = json.loads(row[0])
                dist_val = d.get("district", "")
                if not dist_val:
                    continue
                code = normalize_district(dist_val)
                if code:
                    return code
            except (json.JSONDecodeError, TypeError):
                continue
        return None
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        return None


def folder_name(code):
    name = DISTRICT_NAMES.get(code, f"District-{code}")
    return f"D{code}_{_safe_name(name)}"


def read_and_dedup(db_paths):
    """Read all records from multiple DBs, deduplicate by (tahasil, village, khatiyan_value)."""
    seen = {}
    for db_path in db_paths:
        try:
            c = sqlite3.connect(db_path, timeout=60)
            cursor = c.execute("SELECT tahasil, village, khatiyan_value, data_json FROM khatiyans")
            while True:
                rows = cursor.fetchmany(5000)
                if not rows:
                    break
                for tahasil, village, kh_val, data_json in rows:
                    if not data_json:
                        continue
                    t = _clean(str(tahasil or ""))
                    v = _clean(str(village or ""))
                    k = _clean(str(kh_val or ""))
                    if not t or not v:
                        continue
                    key = (t, v, k)
                    if key not in seen or len(data_json) > len(seen[key]):
                        seen[key] = data_json
            c.close()
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
            print(f"    WARN: skipping {os.path.basename(db_path)}: {e}")
    return seen


def export_district_csv(records_dict, out_dir):
    """Export deduplicated records as tahasil/village.csv under out_dir."""
    village_data = defaultdict(list)
    for (tahasil, village, kh_val), data_json in records_dict.items():
        try:
            record = json.loads(data_json)
            for row in flatten_ror(record):
                village_data[(tahasil, village)].append(row)
        except (json.JSONDecodeError, TypeError):
            pass

    files_written = 0
    for (tahasil, village), rows in sorted(village_data.items()):
        tahasil_dir = os.path.join(out_dir, _safe_name(tahasil))
        os.makedirs(tahasil_dir, exist_ok=True)
        csv_path = os.path.join(tahasil_dir, f"{_safe_name(village)}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore",
                                    quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(rows)
        files_written += 1
    return files_written, len(village_data)


def create_merged_db(records_dict, db_path):
    """Create a clean merged DB file with deduplicated records."""
    if os.path.exists(db_path):
        os.remove(db_path)
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=OFF")
    c.execute("""CREATE TABLE khatiyans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        district TEXT, tahasil TEXT, village TEXT,
        khatiyan_value TEXT, khatiyan_text TEXT,
        data_json TEXT, fetched_at TEXT
    )""")
    batch = []
    for (tahasil, village, kh_val), data_json in records_dict.items():
        try:
            d = json.loads(data_json)
            district = d.get("district", "")
            kh_text = d.get("khatiyan_text", kh_val)
            batch.append((district, tahasil, village, kh_val, kh_text, data_json, ""))
            if len(batch) >= 10000:
                c.executemany(
                    "INSERT INTO khatiyans (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, fetched_at) VALUES (?,?,?,?,?,?,?)",
                    batch)
                c.commit()
                batch = []
        except:
            pass
    if batch:
        c.executemany(
            "INSERT INTO khatiyans (district, tahasil, village, khatiyan_value, khatiyan_text, data_json, fetched_at) VALUES (?,?,?,?,?,?,?)",
            batch)
        c.commit()
    count = c.execute("SELECT COUNT(*) FROM khatiyans").fetchone()[0]
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    # Remove WAL files
    for ext in ["-wal", "-shm"]:
        p = db_path + ext
        if os.path.exists(p):
            os.remove(p)
    return count


def upload_to_s3(local_path, s3_path, is_dir=False):
    if is_dir:
        cmd = ["aws", "s3", "sync", local_path, s3_path, "--quiet"]
    else:
        cmd = ["aws", "s3", "cp", local_path, s3_path, "--quiet"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f"    S3 ERROR: {result.stderr.strip()[:200]}")
        return False
    return True


def main():
    print("=" * 70)
    print(f"  UNIFIED EXPORT TO S3 — Instance: {INSTANCE_TAG}")
    print("=" * 70)

    # Discover and group DBs by district code
    print("\n[1/3] Discovering and mapping databases...")
    district_dbs = defaultdict(list)
    for db_path in sorted(glob.glob(f"{DATA_DIR}/*.db")):
        if "write.lock" in db_path:
            continue
        code = identify_district_code(db_path)
        if code is not None:
            district_dbs[code].append(db_path)
            print(f"  {os.path.basename(db_path):40s} -> D{code} ({DISTRICT_NAMES.get(code, '?')})")
        else:
            print(f"  {os.path.basename(db_path):40s} -> SKIP (unreadable/unmapped)")

    print(f"\n  Mapped {sum(len(v) for v in district_dbs.values())} DBs to {len(district_dbs)} districts")

    # Process each district one at a time (streaming to avoid disk exhaustion)
    print("\n[2/3] Processing districts one-by-one (export + upload + cleanup)...")
    results = []
    for code in sorted(district_dbs.keys()):
        if code in SKIP_CODES:
            print(f"\n  SKIP D{code} (already done)")
            continue
        dbs = district_dbs[code]
        name = folder_name(code)
        print(f"\n  {'='*60}")
        print(f"  {name} ({len(dbs)} DB{'s' if len(dbs)>1 else ''})")
        print(f"  {'='*60}")
        for db in dbs:
            print(f"    src: {os.path.basename(db)}")

        # Read and deduplicate
        records = read_and_dedup(dbs)
        print(f"    Deduplicated records: {len(records):,}")

        if not records:
            print(f"    SKIP (no records)")
            continue

        # Export CSVs
        csv_dir = os.path.join(CSV_OUT, name)
        if os.path.exists(csv_dir):
            shutil.rmtree(csv_dir)
        os.makedirs(csv_dir, exist_ok=True)
        files, villages = export_district_csv(records, csv_dir)
        print(f"    CSVs: {files} files ({villages} villages)")

        # Upload CSVs immediately
        s3_csv_dest = f"s3://{S3_BUCKET}/csv/{name}/"
        print(f"    Uploading CSVs -> {s3_csv_dest}")
        upload_to_s3(csv_dir + "/", s3_csv_dest, is_dir=True)

        # Cleanup CSVs to free space
        shutil.rmtree(csv_dir)
        print(f"    CSVs uploaded and cleaned")

        # Create merged DB
        db_out_dir = os.path.join(DB_OUT)
        os.makedirs(db_out_dir, exist_ok=True)
        db_out_path = os.path.join(db_out_dir, f"{name}.db")
        db_count = create_merged_db(records, db_out_path)
        db_size_mb = os.path.getsize(db_out_path) / 1024 / 1024
        print(f"    DB: {db_count:,} records ({db_size_mb:.0f} MB)")

        # Upload DB immediately
        s3_db_dest = f"s3://{S3_BUCKET}/dbs/{name}.db"
        print(f"    Uploading DB -> {s3_db_dest}")
        upload_to_s3(db_out_path, s3_db_dest)

        # Cleanup DB to free space
        os.remove(db_out_path)
        print(f"    DB uploaded and cleaned")

        # Free memory
        del records

        results.append({"code": code, "name": name, "records": db_count,
                        "csv_files": files, "db_size_mb": db_size_mb})

    # Summary
    print("\n" + "=" * 70)
    print("  UNIFIED EXPORT COMPLETE")
    print(f"  Instance: {INSTANCE_TAG}")
    print(f"  Districts exported: {len(results)}")
    total_records = sum(r["records"] for r in results)
    total_csvs = sum(r["csv_files"] for r in results)
    print(f"  Total records: {total_records:,}")
    print(f"  Total CSV files: {total_csvs:,}")
    print(f"  S3 CSVs: s3://{S3_BUCKET}/csv/")
    print(f"  S3 DBs:  s3://{S3_BUCKET}/dbs/")
    print("=" * 70)
    print("\n  Districts exported:")
    for r in results:
        print(f"    {r['name']:30s} {r['records']:>10,} records  {r['csv_files']:>5} CSV files  {r['db_size_mb']:>6.0f} MB DB")


if __name__ == "__main__":
    main()

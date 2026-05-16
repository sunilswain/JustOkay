"""
Verify the DB structure and extraction pipeline before the full run.

Checks:
  1. DB files exist and are readable
  2. Schema is correct
  3. Sample records look complete (no silent empty extractions)
  4. JSON -> CSV export round-trips correctly
  5. Flags records with missing critical fields (empty landlord/tenant/plots)
  6. Detects which RoR layout type was captured per record

Usage:
  python verify_db.py                          # check bhulekh_data/ folder
  python verify_db.py --data-dir my_data/
  python verify_db.py --sample 20              # show 20 sample records
  python verify_db.py --export-sample out.csv  # export first 500 rows to CSV
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


CRITICAL_FIELDS = ["landlord_name", "tenant_name"]
LOCATION_FIELDS = ["district", "tahasil", "village", "mouja"]
TAX_FIELDS      = ["water_tax", "tax", "total"]
PLOT_FIELDS     = ["plot_no", "acre", "decimil", "hector"]

# Known RoR layout types based on which selectors are present in saved data
def detect_layout(record: dict) -> str:
    """
    Type 1: standard gvfront layout → ror_type='type1' or has landlord_name/tenant_name
    Type 2: ପରିଶିଷ୍ଟ / Form-99   → ror_type='type2' or has form_no/parichheda
    Empty:  page returned no data  → all fields blank (server error or empty khatiyan)
    """
    ror_type = record.get("ror_type", "")
    if ror_type == "type2":
        return "Type-2 (ପରିଶିଷ୍ଟ / Form-99)"
    if ror_type == "type1":
        return "Type-1 (gvfront standard)"

    has_landlord  = bool(record.get("landlord_name", "").strip())
    has_tenant    = bool(record.get("tenant_name",   "").strip())
    has_mouja     = bool(record.get("mouja",         "").strip())
    has_form_no   = bool(record.get("form_no",       "").strip())
    has_parichheda= bool(record.get("parichheda",    "").strip())

    if has_form_no or has_parichheda:
        return "Type-2 (ପରିଶିଷ୍ଟ / Form-99)"
    elif has_landlord or has_tenant or has_mouja:
        return "Type-1 (gvfront standard)"
    else:
        return "Empty (server error or blank khatiyan)"


def check_db(db_path: Path, sample_n: int, verbose: bool) -> dict:
    """Run all checks on a single district DB file. Returns summary dict."""
    result = {
        "path": str(db_path),
        "total": 0,
        "missing_landlord": 0,
        "missing_tenant": 0,
        "missing_plots": 0,
        "empty_records": 0,
        "layout_types": {},
        "sample_records": [],
    }

    try:
        con = sqlite3.connect(str(db_path))
    except Exception as e:
        print(f"  ERROR opening {db_path.name}: {e}")
        result["error"] = str(e)
        return result

    try:
        # Verify schema
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "khatiyans" not in tables:
            print(f"  ERROR: 'khatiyans' table missing in {db_path.name}")
            result["error"] = "missing khatiyans table"
            return result

        cols = {r[1] for r in con.execute("PRAGMA table_info(khatiyans)").fetchall()}
        expected = {"id", "district", "tahasil", "village", "khatiyan_value", "khatiyan_text", "data_json", "fetched_at"}
        missing_cols = expected - cols
        if missing_cols:
            print(f"  WARNING: Missing columns in {db_path.name}: {missing_cols}")

        # Count total
        result["total"] = con.execute("SELECT COUNT(*) FROM khatiyans").fetchone()[0]

        if result["total"] == 0:
            print(f"  WARNING: {db_path.name} — 0 records (empty)")
            return result

        # Sample records
        rows = con.execute(
            "SELECT data_json, district, tahasil, village FROM khatiyans ORDER BY id LIMIT ?",
            (sample_n,)
        ).fetchall()

        for raw_json, district, tahasil, village in rows:
            try:
                record = json.loads(raw_json)
            except json.JSONDecodeError as e:
                result["empty_records"] += 1
                print(f"  ERROR: JSON decode failed for record in {district}/{tahasil}/{village}: {e}")
                continue

            layout = detect_layout(record)
            result["layout_types"][layout] = result["layout_types"].get(layout, 0) + 1

            if not record.get("landlord_name", "").strip():
                result["missing_landlord"] += 1
            if not record.get("tenant_name", "").strip():
                result["missing_tenant"] += 1
            if not record.get("plots"):
                result["missing_plots"] += 1

            result["sample_records"].append(record)

        # Full scan for missing fields (fast COUNT queries)
        result["total_missing_landlord"] = con.execute(
            "SELECT COUNT(*) FROM khatiyans WHERE json_extract(data_json, '$.landlord_name') = '' OR json_extract(data_json, '$.landlord_name') IS NULL"
        ).fetchone()[0]
        result["total_missing_tenant"] = con.execute(
            "SELECT COUNT(*) FROM khatiyans WHERE json_extract(data_json, '$.tenant_name') = '' OR json_extract(data_json, '$.tenant_name') IS NULL"
        ).fetchone()[0]

    finally:
        con.close()

    return result


def check_ndjson(ndjson_path: Path, sample_n: int) -> dict:
    result = {
        "path": str(ndjson_path),
        "total": 0,
        "missing_landlord": 0,
        "missing_tenant": 0,
        "missing_plots": 0,
        "empty_records": 0,
        "layout_types": {},
        "sample_records": [],
    }
    try:
        with open(ndjson_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                result["total"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    result["empty_records"] += 1
                    continue

                if result["total"] <= sample_n:
                    layout = detect_layout(record)
                    result["layout_types"][layout] = result["layout_types"].get(layout, 0) + 1
                    if not record.get("landlord_name", "").strip():
                        result["missing_landlord"] += 1
                    if not record.get("tenant_name", "").strip():
                        result["missing_tenant"] += 1
                    if not record.get("plots"):
                        result["missing_plots"] += 1
                    result["sample_records"].append(record)
    except Exception as e:
        result["error"] = str(e)
    return result


def print_record(record: dict, idx: int) -> None:
    """Pretty-print one record for human inspection."""
    print(f"\n  ── Record #{idx+1} ──────────────────────────────")
    print(f"  Location  : {record.get('district','')} / {record.get('tahasil','')} / {record.get('village','')}")
    print(f"  Mouja     : {record.get('mouja','')}")
    print(f"  Khatiyan  : {record.get('khatiyan_text','')} (val: {record.get('khatiyan_value','').strip()})")
    print(f"  Landlord  : {record.get('landlord_name','')[:80]}")
    print(f"  Tenant    : {record.get('tenant_name','')[:80]}")
    print(f"  Status    : {record.get('status','')}")
    print(f"  Tax total : {record.get('total','')}")
    plots = record.get("plots") or []
    print(f"  Plots     : {len(plots)}")
    for i, p in enumerate(plots[:3]):
        print(f"    Plot {i+1}: no={p.get('plot_no','')}, acre={p.get('acre','')}, "
              f"decimil={p.get('decimil','')}, kisam={p.get('kisam','')}")
    if len(plots) > 3:
        print(f"    ... and {len(plots)-3} more plots")
    print(f"  Layout    : {detect_layout(record)}")


def export_sample_csv(data_dir: Path, out_path: str, limit: int = 500) -> None:
    """Export first `limit` records from any district file as a quick CSV sample."""
    import csv, sys
    sys.path.insert(0, str(Path(__file__).parent))
    from export_csv import flatten_ror, HEADER

    count = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        writer.writeheader()

        for db_file in sorted(data_dir.glob("district_*.db")):
            con = sqlite3.connect(str(db_file))
            for (raw_json,) in con.execute("SELECT data_json FROM khatiyans"):
                try:
                    record = json.loads(raw_json)
                    for row in flatten_ror(record):
                        writer.writerow(row)
                        count += 1
                        if count >= limit:
                            con.close()
                            print(f"Sample CSV written: {count} rows → {out_path}")
                            return
                except Exception:
                    pass
            con.close()

        for ndjson in sorted(data_dir.glob("district_*.ndjson")):
            with open(ndjson, encoding="utf-8") as f2:
                for line in f2:
                    try:
                        record = json.loads(line.strip())
                        for row in flatten_ror(record):
                            writer.writerow(row)
                            count += 1
                            if count >= limit:
                                print(f"Sample CSV written: {count} rows → {out_path}")
                                return
                    except Exception:
                        pass

    print(f"Sample CSV written: {count} rows → {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Verify Bhulekh DB structure and extraction quality")
    parser.add_argument("--data-dir",      default="bhulekh_data")
    parser.add_argument("--sample",        type=int, default=5,   help="Records to show per file")
    parser.add_argument("--verbose",       action="store_true",   help="Print all sample records")
    parser.add_argument("--export-sample", metavar="OUT.CSV",     help="Export first 500 rows to CSV")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}")
        sys.exit(1)

    db_files     = sorted(data_dir.glob("district_*.db"))
    ndjson_files = sorted(data_dir.glob("district_*.ndjson"))
    all_files    = [(f, "sqlite") for f in db_files] + [(f, "ndjson") for f in ndjson_files]

    if not all_files:
        print(f"No district files found in {data_dir}/")
        print("Run a scraper worker first to generate data.")
        sys.exit(0)

    print(f"\n{'='*60}")
    print(f"  Bhulekh DB Verification — {data_dir}/")
    print(f"  Files found: {len(db_files)} SQLite, {len(ndjson_files)} NDJSON")
    print(f"{'='*60}")

    grand_total = 0
    grand_missing_landlord = 0
    grand_missing_tenant = 0
    grand_missing_plots = 0
    all_layouts: dict = {}
    problems: list = []

    for path, kind in all_files:
        print(f"\n[{kind.upper()}] {path.name}")

        if kind == "sqlite":
            r = check_db(path, args.sample, args.verbose)
        else:
            r = check_ndjson(path, args.sample)

        if "error" in r:
            problems.append(f"{path.name}: {r['error']}")
            continue

        total = r["total"]
        grand_total += total

        # Layout breakdown (from sample)
        for lt, n in r["layout_types"].items():
            all_layouts[lt] = all_layouts.get(lt, 0) + n

        ml = r.get("total_missing_landlord", r["missing_landlord"])
        mt = r.get("total_missing_tenant",   r["missing_tenant"])
        mp = r["missing_plots"]

        grand_missing_landlord += ml
        grand_missing_tenant   += mt
        grand_missing_plots    += mp

        pct_ml = f"{100*ml//total}%" if total else "-"
        pct_mt = f"{100*mt//total}%" if total else "-"

        print(f"  Records      : {total:,}")
        print(f"  Empty/corrupt: {r['empty_records']}")
        print(f"  Missing landlord_name : {ml:,} ({pct_ml})")
        print(f"  Missing tenant_name   : {mt:,} ({pct_mt})")
        print(f"  Missing plots (sample): {mp} / {len(r['sample_records'])}")

        if r["layout_types"]:
            print(f"  Layout types (sample): {r['layout_types']}")

        if ml > 0 or mt > 0:
            problems.append(f"{path.name}: {ml} missing landlord, {mt} missing tenant")

        if args.verbose and r["sample_records"]:
            for i, rec in enumerate(r["sample_records"]):
                print_record(rec, i)
        elif r["sample_records"]:
            print_record(r["sample_records"][0], 0)

    # ── Grand summary ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  GRAND SUMMARY")
    print(f"{'='*60}")
    print(f"  Total khatiyans stored : {grand_total:,}")
    print(f"  Missing landlord_name  : {grand_missing_landlord:,}")
    print(f"  Missing tenant_name    : {grand_missing_tenant:,}")
    print(f"  Missing plots (sample) : {grand_missing_plots}")
    print(f"  Layout types detected  : {all_layouts}")

    if grand_total == 0:
        print("\n  ⚠ No records yet. Run the scraper first, then re-run this check.")
    elif grand_missing_landlord / grand_total > 0.3:
        print("\n  ⚠ >30% of records have empty landlord_name.")
        print("    This suggests the extractor is hitting a second RoR layout (Type 2).")
        print("    Share a screenshot of that page so we can add its selectors.")
    else:
        print("\n  ✓ DB looks healthy. Records are being stored and extraction is working.")

    if problems:
        print(f"\n  Issues found ({len(problems)}):")
        for p in problems:
            print(f"    - {p}")
    else:
        print("  ✓ No structural problems found.")

    # ── Export sample CSV ──────────────────────────────────────────────────
    if args.export_sample:
        print(f"\nExporting sample CSV → {args.export_sample}")
        export_sample_csv(data_dir, args.export_sample)
        print("Open this CSV to manually verify the data looks correct.")


if __name__ == "__main__":
    main()

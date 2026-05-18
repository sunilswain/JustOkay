"""
Export all scraped Bhulekh data to CSV.

Reads from every district_*.db (SQLite) and district_*.ndjson file in --data-dir.
One output row per PLOT (khatiyan header info is repeated for each plot).
Handles both RoR layout types automatically.

Usage:
    # Export everything to one big CSV
    python export_csv.py --data-dir bhulekh_data --out bhulekh_all.csv

    # Export only specific districts
    python export_csv.py --data-dir bhulekh_data --out angul.csv --districts 14

    # Export as TSV (easier to open in Excel with Odia text)
    python export_csv.py --data-dir bhulekh_data --out bhulekh_all.tsv --sep tab

    # Show stats without exporting
    python export_csv.py --data-dir bhulekh_data --stats-only
"""

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


# ── Column order in output CSV ────────────────────────────────────────────────

HEADER = [
    # Serial number (added during export, not stored in DB)
    "sl_no",
    # Location
    "district", "tahasil", "village",
    "mouja", "tehsil", "thana", "tehsil_no", "thana_no",
    # Khatiyan identity
    "khatiyan_value", "khatiyan_text", "khatiyan_sl_no",
    # Ownership
    "landlord_name", "tenant_name", "status",
    # Tax
    "water_tax", "tax", "ses", "other_ses", "total",
    # Dates / metadata
    "last_publish_date", "tax_date", "description", "special_case",
    # Type-2 specific (Form 99 / ପରିଶିଷ୍ଟ layout)
    "form_no", "parichheda",
    # Layout type tag (type1 / type2)
    "ror_type",
    # Plot details (one row per plot)
    "plot_no", "plot_chaka", "plot_land_type", "plot_kisam",
    "plot_n_occu", "plot_e_occu", "plot_s_occu", "plot_w_occu",
    "plot_acre", "plot_decimil", "plot_hector", "plot_remarks",
]


# ── Text sanitiser ────────────────────────────────────────────────────────────

def _clean(v: str) -> str:
    """Replace embedded newlines / carriage-returns / tabs with a single space."""
    if not v:
        return v
    return v.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ").strip()


# ── Khatiyan sort-key helper ──────────────────────────────────────────────────

import re as _re

def _khatiyan_sort_key(val: str) -> tuple:
    """
    Return a (int_prefix, remainder) tuple so khatiyans sort numerically.
    '1', '2', '10' → (1,''), (2,''), (10,'')  instead of lexicographic '1','10','2'.
    '1-kha', '2-kha' → (1,'-kha'), (2,'-kha')
    """
    val = val.strip()
    m = _re.match(r"^(\d+)(.*)", val)
    if m:
        return (int(m.group(1)), m.group(2).strip())
    return (0, val)


# ── RoR flattener — handles two layout types ──────────────────────────────────

def _get(d: dict, *keys, default="") -> str:
    """Try multiple key names, return first match (handles layout differences)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return _clean(str(v).strip())
    return default


def flatten_ror(record: dict) -> List[dict]:
    """
    Convert one parsed RoR record (with nested plots list) into
    one flat dict per plot.  If there are no plots, returns one row
    with empty plot fields.

    Handles two known RoR layouts:
      Layout A — fields: landlord_name, tenant_name, plots[{plot_no, acre, ...}]
      Layout B — fields: owner_name, raiyat_name, plot_details[{plot_number, area, ...}]
    """
    base = {
        # Location
        "district":        _get(record, "district"),
        "tahasil":         _get(record, "tahasil"),
        "village":         _get(record, "village"),
        "mouja":           _get(record, "mouja"),
        "tehsil":          _get(record, "tehsil"),
        "thana":           _get(record, "thana"),
        "tehsil_no":       _get(record, "tehsil_no"),
        "thana_no":        _get(record, "thana_no"),
        # Khatiyan identity
        "khatiyan_value":  _get(record, "khatiyan_value"),
        "khatiyan_text":   _get(record, "khatiyan_text"),
        "khatiyan_sl_no":  _get(record, "khatiyan_sl_no", "sl_no"),
        # Ownership — Layout A uses landlord_name/tenant_name
        #             Layout B uses owner_name/raiyat_name
        "landlord_name":   _get(record, "landlord_name", "owner_name", "khewat_name"),
        "tenant_name":     _get(record, "tenant_name",   "raiyat_name", "occupant_name"),
        "status":          _get(record, "status"),
        # Tax
        "water_tax":       _get(record, "water_tax"),
        "tax":             _get(record, "tax"),
        "ses":             _get(record, "ses"),
        "other_ses":       _get(record, "other_ses"),
        "total":           _get(record, "total"),
        # Dates / metadata
        "last_publish_date": _get(record, "last_publish_date"),
        "tax_date":          _get(record, "tax_date"),
        "description":       _get(record, "description"),
        "special_case":      _get(record, "special_case"),
        # Type-2 specific fields
        "form_no":           _get(record, "form_no"),
        "parichheda":        _get(record, "parichheda"),
        "ror_type":          _get(record, "ror_type", default="type1"),
    }

    # Plots: try both known field names
    plots: list = record.get("plots") or record.get("plot_details") or []

    if not plots:
        # No plots: return single row with empty plot columns
        return [{**base,
                 "plot_no": "", "plot_chaka": "", "plot_land_type": "",
                 "plot_kisam": "", "plot_n_occu": "", "plot_e_occu": "",
                 "plot_s_occu": "", "plot_w_occu": "", "plot_acre": "",
                 "plot_decimil": "", "plot_hector": "", "plot_remarks": ""}]

    rows = []
    for p in plots:
        rows.append({**base,
            # Layout A: plot_no / chaka / kisam / acre / decimil / hector
            # Layout B: plot_number / area_acre / area_decimal
            "plot_no":        _get(p, "plot_no",    "plot_number",  "plot_plot_no"),
            "plot_chaka":     _get(p, "chaka",       "plot_chaka"),
            "plot_land_type": _get(p, "land_type",   "plot_land_type"),
            "plot_kisam":     _get(p, "kisam",       "plot_kisam"),
            "plot_n_occu":    _get(p, "n_occu",      "plot_n_occu",  "north"),
            "plot_e_occu":    _get(p, "e_occu",      "plot_e_occu",  "east"),
            "plot_s_occu":    _get(p, "s_occu",      "plot_s_occu",  "south"),
            "plot_w_occu":    _get(p, "w_occu",      "plot_w_occu",  "west"),
            "plot_acre":      _get(p, "acre",        "plot_acre",    "area_acre"),
            "plot_decimil":   _get(p, "decimil",     "plot_decimil", "area_decimal"),
            "plot_hector":    _get(p, "hector",      "plot_hector",  "area_hector"),
            "plot_remarks":   _get(p, "remarks",     "plot_remarks"),
        })
    return rows


# ── Readers for each storage backend ─────────────────────────────────────────

def _read_sqlite(db_path: Path, sort: bool = True) -> Iterator[dict]:
    """
    Read khatiyans from a district SQLite file.
    When sort=True, records are returned sorted by:
      tahasil → village → khatiyan_value (numerically).
    Sorting is done in Python after loading the district's records,
    so memory usage is bounded per district (~20-100 MB each).
    """
    con = sqlite3.connect(str(db_path))
    try:
        records = []
        for row in con.execute("SELECT data_json FROM khatiyans ORDER BY id"):
            try:
                records.append(json.loads(row[0]))
            except json.JSONDecodeError:
                pass
    finally:
        con.close()

    if sort:
        records.sort(key=lambda r: (
            _clean(str(r.get("tahasil", "") or "")),
            _clean(str(r.get("village", "") or "")),
            _khatiyan_sort_key(_clean(str(r.get("khatiyan_value", "") or r.get("khatiyan_text", "") or ""))),
        ))

    yield from records


def _read_ndjson(ndjson_path: Path) -> Iterator[dict]:
    with open(ndjson_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass


def iter_all_records(
    data_dir: str,
    district_filter: Optional[List[int]] = None,
    sort: bool = True,
) -> Iterator[dict]:
    """
    Yield every raw RoR record from all district files.
    Files are processed in alphabetical order of their name (= district name order).
    Within each district file records are sorted by tahasil → village → khatiyan
    when sort=True (the default).
    """
    root = Path(data_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")

    # Collect all district files — sort by filename for consistent district order
    db_files     = sorted(root.glob("district_*.db"))
    ndjson_files = sorted(root.glob("district_*.ndjson"))

    # Prefer .db over .ndjson when both exist for the same district
    seen_stems = {f.stem for f in db_files}
    ndjson_files = [f for f in ndjson_files if f.stem not in seen_stems]

    all_files = [(f, "sqlite") for f in db_files] + [(f, "ndjson") for f in ndjson_files]

    if not all_files:
        print(f"No district files found in {data_dir}", file=sys.stderr)
        return

    for path, kind in all_files:
        if kind == "sqlite":
            records = _read_sqlite(path, sort=sort)
        else:
            records = _read_ndjson(path)   # ndjson stays unsorted for now

        for record in records:
            # Optional district filter (match on district_code or district name)
            if district_filter:
                d_code = record.get("district_code") or record.get("dCode")
                if d_code is not None and int(d_code) not in district_filter:
                    continue
            yield record


# ── Main export logic ─────────────────────────────────────────────────────────

def export(
    data_dir: str,
    out_path: str,
    separator: str = ",",
    district_filter: Optional[List[int]] = None,
    stats_only: bool = False,
    sort: bool = True,
) -> None:
    total_records = 0
    total_rows    = 0
    sl_no         = 0          # running serial number across all rows

    if stats_only:
        root = Path(data_dir)
        for path in sorted(root.glob("district_*.db")):
            con = sqlite3.connect(str(path))
            n = con.execute("SELECT COUNT(*) FROM khatiyans").fetchone()[0]
            con.close()
            print(f"  {path.name:50s}  {n:>10,} khatiyans")
        for path in sorted(root.glob("district_*.ndjson")):
            n = sum(1 for _ in open(path, encoding="utf-8"))
            print(f"  {path.name:50s}  {n:>10,} khatiyans")
        return

    out = Path(out_path)
    print(f"Exporting to {out} ...")
    if sort:
        print("  (sorting within each district: tahasil > village > khatiyan)")

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=HEADER,
            delimiter=separator,
            extrasaction="ignore",
            quoting=csv.QUOTE_MINIMAL,   # only quote when necessary → cleaner file
        )
        writer.writeheader()

        for record in iter_all_records(data_dir, district_filter, sort=sort):
            total_records += 1
            for row in flatten_ror(record):
                sl_no += 1
                row["sl_no"] = sl_no
                writer.writerow(row)
                total_rows += 1

            if total_records % 10_000 == 0:
                print(f"  ... {total_records:,} khatiyans -> {total_rows:,} rows", end="\r")

    print(f"\nDone: {total_records:,} khatiyans -> {total_rows:,} rows -> {out}")
    size_mb = out.stat().st_size / 1_048_576
    print(f"File size: {size_mb:.1f} MB")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export scraped Bhulekh data to CSV/TSV")
    parser.add_argument("--data-dir", default="bhulekh_data", metavar="DIR",
                        help="Directory containing district_*.db / district_*.ndjson files")
    parser.add_argument("--out",      default="bhulekh_all.csv", metavar="FILE",
                        help="Output CSV file path (default: bhulekh_all.csv)")
    parser.add_argument("--sep",      default="comma", choices=["comma", "tab", "pipe"],
                        help="Column separator (default: comma)")
    parser.add_argument("--districts", nargs="+", type=int, metavar="CODE",
                        help="Only export records from these district codes")
    parser.add_argument("--stats-only", action="store_true",
                        help="Just show record counts per district, don't export")
    parser.add_argument("--no-sort", action="store_true",
                        help="Skip per-district sorting (faster for very large exports)")
    args = parser.parse_args()

    sep_map = {"comma": ",", "tab": "\t", "pipe": "|"}
    sep = sep_map[args.sep]

    export(
        data_dir=args.data_dir,
        out_path=args.out,
        separator=sep,
        district_filter=args.districts,
        stats_only=args.stats_only,
        sort=not args.no_sort,
    )


if __name__ == "__main__":
    main()

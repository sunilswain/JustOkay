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


# Public export columns
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


def flatten_ror(record: dict, village_override: Optional[str] = None) -> List[dict]:
    """
    Convert one parsed RoR record into one flat dict per plot,
    using the canonical export column names.
    """
    ror_type = _get(record, "ror_type", default="type1")

    base = {
        "record_type":      ror_type,
        "district":         _get(record, "district"),
        "tahasil":          _get(record, "tahasil"),
        "village":          village_override or _get(record, "village"),
        "thana":            _get(record, "thana"),
        "thana_no":         _get(record, "thana_no"),
        "tahasil_no":       _get(record, "tehsil_no", "tahasil_no"),
        "khatiyan_no":      _get(record, "khatiyan_sl_no", "khatiyan_text", "sl_no"),
        "landlord_name":    _get(record, "landlord_name", "owner_name", "khewat_name"),
        "tenant_details":   _get(record, "tenant_name", "raiyat_name", "occupant_name"),
        "status":           _get(record, "status"),
        "water_tax":        _get(record, "water_tax"),
        "tax":              _get(record, "tax"),
        "cess":             _get(record, "ses"),
        "other_cess":       _get(record, "other_ses"),
        "total_tax":        _get(record, "total"),
        "special_case":     _get(record, "special_case"),
        "last_publish_date": _get(record, "last_publish_date"),
        "tax_date":         _get(record, "tax_date"),
    }

    plots: list = record.get("plots") or record.get("plot_details") or []

    if not plots:
        return [{**base,
                 "plot_no_or_chaka_no": "", "chaka_name": "", "land_type": "",
                 "chaka_included_plot": "", "non_chaka_plot": "",
                 "kisam_details": "",
                 "north_boundary": "", "east_boundary": "",
                 "south_boundary": "", "west_boundary": "",
                 "acre": "", "decimal": "", "hectare": "",
                 "non_chaka_land_type": "", "plot_remarks": ""}]

    rows = []
    for p in plots:
        if ror_type == "type2":
            plot_or_chaka = _get(p, "chaka", "plot_chaka")
        else:
            plot_or_chaka = _get(p, "plot_no", "plot_number", "plot_plot_no")

        rows.append({**base,
            "plot_no_or_chaka_no":  plot_or_chaka,
            "chaka_name":           _get(p, "chaka", "plot_chaka") if ror_type != "type2" else "",
            "land_type":            _get(p, "land_type", "plot_land_type"),
            "chaka_included_plot":  _get(p, "chaka_included_plot", "plot_chaka_included_plot"),
            "non_chaka_plot":       _get(p, "non_chaka_plot", "plot_non_chaka_plot"),
            "kisam_details":        _get(p, "kisam", "plot_kisam"),
            "north_boundary":       _get(p, "n_occu", "plot_n_occu", "north"),
            "east_boundary":        _get(p, "e_occu", "plot_e_occu", "east"),
            "south_boundary":       _get(p, "s_occu", "plot_s_occu", "south"),
            "west_boundary":        _get(p, "w_occu", "plot_w_occu", "west"),
            "acre":                 _get(p, "acre", "plot_acre", "area_acre"),
            "decimal":              _get(p, "decimil", "plot_decimil", "area_decimal"),
            "hectare":              _get(p, "hector", "plot_hector", "area_hector"),
            "non_chaka_land_type":  _get(p, "non_chaka_land_type", "plot_non_chaka_land_type"),
            "plot_remarks":         _get(p, "remarks", "plot_remarks"),
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

def _build_village_disambiguation(data_dir: str, district_filter: Optional[List[int]] = None) -> Dict[str, str]:
    """
    Pre-scan all records to detect villages sharing the same name within a tahasil
    but having different thana_no. For those, build a mapping:
      (tahasil, village, thana_no) -> disambiguated village name (e.g. "ଗୋବଣ୍ଡିଆ_1221")
    Returns empty dict if no disambiguation needed.
    """
    from collections import defaultdict

    # (tahasil, village) -> set of thana_no values seen
    village_thanas: Dict[tuple, set] = defaultdict(set)

    root = Path(data_dir)
    for db_path in sorted(root.glob("district_*.db")):
        try:
            con = sqlite3.connect(str(db_path))
            # Extract thana_no from data_json using json_extract (SQLite >= 3.38)
            # Fallback: sample one record per (tahasil, village) group
            try:
                for row in con.execute(
                    "SELECT tahasil, village, json_extract(data_json, '$.thana_no') "
                    "FROM khatiyans GROUP BY tahasil, village, json_extract(data_json, '$.thana_no')"
                ):
                    tahasil, village, thana_no = row[0], row[1], row[2]
                    if thana_no:
                        village_thanas[(tahasil, village)].add(str(thana_no))
            except Exception:
                # Older SQLite without json_extract: sample one per village
                for row in con.execute(
                    "SELECT tahasil, village, data_json FROM khatiyans "
                    "GROUP BY tahasil, village"
                ):
                    try:
                        data = json.loads(row[2])
                        thana_no = data.get("thana_no", "")
                        if thana_no:
                            village_thanas[(row[0], row[1])].add(thana_no)
                    except (json.JSONDecodeError, TypeError):
                        pass
            con.close()
        except Exception:
            pass

    # Build disambiguation map only for villages with multiple thana_no values
    disambiguation: Dict[str, str] = {}
    for (tahasil, village), thanas in village_thanas.items():
        if len(thanas) > 1:
            for thana_no in thanas:
                key = f"{tahasil}\x00{village}\x00{thana_no}"
                disambiguation[key] = f"{village}_{thana_no}"

    return disambiguation


def _safe_name(name: str) -> str:
    """Make a string safe for use as a filename/folder name."""
    name = name.strip()
    name = _re.sub(r'[<>:"/\\|?*]', '_', name)
    name = _re.sub(r'\s+', '_', name)
    return name or "unknown"


def export(
    data_dir: str,
    out_path: str,
    separator: str = ",",
    district_filter: Optional[List[int]] = None,
    stats_only: bool = False,
    sort: bool = True,
    single_file: bool = False,
) -> None:
    total_records = 0
    total_rows    = 0
    sl_no         = 0

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

    header = HEADER

    # Pre-scan for village name disambiguation
    print("  (pre-scanning for duplicate village names...)")
    disambiguation = _build_village_disambiguation(data_dir, district_filter)
    if disambiguation:
        print(f"  (found {len(disambiguation)} village+thana_no combos needing disambiguation)")

    if single_file:
        out = Path(out_path)
        print(f"Exporting to single file: {out} ...")
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=header, delimiter=separator,
                                    extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            for record in iter_all_records(data_dir, district_filter, sort=sort):
                total_records += 1
                village_name = None
                if disambiguation:
                    tahasil = _clean(str(record.get("tahasil", "") or ""))
                    village = _clean(str(record.get("village", "") or ""))
                    thana_no = _clean(str(record.get("thana_no", "") or ""))
                    key = f"{tahasil}\x00{village}\x00{thana_no}"
                    village_name = disambiguation.get(key)
                for row in flatten_ror(record, village_override=village_name):
                    sl_no += 1
                    row["sl_no"] = sl_no
                    writer.writerow(row)
                    total_rows += 1
                if total_records % 10_000 == 0:
                    print(f"  ... {total_records:,} khatiyans -> {total_rows:,} rows", end="\r")
        print(f"\nDone: {total_records:,} khatiyans -> {total_rows:,} rows -> {out}")
        print(f"File size: {out.stat().st_size / 1_048_576:.1f} MB")
        return

    # Default mode: per-village CSVs under tahasil folders
    out_dir = Path(out_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Exporting to {out_dir}/ (tahasil/village.csv) ...")

    from collections import defaultdict
    village_data: Dict[tuple, list] = defaultdict(list)

    for record in iter_all_records(data_dir, district_filter, sort=sort):
        total_records += 1
        tahasil = _clean(str(record.get("tahasil", "") or "")) or "unknown"
        village = _clean(str(record.get("village", "") or "")) or "unknown"

        village_name = None
        if disambiguation:
            thana_no = _clean(str(record.get("thana_no", "") or ""))
            key = f"{tahasil}\x00{village}\x00{thana_no}"
            village_name = disambiguation.get(key)

        for row in flatten_ror(record, village_override=village_name):
            sl_no += 1
            row["sl_no"] = sl_no
            total_rows += 1
            final_village = village_name or village
            village_data[(tahasil, final_village)].append(row)

        if total_records % 10_000 == 0:
            print(f"  ... {total_records:,} khatiyans loaded", end="\r")

    print(f"\n  Writing {len(village_data)} village files...")
    village_count = 0
    for (tahasil, village), rows in sorted(village_data.items()):
        tahasil_dir = out_dir / _safe_name(tahasil)
        tahasil_dir.mkdir(parents=True, exist_ok=True)
        csv_path = tahasil_dir / f"{_safe_name(village)}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=header, delimiter=separator,
                                    extrasaction="ignore", quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(rows)
        village_count += 1

    print(f"\nDone: {total_records:,} khatiyans -> {total_rows:,} rows")
    print(f"  {village_count} village CSVs in {out_dir}/")
    print(f"  Organized as: tahasil/village.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export scraped Bhulekh data to CSV/TSV")
    parser.add_argument("--data-dir", default="bhulekh_data", metavar="DIR",
                        help="Directory containing district_*.db / district_*.ndjson files")
    parser.add_argument("--out",      default="export", metavar="PATH",
                        help="Output directory for village CSVs (default: export/). "
                             "With --single-file, this is the output file path.")
    parser.add_argument("--sep",      default="comma", choices=["comma", "tab", "pipe"],
                        help="Column separator (default: comma)")
    parser.add_argument("--districts", nargs="+", type=int, metavar="CODE",
                        help="Only export records from these district codes")
    parser.add_argument("--stats-only", action="store_true",
                        help="Just show record counts per district, don't export")
    parser.add_argument("--no-sort", action="store_true",
                        help="Skip per-district sorting (faster for very large exports)")
    parser.add_argument("--single-file", action="store_true",
                        help="Export everything to a single CSV file instead of per-village")
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
        single_file=args.single_file,
    )


if __name__ == "__main__":
    main()

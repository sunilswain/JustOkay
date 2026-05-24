#!/usr/bin/env python3
"""
Test extraction for two specific khatiyans (Type-1 and Type-2) and export to CSV.

Usage:
    python test_specific_khatiyans.py
    python test_specific_khatiyans.py --headless
    python test_specific_khatiyans.py --output test_extraction_results.csv
"""

import argparse
import asyncio
import csv
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from bhulekh_scraper import (
    BhulekhScraper,
    SELECTOR_DISTRICT,
    SELECTOR_TAHASIL,
    SELECTOR_VILLAGE,
    SELECTOR_KHATIYAN,
)

# Khatiyan metadata fields (front page / Type-2 header)
KHATIYAN_FIELDS = [
    "ror_type",
    "district",
    "mouja",
    "tehsil",
    "thana",
    "tehsil_no",
    "thana_no",
    "landlord_name",
    "khatiyan_sl_no",
    "tenant_name",
    "status",
    "water_tax",
    "tax",
    "ses",
    "other_ses",
    "total",
    "description",
    "special_case",
    "last_publish_date",
    "tax_date",
    "form_no",
    "parichheda",
]

# Plot fields (one row per plot, prefixed for CSV clarity)
PLOT_FIELDS = [
    "plot_no",
    "chaka",
    "land_type",
    "kisam",
    "n_occu",
    "e_occu",
    "s_occu",
    "w_occu",
    "acre",
    "decimil",
    "hector",
    "remarks",
]

TEST_CASES = [
    {
        "label": "type2_ankulla_khatiyan1",
        "expected_ror_type": "type2",
        "district": "ଅନୁଗୋଳ",
        "tahasil": "ଅନୁଗୋଳ",
        "village": "ଆଙ୍କୁଲା",
        "khatiyan_text": "1",
    },
    {
        "label": "type1_krusnachakra_khatiyan2",
        "expected_ror_type": "type1",
        "district": "ଅନୁଗୋଳ",
        "tahasil": "ଅନୁଗୋଳ",
        "village": "କୃଷ୍ଣଚକ୍ର",
        "khatiyan_text": "2",
    },
]


def normalize_odia(text: str) -> str:
    """Normalize Odia text for fuzzy dropdown matching."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFC", text)
    return normalized.replace("\u0b3c", "")


def find_match(options: List[Dict[str, str]], target: str) -> Optional[Dict[str, str]]:
    """Find dropdown option by exact, normalized, or partial Odia text match."""
    target_norm = normalize_odia(target)
    for o in options:
        if o["text"] == target:
            return o
    for o in options:
        if normalize_odia(o["text"]) == target_norm:
            return o
    for o in options:
        opt_norm = normalize_odia(o["text"])
        if target_norm in opt_norm or opt_norm in target_norm:
            return o
    return None


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ""
    return str(value).strip()


def flatten_ror_to_rows(
    test_case: Dict[str, str],
    selection: Dict[str, str],
    ror_data: Dict[str, Any],
    error: str = "",
) -> List[Dict[str, str]]:
    """Build CSV rows: one row per plot, khatiyan metadata repeated."""
    base: Dict[str, str] = {
        "test_label": test_case["label"],
        "expected_ror_type": test_case["expected_ror_type"],
        "selected_district": selection.get("district_text", ""),
        "selected_tahasil": selection.get("tahasil_text", ""),
        "selected_village": selection.get("village_text", ""),
        "selected_khatiyan_text": selection.get("khatiyan_text", ""),
        "selected_khatiyan_value": selection.get("khatiyan_value", ""),
        "error": error,
    }
    for field in KHATIYAN_FIELDS:
        base[field] = clean(ror_data.get(field, ""))

    plots = ror_data.get("plots") or []
    rows: List[Dict[str, str]] = []

    if plots:
        for plot in plots:
            row = base.copy()
            for field in PLOT_FIELDS:
                row[f"plot_{field}"] = clean(plot.get(field, ""))
            rows.append(row)
    else:
        row = base.copy()
        for field in PLOT_FIELDS:
            row[f"plot_{field}"] = ""
        rows.append(row)

    return rows


def csv_columns() -> List[str]:
    meta = [
        "test_label",
        "expected_ror_type",
        "selected_district",
        "selected_tahasil",
        "selected_village",
        "selected_khatiyan_text",
        "selected_khatiyan_value",
        "error",
    ]
    plot_cols = [f"plot_{f}" for f in PLOT_FIELDS]
    return meta + KHATIYAN_FIELDS + plot_cols


async def navigate_to_khatiyan(
    scraper: BhulekhScraper,
    district: str,
    tahasil: str,
    village: str,
    khatiyan_text: str,
) -> Dict[str, str]:
    """Select district → tahasil → village → khatiyan; return resolved values."""
    district_opts = await scraper.get_dropdown_options(SELECTOR_DISTRICT)
    district_match = find_match(district_opts, district)
    if not district_match:
        available = [o["text"] for o in district_opts[:15]]
        raise ValueError(f"District {district!r} not found. Available: {available}")

    scraper._current_district_value = district_match["value"]
    scraper._current_district_text = district_match["text"]
    await scraper.select_dropdown(SELECTOR_DISTRICT, district_match["value"], wait_for_update=True)
    await scraper.select_search_type("Khatiyan")

    if not await scraper.wait_for_dropdown_populated(SELECTOR_TAHASIL, min_options=1):
        raise ValueError("Tahasil dropdown did not populate")

    tahasil_opts = await scraper.get_dropdown_options(SELECTOR_TAHASIL)
    tahasil_match = find_match(tahasil_opts, tahasil)
    if not tahasil_match:
        available = [o["text"] for o in tahasil_opts[:15]]
        raise ValueError(f"Tahasil {tahasil!r} not found. Available: {available}")

    await scraper.select_dropdown(SELECTOR_TAHASIL, tahasil_match["value"], wait_for_update=True)

    if not await scraper.wait_for_dropdown_populated(SELECTOR_VILLAGE, min_options=1):
        raise ValueError("Village dropdown did not populate")

    village_opts = await scraper.get_dropdown_options(SELECTOR_VILLAGE)
    village_match = find_match(village_opts, village)
    if not village_match:
        available = [o["text"] for o in village_opts[:15]]
        raise ValueError(f"Village {village!r} not found. Available: {available}")

    await scraper.select_dropdown(SELECTOR_VILLAGE, village_match["value"], wait_for_update=True)

    if not await scraper.wait_for_dropdown_populated(SELECTOR_KHATIYAN, min_options=1):
        raise ValueError("Khatiyan dropdown did not populate")

    khatiyan_opts = await scraper.get_dropdown_options(SELECTOR_KHATIYAN)
    khatiyan_match = find_match(khatiyan_opts, khatiyan_text)
    if not khatiyan_match:
        # Also try matching trimmed value or exact text digit
        for o in khatiyan_opts:
            if o["text"].strip() == khatiyan_text.strip() or o["value_trimmed"] == khatiyan_text.strip():
                khatiyan_match = o
                break
    if not khatiyan_match:
        available = [o["text"] for o in khatiyan_opts[:20]]
        raise ValueError(f"Khatiyan {khatiyan_text!r} not found. Available: {available}")

    return {
        "district_text": district_match["text"],
        "district_value": district_match["value"],
        "tahasil_text": tahasil_match["text"],
        "tahasil_value": tahasil_match["value"],
        "village_text": village_match["text"],
        "village_value": village_match["value"],
        "khatiyan_text": khatiyan_match["text"],
        "khatiyan_value": khatiyan_match["value"],
    }


async def extract_test_khatiyan(
    scraper: BhulekhScraper,
    test_case: Dict[str, str],
) -> Dict[str, Any]:
    """Navigate and extract one khatiyan; return result dict for CSV + summary."""
    result: Dict[str, Any] = {
        "test_case": test_case,
        "selection": {},
        "ror_data": {},
        "error": "",
        "rows": [],
    }

    try:
        await scraper.navigate_to_ror_page()
        selection = await navigate_to_khatiyan(
            scraper,
            test_case["district"],
            test_case["tahasil"],
            test_case["village"],
            test_case["khatiyan_text"],
        )
        result["selection"] = selection

        ok = await scraper.process_khatiyan(
            khatiyan_value=selection["khatiyan_value"],
            khatiyan_text=selection["khatiyan_text"],
            district=selection["district_text"],
            tahasil=selection["tahasil_text"],
            village=selection["village_text"],
            tahasil_value=selection["tahasil_value"],
            village_value=selection["village_value"],
        )
        if not ok:
            raise RuntimeError("process_khatiyan returned False")

        if not scraper.data_list:
            raise RuntimeError("No data captured after processing")

        ror_data = scraper.data_list[-1]
        result["ror_data"] = ror_data
        result["rows"] = flatten_ror_to_rows(test_case, selection, ror_data)

    except Exception as exc:
        result["error"] = str(exc)
        result["rows"] = flatten_ror_to_rows(test_case, result.get("selection", {}), {}, error=str(exc))

    return result


def print_summary(results: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 70)
    print("EXTRACTION SUMMARY")
    print("=" * 70)

    for res in results:
        tc = res["test_case"]
        sel = res.get("selection") or {}
        ror = res.get("ror_data") or {}
        error = res.get("error", "")

        print(f"\n[{tc['label']}]")
        print(f"  Location: {tc['district']} / {tc['tahasil']} / {tc['village']} / Khatiyan {tc['khatiyan_text']}")
        if error:
            print(f"  Status:   FAILED — {error}")
            continue

        detected = ror.get("ror_type", "unknown")
        expected = tc["expected_ror_type"]
        type_ok = "OK" if detected == expected else f"MISMATCH (expected {expected})"
        plots = ror.get("plots") or []
        filled = sum(1 for k, v in ror.items() if v and v != [] and k not in ("ror_type", "plots"))

        print(f"  Status:   OK")
        print(f"  RoR type: {detected} ({type_ok})")
        print(f"  Plots:    {len(plots)}")
        print(f"  Fields:   {filled} khatiyan metadata fields populated")
        if sel:
            print(f"  Resolved: district={sel.get('district_text')}, tahasil={sel.get('tahasil_text')}, "
                  f"village={sel.get('village_text')}, khatiyan={sel.get('khatiyan_text')}")
        if ror.get("tenant_name"):
            print(f"  Tenant:   {ror['tenant_name'][:80]}")
        if ror.get("landlord_name"):
            print(f"  Landlord: {ror['landlord_name'][:80]}")
        if plots:
            sample = plots[0]
            print(f"  Sample plot: no={sample.get('plot_no')}, acre={sample.get('acre')}, "
                  f"decimil={sample.get('decimil')}, kisam={sample.get('kisam', '')[:40]}")

    print("\n" + "=" * 70)


def write_csv(rows: List[Dict[str, str]], output_path: Path) -> None:
    columns = csv_columns()
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} row(s) to {output_path}")


async def run_tests(headless: bool, output_path: Path, delay_scale: float) -> int:
    scraper = BhulekhScraper(delay_scale=delay_scale)
    all_rows: List[Dict[str, str]] = []
    results: List[Dict[str, Any]] = []

    try:
        await scraper.init_browser(headless=headless)
        for i, test_case in enumerate(TEST_CASES):
            print(f"\n--- Test {i + 1}/{len(TEST_CASES)}: {test_case['label']} ---")
            result = await extract_test_khatiyan(scraper, test_case)
            results.append(result)
            all_rows.extend(result["rows"])
    finally:
        await scraper.cleanup()

    write_csv(all_rows, output_path)
    print_summary(results)

    failures = sum(1 for r in results if r.get("error"))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Test RoR extraction for two specific khatiyans")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (default: visible browser)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="test_extraction_results.csv",
        help="Output CSV path (default: test_extraction_results.csv)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use shorter delays (delay_scale=0.15)",
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    delay_scale = 0.15 if args.fast else 1.0

    print("Bhulekh specific khatiyan extraction test")
    print(f"  Headless: {args.headless}")
    print(f"  Output:   {output_path.resolve()}")

    return asyncio.run(run_tests(args.headless, output_path, delay_scale))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Unit test Form-20 extraction against a captured HTML fixture."""
import json
import sys
from pathlib import Path

from http_scraper import parse_ror_html, _is_form20_body

FIXTURE = Path("verification_output/test_capture/form20_achyutapali_khatiyan1.html")


def main() -> int:
    html = FIXTURE.read_text(encoding="utf-8")
    assert _is_form20_body(html), "fixture should detect as Form-20"
    data = parse_ror_html(html)
    assert data.get("ror_type") == "form20", f"expected form20, got {data.get('ror_type')}"
    assert data.get("mouja") == "ଅଚ୍ୟୁତପାଲି", data.get("mouja")
    assert data.get("landlord_name") == "ଓଡ଼ିଶା ସରକାର", data.get("landlord_name")
    assert data.get("tenant_name") == "ଗ୍ରାମବାସୀ", data.get("tenant_name")
    assert data.get("form_no") == "20", data.get("form_no")
    plots = data.get("plots") or []
    assert len(plots) >= 3, f"expected >=3 plots, got {len(plots)}"
    plot_nos = {p.get("plot_no") for p in plots}
    assert "3" in plot_nos and "4" in plot_nos and "2" in plot_nos, plot_nos
    out = Path("_test_form20_result.json")
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK form20 extraction -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

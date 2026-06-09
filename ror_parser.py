"""
Shared RoR HTML parser for HTTP scraper and Playwright verifier.

Extracts type1/type2/form20 layouts with full type2 chaka/non-chaka separation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from bs4 import BeautifulSoup

_PLOT_TABLE_IDS = ["gvRorBack", "gvRorBack2", "gvRorFrontBack", "gvplotdetail"]
_FORM20_PLOT_TABLE_IDS = [
    "gvRorSettBack", "gvSettPlot", "gvSettPlotDetail", "gvPlotSettle", "gvForm20Back",
    "gvRorBack", "gvRorBack2", "gvplotdetail",
]


def is_form20_body(body_text: str) -> bool:
    if re.search(r"ଫର୍ମ\s*ନଂ\s*[-–]?\s*20(?:\D|$)", body_text):
        return True
    if re.search(r"Form\s*(?:No\.?\s*)?20\b", body_text, re.I):
        return True
    settlement_markers = (
        "ସମୀକ୍ଷା ସେଟଲମେଣ୍ଟ",
        "Special Survey & Settlement",
        "Survey & Settlement Act",
        "Odisha Special Survey",
    )
    if any(m in body_text for m in settlement_markers):
        if re.search(r"ଫର୍ମ\s*ନଂ", body_text) and not re.search(
            r"ଫର୍ମ\s*ନଂ\s*[-–]?\s*99\b", body_text
        ):
            return True
    return False


def _cell_text(elem) -> str:
    if elem is None:
        return ""
    return (elem.get_text() or "").strip()


def _first_text(soup: BeautifulSoup, selectors: List[str]) -> str:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = _cell_text(el)
            if text:
                return text
    return ""


def _first_text_in_row(row, selectors: List[str]) -> str:
    for sel in selectors:
        el = row.select_one(sel)
        if el:
            text = _cell_text(el)
            if text:
                return text
    return ""


def parse_ror_html(html: str, *, village_info: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Parse RoR front + back page HTML into standard dict shape.
    Optional village_info fills district/tahasil/village when labels are missing.
    """
    soup = BeautifulSoup(html, "html.parser")
    data: Dict[str, Any] = {"plots": []}

    body_text = soup.get_text()
    is_form20 = is_form20_body(body_text)
    type2_markers = ["ପରିଶିଷ୍ଟ", "ଫର୍ମ ନଂ", "ପରିଚ୍ଛେଦ", "ଭୂ-ସ୍ୱାମୀ"]
    if is_form20:
        data["ror_type"] = "form20"
    elif any(m in body_text for m in type2_markers):
        data["ror_type"] = "type2"
    elif soup.find(id="gvfront"):
        data["ror_type"] = "type1"
    else:
        data["ror_type"] = "type2"

    front_selectors = {
        "mouja": ["#gvfront_ctl02_lblMouja", "#gvRorFront_ctl02_lblMouja", '[id*="lblMouja"]'],
        "tehsil": ["#gvfront_ctl02_lblTehsil", "#gvRorFront_ctl02_lblTehsil", '[id*="lblTehsil"]'],
        "thana": ["#gvfront_ctl02_lblThana", "#gvRorFront_ctl02_lblThana", '[id*="lblThana"]'],
        "tehsil_no": ["#gvfront_ctl02_lblTesilNo", "#gvRorFront_ctl02_lblTesilNo", '[id*="lblTesilNo"]'],
        "thana_no": ["#gvfront_ctl02_lblThanano", "#gvRorFront_ctl02_lblThanano", '[id*="lblThanano"]'],
        "district": ["#gvfront_ctl02_lblDist", "#gvRorFront_ctl02_lblDist", '[id*="lblDist"]'],
        "landlord_name": [
            "#gvfront_ctl02_lblLandlordName", "#gvRorFront_ctl02_lblLandlordName",
            '[id*="lblLandlordName"]', '[id*="lblBhuswami"]',
        ],
        "khatiyan_sl_no": [
            "#gvfront_ctl02_lblKhatiyanslNo", "#gvRorFront_ctl02_lblKhatiyanslNo",
            '[id*="lblKhatiyanslNo"]',
        ],
        "tenant_name": [
            "#gvfront_ctl02_lblName", "#gvRorFront_ctl02_lblName",
            '[id*="lblName"]', '[id*="lblRaiyat"]',
        ],
        "status": ["#gvfront_ctl02_lblStatua", "#gvRorFront_ctl02_lblStatua", '[id*="lblStatua"]'],
        "water_tax": ['[id*="lblWaterTax"]'],
        "tax": ['[id*="lblTax"]'],
        "ses": ['[id*="lblSes"]'],
        "other_ses": ['[id*="lblOtherses"]'],
        "total": ['[id*="lblTotal"]'],
        "description": ['[id*="lblDescription"]'],
        "special_case": ['[id*="lblSpecialCase"]'],
        "last_publish_date": ['[id*="lblLastPublishDate"]'],
        "tax_date": ['[id*="lblTaxDate"]'],
        "form_no": ['[id*="lblFormNo"]'],
        "parichheda": ['[id*="lblParichheda"]'],
        "parishista": ['[id*="lblParishista"]'],
    }

    for field, selectors in front_selectors.items():
        data[field] = _first_text(soup, selectors)

    num_pat = r"(\d+)" if is_form20 else r"(\S+)"
    if not data.get("form_no") or not data.get("parichheda") or (
        is_form20 and not data.get("parishista")
    ):
        m = re.search(rf"ଫର୍ମ\s*ନଂ\s*[-–]?\s*{num_pat}", body_text)
        if m and not data.get("form_no"):
            data["form_no"] = m.group(1).strip()
        m = re.search(rf"ପରିଚ୍ଛେଦ\s*[-–]?\s*{num_pat}", body_text)
        if m and not data.get("parichheda"):
            data["parichheda"] = m.group(1).strip()
        if is_form20:
            m = re.search(r"ପରିଶିଷ୍ଟ\s*[-–]?\s*(\S+)", body_text)
            if m and not data.get("parishista"):
                data["parishista"] = m.group(1).strip()

    if village_info:
        data.setdefault("district", village_info.get("district_name", ""))
        if not data.get("mouja"):
            data["mouja"] = village_info.get("village_name", "")
        if not data.get("tehsil"):
            data["tehsil"] = village_info.get("tahasil_name", "")

    plot_table_ids = _FORM20_PLOT_TABLE_IDS if is_form20 else _PLOT_TABLE_IDS
    table = None
    for tid in plot_table_ids:
        table = soup.find(id=tid)
        if table:
            break

    if table is None:
        best_score = 0
        id_pattern = re.compile(r"Ror|plot|Back|Sett|Form20", re.I) if is_form20 else re.compile(
            r"Ror|plot|Back", re.I
        )
        for t in soup.find_all("table", id=True):
            tid = t.get("id", "")
            if tid == "gvfront":
                continue
            if not id_pattern.search(tid):
                continue
            score = len(
                t.select('[id*="lblPlotNo"], [id*="lblPlotcni"], [id*="lblPlotci"], [id*="lblAcre"]')
            )
            if score > best_score:
                best_score = score
                table = t

    if table:
        for row in table.find_all("tr"):
            plot_no = ""
            for sel in (
                'a[id*="lblPlotcni"]', 'span[id*="lblPlotcni"]',
                'a[id*="lblPlotNo"]', 'span[id*="lblPlotNo"]',
                'a[id*="lblPlotci"]', 'span[id*="lblPlotci"]',
            ):
                el = row.select_one(sel)
                if el:
                    val = _cell_text(el)
                    if val and re.search(r"\d", val):
                        plot_no = val
                        break
            if not plot_no and is_form20:
                tds = row.find_all("td")
                if tds:
                    first = _cell_text(tds[0])
                    if re.fullmatch(r"\d+", first.strip()):
                        plot_no = first.strip()
            if not plot_no:
                continue

            plot: Dict[str, str] = {"plot_no": plot_no}

            chaka_name = _first_text_in_row(
                row, ['span[id*="lblchaka"]', 'span[id*="Chaka"]', '[id*="lblchaka"]'],
            )
            chaka_included_plot = _first_text_in_row(
                row, ['a[id*="lblPlotci"]', 'span[id*="lblPlotci"]'],
            )
            non_chaka_plot = _first_text_in_row(
                row, ['a[id*="lblPlotcni"]', 'span[id*="lblPlotcni"]'],
            )
            non_chaka_land_type = _first_text_in_row(
                row, ['span[id*="lblCNItype"]', '[id*="CNItype"]'],
            )

            if not chaka_name and chaka_included_plot:
                m = re.match(r"^[\d/]+\s+(.+)$", chaka_included_plot)
                chaka_name = m.group(1).strip() if m else ""

            plot["chaka"] = chaka_name
            plot["chaka_included_plot"] = chaka_included_plot
            plot["non_chaka_plot"] = non_chaka_plot
            plot["non_chaka_land_type"] = non_chaka_land_type

            plot_fields = {
                "land_type": ['span[id*="lbllType"]', '[id*="lbllType"]'],
                "kisam": ['span[id*="lblKisama"]', '[id*="Kisam"]'],
                "n_occu": ['span[id*="lbln_occu"]', '[id*="n_occu"]'],
                "e_occu": ['span[id*="lble_occu"]', '[id*="e_occu"]'],
                "s_occu": ['span[id*="lbls_occu"]', '[id*="s_occu"]'],
                "w_occu": ['span[id*="lblw_occu"]', '[id*="w_occu"]'],
                "acre": ['span[id*="lblAcre"]', '[id*="Acre"]'],
                "decimil": ['span[id*="lblDecimil"]', '[id*="Decimil"]'],
                "hector": ['span[id*="lblHector"]', '[id*="Hector"]'],
                "remarks": ['span[id*="lblPlotRemarks"]', '[id*="Remark"]'],
            }
            for field, selectors in plot_fields.items():
                plot[field] = _first_text_in_row(row, selectors)

            data["plots"].append(plot)

    return data

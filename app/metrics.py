"""Metric lookups over full passdown text.

The risk register stores a 600-character summary per section, which is enough for
risk triage but drops the numbers buried in the passdown's wide HTML tables
(LIPAS attainment, VPO miss counts, UTZ). Those full rows live in
``source_documents``; this module reads them so the assistant can answer
"what is the LIPAS for class test?" with the actual figure instead of prose.

Everything here is extraction, never inference: if a number is not present in the
source text, the caller is told so rather than given an estimate.
"""

from __future__ import annotations

import re
from typing import Any

from .db import query

# Passdown tables are flattened to " | "-separated cells by the HTML stripper.
CELL_SPLIT = re.compile(r"\s*\|\s*")
PERCENT_RE = re.compile(r"^\d{1,3}(?:\.\d+)?%?$")


def _rows(body: str) -> list[list[str]]:
    """Split flattened table text into rows of cells.

    ``_strip_html`` already emits one line per table row with " | " between
    cells, so this only needs to split on the separator.
    """
    out: list[list[str]] = []
    for line in body.split("\n"):
        if "|" not in line:
            continue
        cells = [c.strip() for c in CELL_SPLIT.split(line.strip().strip("|"))]
        if len(cells) > 1:
            out.append(cells)
    return out


def _docs(source: str | None = None) -> list[dict[str, Any]]:
    if source:
        return query("SELECT * FROM source_documents WHERE source = ?", (source,))
    return query("SELECT * FROM source_documents")


def _find_header(rows: list[list[str]], *needles: str) -> tuple[int, list[str]] | None:
    for i, cells in enumerate(rows):
        low = [c.lower() for c in cells]
        if all(any(n in c for c in low) for n in needles):
            return i, cells
    return None


def _pct(value: str) -> float | None:
    """Parse a LIPAS cell to a number, or None if it isn't a figure."""
    v = value.strip().rstrip("%").strip()
    try:
        return float(v)
    except ValueError:
        return None


def lipas_report(section_hint: str = "") -> dict[str, Any]:
    """Extract LIPAS attainment columns from passdown tables.

    Returns per-document entries with the Grand Total row when present, plus any
    product rows carrying a LIPAS value.
    """
    results: list[dict[str, Any]] = []
    for doc in _docs("MMS Passdown"):
        title = doc["title"]
        if section_hint and section_hint.lower() not in title.lower():
            continue
        body = doc["body"]
        if "lipas" not in body.lower():
            continue

        rows = _rows(body)
        hdr = _find_header(rows, "lipas")
        if not hdr:
            continue
        hdr_idx, header = hdr
        lipas_cols = [i for i, c in enumerate(header) if "lipas" in c.lower()]
        if not lipas_cols:
            continue

        total: dict[str, str] = {}
        products: list[dict[str, str]] = []
        for cells in rows[hdr_idx + 1:]:
            label = cells[0].strip()
            if not label:
                continue
            # Nested UTZ/downtime tables sit inside the product table but have
            # far fewer columns; skip them so their percentages aren't read as
            # product attainment.
            if len(cells) < len(header) - 1:
                continue
            values = {
                header[i].strip(): cells[i].strip()
                for i in lipas_cols
                if i < len(cells) and cells[i].strip()
            }
            if not values:
                continue
            if "grand total" in label.lower() or label.lower().startswith("total"):
                total = values
            else:
                products.append({"product": label, **values})

        results.append({
            "section": title,
            "grand_total": total,
            "products": products,
            "updated_by": doc["updated_by"],
            "fetched_at": doc["fetched_at"],
        })
    return {"sections": results}


VPO_MISS_RE = re.compile(
    r"(?im)^(.*\b(?:vpo|lipas)\b.*\b(?:miss|missed|shortfall|not\s*meet)\b.*)$"
)

# Under a "VPO ... MISS" heading the passdown lists the affected queue as
#   QUEUE LOT TYPE <X>
#   Vpo product code name | Qual
#   <PRODUCT> | <qty>
# These two lines are structural, not data.
_VPO_STRUCTURAL_RE = re.compile(r"(?i)^(queue lot type|vpo product code name)\b")
_VPO_ENTRY_RE = re.compile(r"^(.*?)\s*\|\s*(\d+)$")
# Where the block under a heading ends.
_VPO_BLOCK_END_RE = re.compile(r"(?i)\n\s*\n|HDBI Execution|HBI UTZ|Link to ")


def _vpo_miss_entries(body: str, match_end: int) -> list[dict[str, Any]]:
    """Products listed under a VPO miss heading, with their quantities.

    Returns [] when the heading is only an unfilled template, which is the
    difference between "no misses" and "the table wasn't completed".
    """
    tail = body[match_end: match_end + 500]
    block = _VPO_BLOCK_END_RE.split(tail)[0]
    entries: list[dict[str, Any]] = []
    for raw in block.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip(" |")
        if not line or _VPO_STRUCTURAL_RE.match(line):
            continue
        m = _VPO_ENTRY_RE.match(line)
        if m and m.group(1).strip():
            entries.append({"product": m.group(1).strip(), "qty": int(m.group(2))})
    return entries


def vpo_report() -> dict[str, Any]:
    """Collect VPO miss signals and the single/double digit completion columns.

    A heading such as "VPO HDBI LIPAS MISS" is only meaningful together with the
    queue listed beneath it. Headings with no entries are reported separately as
    ``templates`` so an unfilled table is never mistaken for a clean shift.
    """
    misses: list[dict[str, Any]] = []
    templates: list[dict[str, str]] = []
    execution: list[dict[str, Any]] = []

    for doc in _docs("MMS Passdown"):
        body = doc["body"]
        low = body.lower()
        if "vpo" not in low and "single digit" not in low:
            continue

        for mo in VPO_MISS_RE.finditer(body):
            clean = re.sub(r"\s*\|\s*", " | ",
                           re.sub(r"\s+", " ", mo.group(1))).strip(" |")
            if len(clean) <= 3:
                continue
            entries = _vpo_miss_entries(body, mo.end())
            if entries:
                misses.append({
                    "section": doc["title"],
                    "detail": clean[:300],
                    "products": entries,
                    "total_qty": sum(e["qty"] for e in entries),
                })
            else:
                templates.append({"section": doc["title"], "detail": clean[:300]})

        rows = _rows(body)
        hdr = _find_header(rows, "scheduled", "completed")
        if hdr:
            hdr_idx, header = hdr
            for cells in rows[hdr_idx + 1:]:
                label = cells[0].strip()
                if not label or len(cells) < 3:
                    continue
                pairs = {
                    header[i].strip(): cells[i].strip()
                    for i in range(1, min(len(cells), len(header)))
                    if cells[i].strip()
                }
                if pairs:
                    execution.append({
                        "section": doc["title"], "row": label, "values": pairs,
                    })

    return {
        "misses": misses,
        "templates": templates,
        "total_miss_qty": sum(m["total_qty"] for m in misses),
        "execution": execution[:40],
    }


def find_metric(term: str) -> list[dict[str, str]]:
    """Generic fallback: return source lines mentioning an arbitrary term."""
    hits: list[dict[str, str]] = []
    needle = term.lower()
    for doc in _docs():
        for line in doc["body"].split("\n"):
            if needle in line.lower():
                clean = re.sub(r"\s+", " ", line).strip(" |")
                if len(clean) > 2:
                    hits.append({"section": doc["title"], "line": clean[:300]})
    return hits[:25]


# --------------------------------------------------------- focus topics
# The shift review is driven by a fixed agenda. Each entry declares which
# passdown section to read and how to pull its headline out, so the summary and
# the register can both key off the same definitions.

def _doc_like(*fragments: str) -> dict[str, Any] | None:
    for doc in _docs("MMS Passdown"):
        title = doc["title"].lower()
        if any(f.lower() in title for f in fragments):
            return doc
    return None


def usdt_by_area() -> dict[str, Any]:
    """USDT rate plus the per-area high-USDT handler/collateral issues."""
    doc = _doc_like("MEOS UPDATE")
    if not doc:
        return {"available": False, "rates": [], "areas": []}

    rows = _rows(doc["body"])
    rates: list[dict[str, str]] = []
    areas: list[dict[str, str]] = []

    rate_hdr = _find_header(rows, "usdt %")
    if rate_hdr:
        idx, header = rate_hdr
        for cells in rows[idx + 1:]:
            if len(cells) < 2 or not cells[0]:
                continue
            if _pct(cells[1]) is None:
                break
            rates.append({
                "period": cells[0],
                **{header[i].strip(): cells[i].strip()
                   for i in range(1, min(len(cells), len(header))) if cells[i].strip()},
            })

    area_hdr = _find_header(rows, "rea", "remark")  # source prints "A REA"
    if area_hdr:
        idx, _ = area_hdr
        for cells in rows[idx + 1:]:
            if len(cells) < 2 or not cells[0]:
                continue
            detail = cells[1].strip()
            if detail.upper() in ("N/A", ""):
                continue
            areas.append({
                "area": re.sub(r"\s+", " ", cells[0]).strip(),
                "detail": detail[:400],
                "help_needed": (cells[2].strip()[:300] if len(cells) > 2
                                and cells[2].strip().upper() != "N/A" else ""),
            })

    return {"available": bool(rates or areas), "rates": rates, "areas": areas,
            "section": doc["title"]}


def hot_swap_and_spares() -> dict[str, Any]:
    """Hot swap trend by work week and any items waiting for spare."""
    doc = _doc_like("Collaterals Update")
    if not doc:
        return {"available": False, "trend": [], "waiting": []}

    body = doc["body"]
    trend: list[dict[str, str]] = []
    for ww, detail in re.findall(r"(WW\d+\.\d+)\s*-\s*(.*?)(?=WW\d+\.\d+\s*-|Waiting for Spare|$)",
                                 body, re.S):
        clean = re.sub(r"\s+", " ", detail).strip(" |-")
        if clean:
            trend.append({"week": ww, "detail": clean[:300]})

    waiting: list[dict[str, str]] = []
    tail = body.split("Waiting for Spare", 1)
    if len(tail) > 1:
        for cells in _rows(tail[1]):
            item = cells[0].strip().strip(":").strip()
            status = cells[1].strip() if len(cells) > 1 else ""
            # Skip the repeated header row ("Waiting for Spare | Status").
            if not item or item.lower().startswith("waiting") or status.lower() == "status":
                continue
            waiting.append({"item": item, "status": status})

    return {"available": bool(trend or waiting), "trend": trend,
            "waiting": waiting, "section": doc["title"]}


def conversion_status() -> dict[str, Any]:
    """Conversion success rate per module, with the stated target."""
    doc = _doc_like("CONVERSION STATUS")
    if not doc:
        return {"available": False, "modules": [], "target": ""}

    body = re.sub(r"\s+", " ", doc["body"])
    target_m = re.search(r"Score\s*:?\s*(>?\s*\d+(?:\.\d+)?%)", body, re.I)
    modules = [
        {"module": name, "rate": rate}
        for name, rate in re.findall(r"([A-Z][A-Z0-9]{1,6})\s*:\s*(\d+(?:\.\d+)?%)", body)
    ]
    return {
        "available": bool(modules),
        "modules": modules,
        "target": (target_m.group(1).replace(" ", "") if target_m else ""),
        "section": doc["title"],
    }


def hdmx_dt_trend() -> dict[str, Any]:
    """HDMX downtime trend split into product performance and chiller status."""
    doc = _doc_like("HDMX DT TREND")
    if not doc:
        return {"available": False, "products": [], "chillers": []}

    body = re.sub(r"\s+", " ", doc["body"])
    prod_part, _, chill_part = body.partition("Chiller Performance")
    prod_part = re.sub(r"(?i)^.*?N-7 Product Performance", "", prod_part).strip()

    products = [
        {"product": name.strip(), "detail": re.sub(r"\s+", " ", det).strip(" -,")[:200]}
        for name, det in re.findall(
            r"([A-Z][A-Z0-9]{3,7})\s*-\s*(.*?)(?=[A-Z][A-Z0-9]{3,7}\s*-|$)", prod_part, re.S)
    ]
    chillers = [
        {"unit": unit.strip(), "detail": re.sub(r"\s+", " ", det).strip(" -,")[:250]}
        for unit, det in re.findall(
            r"(HDMX#\d+)\s*-\s*(.*?)(?=HDMX#\d+\s*-|$)", chill_part, re.S)
    ]
    return {"available": bool(products or chillers), "products": products,
            "chillers": chillers, "section": doc["title"]}


def focus_report() -> dict[str, Any]:
    """The full shift-review agenda in one payload."""
    return {
        "lipas": lipas_report(),
        "vpo": vpo_report(),
        "usdt": usdt_by_area(),
        "hot_swap": hot_swap_and_spares(),
        "conversion": conversion_status(),
        "hdmx_dt": hdmx_dt_trend(),
    }


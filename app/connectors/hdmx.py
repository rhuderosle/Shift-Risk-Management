"""Connector for the HDMX file share (structured CSV, no scraping).

Reads the same CSVs that back the "HDMX Performance KPI" Power BI report, so
the numbers reported here match that dashboard:

    <share>\\hsd\\Summary_Overall.csv          hot swap (H/S), UDT%, CS score
    <share>\\hsd\\cmms_waiting_spare.csv       open waiting-for-spare events
    <share>\\SystemUtilizationMonitor\\...     per-tool utilisation -> USDT

Unlike the MMS connector, nothing here is inferred from prose: every figure is
read from a named column. Values are therefore *verbatim* and safe to quote in
a passdown. Where a file is missing we say so explicitly rather than reporting
a zero, because a silent zero reads as "good" when it actually means "unknown".

Access needs a Windows account with rights to the share, which is why this runs
on an internal machine.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..config import settings

log = logging.getLogger(__name__)

SUMMARY_REL = os.path.join("hsd", "Summary_Overall.csv")
WAITING_SPARE_REL = os.path.join("hsd", "cmms_waiting_spare.csv")
UTIL_DIR_REL = os.path.join("SystemUtilizationMonitor", "Output", "Archeived")
UTIL_LATEST = "Latest.csv"

# Work-week archive files are named like "202636.csv" (YYYYWW).
WW_FILE_RE = re.compile(r"^(\d{6})\.csv$", re.I)


class ShareUnavailable(RuntimeError):
    """The share could not be read (offline, VPN down, or no permission)."""


def _num(value: Any) -> float | None:
    """Parse a CSV cell to float, tolerating blanks, '%' and stray spaces."""
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _read_csv(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        raise ShareUnavailable(f"not found: {path}")
    # utf-8-sig: these files are Windows-generated and often carry a BOM, which
    # would otherwise corrupt the first column name.
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        return list(csv.DictReader(fh))


def share_root() -> str:
    return settings.hdmx_share.rstrip("\\/")


def _p(*parts: str) -> str:
    return os.path.join(share_root(), *parts)


def share_status() -> dict[str, Any]:
    """Cheap reachability probe used by the dashboard and diagnostics."""
    root = share_root()
    files = {
        "summary": _p(SUMMARY_REL),
        "waiting_spare": _p(WAITING_SPARE_REL),
        "utilisation": _p(UTIL_DIR_REL, UTIL_LATEST),
    }
    present = {k: os.path.exists(v) for k, v in files.items()}
    return {
        "share": root,
        "reachable": any(present.values()),
        "files": present,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# Hot swap / UDT / waiting-spare summary
# --------------------------------------------------------------------------

def summary_rows() -> list[dict[str, str]]:
    return _read_csv(_p(SUMMARY_REL))


def hot_swap_trend(weeks: int | None = None) -> dict[str, Any]:
    """Hot swap (H/S) and UDT% by work week, and the per-product detail.

    ``Day`` is formatted ``YYYYWW.S`` (e.g. 202636.4 = WW36 shift 4), so the
    work week is the integer part and shifts sort naturally within it.
    """
    weeks = weeks or settings.hdmx_trend_weeks
    try:
        rows = summary_rows()
    except ShareUnavailable as exc:
        return {"available": False, "reason": str(exc), "weeks": [], "products": []}

    by_week: dict[str, dict[str, float]] = {}
    for r in rows:
        ww = (r.get("WW") or "").strip()
        if not ww:
            continue
        acc = by_week.setdefault(ww, {"hot_swap": 0.0, "udt_sum": 0.0, "udt_n": 0.0,
                                      "waiting_spare_min": 0.0, "cs_sum": 0.0, "cs_n": 0.0})
        hs = _num(r.get("H/S"))
        if hs is not None:
            acc["hot_swap"] += hs
        udt = _num(r.get("UDT%"))
        if udt is not None:
            acc["udt_sum"] += udt
            acc["udt_n"] += 1
        ws = _num(r.get("Waiting_Spare(m)"))
        if ws is not None:
            acc["waiting_spare_min"] += ws
        cs = _num(r.get("CS_Score%"))
        if cs is not None:
            acc["cs_sum"] += cs
            acc["cs_n"] += 1

    weeks_out = []
    for ww in sorted(by_week)[-weeks:]:
        a = by_week[ww]
        weeks_out.append({
            "week": ww,
            "hot_swap": round(a["hot_swap"], 1),
            "udt_pct": round(a["udt_sum"] / a["udt_n"], 2) if a["udt_n"] else None,
            "cs_score_pct": round(a["cs_sum"] / a["cs_n"], 1) if a["cs_n"] else None,
            "waiting_spare_hrs": round(a["waiting_spare_min"] / 60, 1),
        })

    # Per-product detail for the most recent week only: that is what the shift
    # review actually discusses.
    latest = max(by_week) if by_week else ""
    prod: dict[str, dict[str, float]] = {}
    for r in rows:
        if (r.get("WW") or "").strip() != latest:
            continue
        name = (r.get("Prod") or "").strip()
        if not name:
            continue
        acc = prod.setdefault(name, {"hot_swap": 0.0, "udt_sum": 0.0, "udt_n": 0.0,
                                     "waiting_spare_min": 0.0})
        hs = _num(r.get("H/S"))
        if hs is not None:
            acc["hot_swap"] += hs
        udt = _num(r.get("UDT%"))
        if udt is not None:
            acc["udt_sum"] += udt
            acc["udt_n"] += 1
        ws = _num(r.get("Waiting_Spare(m)"))
        if ws is not None:
            acc["waiting_spare_min"] += ws

    products = sorted(
        (
            {
                "product": k,
                "hot_swap": round(v["hot_swap"], 1),
                "udt_pct": round(v["udt_sum"] / v["udt_n"], 2) if v["udt_n"] else None,
                "waiting_spare_hrs": round(v["waiting_spare_min"] / 60, 1),
            }
            for k, v in prod.items()
        ),
        key=lambda d: (-(d["hot_swap"] or 0), -(d["udt_pct"] or 0)),
    )

    return {
        "available": bool(weeks_out),
        "weeks": weeks_out,
        "latest_week": latest,
        "products": products,
        "source": _p(SUMMARY_REL),
    }


def waiting_for_spare() -> dict[str, Any]:
    """Open CMMS events currently blocked waiting for a spare part."""
    try:
        rows = _read_csv(_p(WAITING_SPARE_REL))
    except ShareUnavailable as exc:
        return {"available": False, "reason": str(exc), "items": [], "total_hours": 0.0}

    items = []
    total = 0.0
    for r in rows:
        mins = _num(r.get("Waiting_Spare(m)")) or 0.0
        total += mins
        problem = re.sub(r"\s+", " ", (r.get("PROBLEM") or "")).strip()
        items.append({
            "report_id": (r.get("REPORT_ID") or "").strip(),
            "tool": (r.get("Tool") or "").strip(),
            "cell": (r.get("Cell") or "").strip(),
            "product": (r.get("PRODUCT") or "").strip(),
            "collateral": (r.get("COLLATERALNAME") or "").strip(),
            "problem": problem[:300],
            "worklog": re.sub(r"\s+", " ", (r.get("WORKLOG") or "")).strip()[:300],
            "location": (r.get("CURRENT_LOCATION") or "").strip(),
            "waiting_hours": round(mins / 60, 1),
            "created": (r.get("PG_CREATE_DATE") or "").strip(),
            "shift_key": (r.get("ShiftKey") or "").strip(),
        })

    items.sort(key=lambda d: -d["waiting_hours"])
    return {
        "available": bool(items),
        "items": items,
        "count": len(items),
        "total_hours": round(total / 60, 1),
        "tools_affected": len({i["tool"] for i in items if i["tool"]}),
        "source": _p(WAITING_SPARE_REL),
    }


# --------------------------------------------------------------------------
# Utilisation / USDT
# --------------------------------------------------------------------------

def _util_file(week: str | None = None) -> str:
    if week:
        return _p(UTIL_DIR_REL, f"{week}.csv")
    return _p(UTIL_DIR_REL, UTIL_LATEST)


def usdt(week: str | None = None, group_by: str = "Site", site: str | None = None) -> dict[str, Any]:
    """Unscheduled downtime from the utilisation monitor.

    USDT% = DOWN / Total. ``group_by`` defaults to ``Site`` because
    PARENT_DEPT_NAME is blank on roughly 60% of rows and would silently drop
    most of the fleet from the breakdown. Pass ``site`` (e.g. ``"PG12"``) to
    restrict the overall figures and breakdown to that Site only, rather than
    the whole fleet.
    """
    path = _util_file(week)
    try:
        rows = _read_csv(path)
    except ShareUnavailable as exc:
        return {"available": False, "reason": str(exc), "overall": None, "areas": []}

    if group_by not in (rows[0].keys() if rows else ()):
        group_by = "Site"
    if site:
        rows = [r for r in rows if (r.get("Site") or "").strip().upper() == site.upper()]

    down = total = prod = sdt = 0.0
    groups: dict[str, dict[str, float]] = {}
    blank_group = 0

    for r in rows:
        d = _num(r.get("DOWN")) or 0.0
        t = _num(r.get("Total")) or 0.0
        p = _num(r.get("PRODUCTION")) or 0.0
        s = _num(r.get("SDT")) or 0.0
        down, total, prod, sdt = down + d, total + t, prod + p, sdt + s

        key = (r.get(group_by) or "").strip()
        if not key:
            blank_group += 1
            key = "(unassigned)"
        g = groups.setdefault(key, {"down": 0.0, "total": 0.0, "tools": set()})  # type: ignore[dict-item]
        g["down"] += d
        g["total"] += t
        tool = (r.get("Tool") or "").strip()
        if tool:
            g["tools"].add(tool)  # type: ignore[union-attr]

    areas = sorted(
        (
            {
                "area": k,
                "usdt_pct": round(v["down"] / v["total"] * 100, 2) if v["total"] else None,
                "down_hours": round(v["down"] / 3600, 1),
                "tools": len(v["tools"]),  # type: ignore[arg-type]
            }
            for k, v in groups.items()
        ),
        key=lambda d: -(d["usdt_pct"] or 0),
    )

    return {
        "available": total > 0,
        "overall": {
            "usdt_pct": round(down / total * 100, 2) if total else None,
            "sdt_pct": round(sdt / total * 100, 2) if total else None,
            "production_pct": round(prod / total * 100, 2) if total else None,
        },
        "areas": areas,
        "grouped_by": group_by,
        "site": site or "",
        "unassigned_rows": blank_group,
        "rows": len(rows),
        "week": week or "latest",
        "source": path,
    }


def available_weeks() -> list[str]:
    """Work weeks present in the utilisation archive, oldest first."""
    d = _p(UTIL_DIR_REL)
    if not os.path.isdir(d):
        return []
    weeks = []
    for name in os.listdir(d):
        m = WW_FILE_RE.match(name)
        if m:
            weeks.append(m.group(1))
    return sorted(weeks)


def usdt_trend(weeks: int | None = None, site: str | None = None) -> dict[str, Any]:
    """USDT% per work week, for trend rather than a single snapshot."""
    weeks = weeks or settings.hdmx_trend_weeks
    picked = available_weeks()[-weeks:]
    points = []
    for w in picked:
        r = usdt(week=w, site=site)
        if r["available"]:
            points.append({"week": w, "usdt_pct": r["overall"]["usdt_pct"]})
    return {"available": bool(points), "points": points}


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------

def focus_report() -> dict[str, Any]:
    """Everything this connector supplies, in one payload."""
    return {
        "status": share_status(),
        "usdt": usdt(),
        "usdt_trend": usdt_trend(),
        "hot_swap": hot_swap_trend(),
        "waiting_spare": waiting_for_spare(),
    }


def sync() -> dict[str, Any]:
    """Probe the share and report what is readable.

    Deliberately read-only: these CSVs are already authoritative, so copying
    them into the local database would only create a second, staler copy.
    """
    status = share_status()
    if not status["reachable"]:
        raise ShareUnavailable(
            f"HDMX share not reachable at {status['share']}. "
            "Check VPN/network and that you have permission to the share."
        )

    hs = hot_swap_trend()
    ws = waiting_for_spare()
    ut = usdt()
    result = {
        "share": status["share"],
        "hot_swap_weeks": len(hs.get("weeks", [])),
        "waiting_spare_items": ws.get("count", 0),
        "waiting_spare_hours": ws.get("total_hours", 0.0),
        "usdt_pct": (ut.get("overall") or {}).get("usdt_pct"),
        "utilisation_rows": ut.get("rows", 0),
        "synced_at": datetime.now().isoformat(timespec="seconds"),
    }
    log.info("HDMX sync: %s", result)
    return result

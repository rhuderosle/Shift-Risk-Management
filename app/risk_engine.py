"""Deterministic risk scoring, shift-window resolution and aggregation."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import query

# Shift definitions: name -> (start_hour, duration_hours) in local time.
# Four crews on a 12-hour 7-to-7 rotation: 1 & 3 are day crews, 2 & 4 are night crews.
SHIFTS: dict[str, tuple[int, int]] = {"1": (7, 12), "2": (19, 12), "3": (7, 12), "4": (19, 12)}

BAND_ORDER = ["Critical", "High", "Medium", "Low"]


def risk_score(severity: int, likelihood: int, downtime_minutes: float = 0.0,
               impact_units: float = 0.0, status: str = "open") -> float:
    """0-100 composite score. Severity x likelihood dominates; impact adds weight."""
    base = (severity * likelihood) / 25.0 * 70.0
    downtime_factor = min(downtime_minutes / 240.0, 1.0) * 20.0
    units_factor = min(impact_units / 5000.0, 1.0) * 10.0
    score = base + downtime_factor + units_factor
    if status == "mitigating":
        score *= 0.85
    elif status == "closed":
        score *= 0.25
    return round(min(score, 100.0), 1)


def band(score: float) -> str:
    if score >= 75:
        return "Critical"
    if score >= 50:
        return "High"
    if score >= 25:
        return "Medium"
    return "Low"


def current_shift(now: datetime | None = None) -> str:
    now = now or datetime.now()
    hour = now.hour
    for name, (start, dur) in SHIFTS.items():
        end = (start + dur) % 24
        if start < end:
            if start <= hour < end:
                return name
        elif hour >= start or hour < end:
            return name
    return "1"


def shift_window(shift: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Most recent completed-or-current window for the given shift, as UTC datetimes."""
    now = now or datetime.now()
    start_hour, dur = SHIFTS.get(shift, SHIFTS["1"])
    start = now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    end = start + timedelta(hours=dur)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def enrich(row: dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    r["score"] = risk_score(
        r["severity"], r["likelihood"], r.get("downtime_minutes", 0),
        r.get("impact_units", 0), r.get("status", "open"),
    )
    r["band"] = band(r["score"])
    r["focus"] = focus_topics(r)
    return r


# The standing shift-review agenda. A risk touching one of these is surfaced
# first in the register, because these are the topics the review is run against.
FOCUS_PATTERNS: dict[str, re.Pattern[str]] = {
    "LIPAS": re.compile(r"\blipas\b|\bvpo\b", re.I),
    "USDT": re.compile(r"\busdt\b|\bunscheduled\s+down", re.I),
    "Hot swap": re.compile(r"\bhot\s*swap\b|\bswap\s+(?:spare|tester|ap)\b", re.I),
    "Waiting for spare": re.compile(r"waiting\s+for\s+spare|\bspare\s+(?:shortage|pending)"
                                    r"|\bshortage\b", re.I),
    "Conversion": re.compile(r"\bconversion\b", re.I),
    "HDMX DT": re.compile(r"\bhdmx\b.*\b(?:dt|downtime|chiller)\b"
                          r"|\b(?:dt|downtime|chiller)\b.*\bhdmx\b|\bchiller\b", re.I),
}

FOCUS_ORDER = list(FOCUS_PATTERNS)


def focus_topics(row: dict[str, Any]) -> list[str]:
    """Which shift-review topics a risk relates to, by its own text."""
    blob = " ".join(str(row.get(k) or "") for k in ("title", "description", "area", "action"))
    return [name for name, rx in FOCUS_PATTERNS.items() if rx.search(blob)]


def fetch_risks(include_closed: bool = False, focus_first: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM risks"
    if not include_closed:
        sql += " WHERE status != 'closed'"
    rows = [enrich(r) for r in query(sql + " ORDER BY updated_at DESC")]
    if focus_first:
        # Agenda topics rank above score so the review can be run top-to-bottom,
        # then by the earliest agenda topic, then by score within each group.
        rows.sort(key=lambda r: (
            0 if r["focus"] else 1,
            min((FOCUS_ORDER.index(f) for f in r["focus"]), default=len(FOCUS_ORDER)),
            -r["score"],
        ))
    else:
        rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def fetch_window_risks(start: datetime, end: datetime) -> list[dict[str, Any]]:
    rows = query(
        "SELECT * FROM risks WHERE updated_at >= ? AND updated_at <= ?",
        (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
    )
    out = [enrich(r) for r in rows]
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def aggregate(risks: list[dict[str, Any]]) -> dict[str, Any]:
    by_band = {b: 0 for b in BAND_ORDER}
    by_area: dict[str, int] = {}
    by_focus: dict[str, int] = {}
    total_downtime = 0.0
    total_units = 0.0
    for r in risks:
        by_band[r["band"]] = by_band.get(r["band"], 0) + 1
        by_area[r["area"]] = by_area.get(r["area"], 0) + 1
        for f in r.get("focus") or []:
            by_focus[f] = by_focus.get(f, 0) + 1
        total_downtime += r.get("downtime_minutes") or 0
        total_units += r.get("impact_units") or 0
    open_risks = [r for r in risks if r["status"] != "closed"]
    avg = round(sum(r["score"] for r in open_risks) / len(open_risks), 1) if open_risks else 0.0
    # "top" must stay score-ranked even when the register is ordered by agenda,
    # otherwise the executive summary would headline a low-scoring risk.
    by_score = sorted(risks, key=lambda r: r["score"], reverse=True)
    return {
        "total": len(risks),
        "open": len(open_risks),
        "by_band": by_band,
        "by_area": dict(sorted(by_area.items(), key=lambda kv: -kv[1])),
        "by_focus": {k: by_focus[k] for k in FOCUS_ORDER if k in by_focus},
        "critical": [r for r in risks if r["band"] == "Critical"],
        "top": by_score[:5],
        "total_downtime_minutes": round(total_downtime, 1),
        "total_impact_units": round(total_units, 1),
        "avg_score": avg,
        "health": health_index(avg, by_band.get("Critical", 0)),
    }


def health_index(avg_score: float, critical_count: int) -> int:
    """0-100 where 100 is a fully healthy shift."""
    return max(0, min(100, round(100 - avg_score - critical_count * 5)))

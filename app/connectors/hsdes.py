"""Connector for an HSD-ES saved query ("HSD DT Latest").

Reads a saved HSD-ES community query via its REST execution endpoint, using
the caller's Windows identity (same negotiate-via-PowerShell pattern as the
MMS connector, since HSD-ES also rejects Python's SSPI handshake directly).

The query itself only returns record-level fields (title, priority, status,
submitter, dates, ...) — there is no dedicated tool/cell/product column, so
the tool/cell identifier is extracted from the free-text title (e.g.
"HDMX 7168 TOOL DOWN" -> "7168"). Records are grouped by that identifier so
repeat offenders (the same tool opening multiple DT tickets) surface as a
follow-up list, which is what the shift review actually wants to see.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# Tool/cell identifiers in HSD-ES DT titles look like "HDMX 7168", "HDMX#7168",
# "HDMX7168_TOOLDOWN", or occasionally just the bare number "2993 TOOL DOWN".
TOOL_RE = re.compile(r"(?:HDMX\s*#?\s*)?(\d{3,5})", re.I)

# A PowerShell subprocess round-trip to HSD-ES costs several seconds, and the
# dashboard/summary rebuild this on every render, so the raw row list is
# cached briefly (see HSDES_CACHE_SECONDS) rather than fetched every time.
_cache: dict[str, Any] = {"query_id": None, "rows": None, "fetched_at": 0.0}


class HSDESUnavailable(RuntimeError):
    """The query could not be read (VPN down, no permission, bad query id)."""


def _fetch_page(query_id: str, start_at: int, timeout: int) -> dict[str, Any]:
    url = f"{settings.hsdes_base_url.rstrip('/')}/rest/query/execution/{query_id}?start_at={start_at}"
    script = (
        "$ProgressPreference='SilentlyContinue';"
        f"$r = Invoke-WebRequest -Uri '{url}' -UseBasicParsing -UseDefaultCredentials"
        f" -TimeoutSec {timeout};"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Write-Output $r.Content"
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=timeout + 30,
    )
    body = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 or not body.strip():
        raise HSDESUnavailable(
            f"failed to fetch HSD-ES query {query_id} (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:300]}"
        )
    try:
        return json.loads(body)
    except ValueError as exc:
        raise HSDESUnavailable(f"HSD-ES query {query_id} did not return JSON: {body[:200]}") from exc


def fetch_query(query_id: str | None = None, timeout: int | None = None,
                use_cache: bool = True) -> list[dict[str, Any]]:
    """Fetch every row of the saved query, paging past the 100-row page size.

    Cached for ``settings.hsdes_cache_seconds`` since each call costs a
    PowerShell subprocess round-trip (~5s); pass ``use_cache=False`` to force
    a refresh (e.g. the explicit "Sync HSD-ES" button).
    """
    query_id = query_id or settings.hsdes_query_id
    timeout = timeout or settings.hsdes_timeout_seconds
    if not query_id:
        raise HSDESUnavailable("HSDES_QUERY_ID is not configured")

    if use_cache and _cache["query_id"] == query_id and _cache["rows"] is not None:
        age = time.time() - _cache["fetched_at"]
        if age < settings.hsdes_cache_seconds:
            return _cache["rows"]

    rows: list[dict[str, Any]] = []
    start_at = 1
    while True:
        page = _fetch_page(query_id, start_at, timeout)
        data = page.get("data") or []
        rows.extend(data)
        max_results = page.get("max_results") or len(data) or 1
        if len(data) < max_results:
            break
        start_at += max_results
        if start_at > 5000:  # safety cap: a saved query is never this large
            break

    _cache.update(query_id=query_id, rows=rows, fetched_at=time.time())
    return rows


def _tool_id(title: str) -> str | None:
    m = TOOL_RE.search(title or "")
    return m.group(1) if m else None


def dt_latest_report(min_repeats: int | None = None, use_cache: bool = True) -> dict[str, Any]:
    """Group the HSD DT Latest query by tool/cell, surfacing repeat offenders.

    Returns every open/complete record plus a ``repeat_offenders`` list of
    tools that appear at least ``min_repeats`` times, newest-first within
    each group, sorted by how many times that tool has recurred.
    """
    min_repeats = min_repeats or settings.hsdes_min_repeats
    rows = fetch_query(use_cache=use_cache)

    groups: dict[str, list[dict[str, Any]]] = {}
    unmatched = 0
    for r in rows:
        tool = _tool_id(r.get("title", ""))
        if not tool:
            unmatched += 1
            continue
        groups.setdefault(tool, []).append(r)

    offenders = []
    for tool, recs in groups.items():
        if len(recs) < min_repeats:
            continue
        recs_sorted = sorted(recs, key=lambda r: r.get("open_date") or "", reverse=True)
        offenders.append({
            "tool": tool,
            "count": len(recs),
            "open_count": sum(1 for r in recs if (r.get("status") or "").lower() == "open"),
            "titles": [r.get("title", "").strip() for r in recs_sorted],
            "priorities": sorted({r.get("priority", "") for r in recs_sorted}),
            "programs": sorted({r.get("program", "") for r in recs_sorted if r.get("program")}),
            "latest_open_date": recs_sorted[0].get("open_date", "") if recs_sorted else "",
            "submitted_by": sorted({r.get("submitted_by", "") for r in recs_sorted if r.get("submitted_by")}),
            "ids": [r.get("id", "") for r in recs_sorted],
        })

    offenders.sort(key=lambda o: (-o["count"], -o["open_count"]))

    return {
        "available": bool(rows),
        "total_records": len(rows),
        "unmatched": unmatched,
        "repeat_offenders": offenders,
        "min_repeats": min_repeats,
    }


def focus_report() -> dict[str, Any]:
    """Everything this connector supplies, in one payload."""
    return dt_latest_report()


def sync() -> dict[str, Any]:
    """Probe the query and report what is readable.

    Deliberately read-only: HSD-ES is already authoritative, so copying rows
    into the local database would only create a second, staler copy.
    """
    if not settings.hsdes_query_id:
        raise HSDESUnavailable("HSDES_QUERY_ID is not configured")
    report = dt_latest_report(use_cache=False)
    if not report["available"]:
        raise HSDESUnavailable("HSD-ES query returned no rows — check VPN/permissions/query id.")
    return report

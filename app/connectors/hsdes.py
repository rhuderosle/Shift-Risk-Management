"""Connector for an HSD-ES saved query ("HSD DT Latest").

Reads a saved HSD-ES community query via its REST execution endpoint, using
the caller's Windows identity (same negotiate-via-PowerShell pattern as the
MMS connector, since HSD-ES also rejects Python's SSPI handshake directly).

The saved query itself only returns record-level bookkeeping fields (title,
priority, status, submitter, dates, ...) — no tool/cell/module or problem
text. Those live on each record's *article* (``/rest/article/<id>``), which is
fetched in a second pass, batched into a single PowerShell subprocess so N
records cost one process spawn rather than N.

Grouping is done on ``tool_top_cell_id`` (the canonical asset id HSD-ES
assigns), not the bare number in the title: the same number can refer to
different equipment (e.g. "2677" is both a chiller, ``ATS_Chiller#2677``, and
an unrelated handler tool, ``PG12THHX2677``) — grouping on the title number
alone silently merges unrelated issues into one false "repeat offender".
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import re
import subprocess
import time
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# Fallback tool/cell identifier parsed from the title when the article-level
# tool_top_cell_id is blank. Titles look like "HDMX 7168", "HDMX#7168",
# "HDMX7168_TOOLDOWN", or occasionally just the bare number "2993 TOOL DOWN".
TITLE_TOOL_RE = re.compile(r"(?:HDMX\s*#?\s*)?(\d{3,5})", re.I)

ARTICLE_FIELDS = (
    "id,description,"
    "services_sys_val.support.tool_id,"
    "services_sys_val.support.tool_top_cell_id,"
    "services_sys_val.support.main_module,"
    "services_sys_val.support.sub_module"
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
# Generic dropdown placeholder text some submitters never replace; not a real
# module description, so it is dropped rather than shown as if meaningful.
PLACEHOLDER_MODULE_RE = re.compile(r"if found symptom existing", re.I)

# A PowerShell subprocess round-trip to HSD-ES costs several seconds, and the
# dashboard/summary rebuild this on every render, so results are cached
# briefly (see HSDES_CACHE_SECONDS) rather than fetched every time.
_query_cache: dict[str, Any] = {"query_id": None, "rows": None, "fetched_at": 0.0}
_article_cache: dict[str, Any] = {"by_id": {}, "fetched_at": 0.0}


class HSDESUnavailable(RuntimeError):
    """The query could not be read (VPN down, no permission, bad query id)."""


def _run_powershell(script: str, timeout: int) -> str:
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=timeout + 30,
    )
    body = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 or not body.strip():
        raise HSDESUnavailable(
            f"HSD-ES request failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:300]}"
        )
    return body


def _fetch_page(query_id: str, start_at: int, timeout: int) -> dict[str, Any]:
    url = f"{settings.hsdes_base_url.rstrip('/')}/rest/query/execution/{query_id}?start_at={start_at}"
    script = (
        "$ProgressPreference='SilentlyContinue';"
        f"$r = Invoke-WebRequest -Uri '{url}' -UseBasicParsing -UseDefaultCredentials"
        f" -TimeoutSec {timeout};"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Write-Output $r.Content"
    )
    body = _run_powershell(script, timeout)
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

    if use_cache and _query_cache["query_id"] == query_id and _query_cache["rows"] is not None:
        age = time.time() - _query_cache["fetched_at"]
        if age < settings.hsdes_cache_seconds:
            return _query_cache["rows"]

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

    _query_cache.update(query_id=query_id, rows=rows, fetched_at=time.time())
    return rows


def fetch_articles(ids: list[str], timeout: int | None = None,
                   use_cache: bool = True) -> dict[str, dict[str, Any]]:
    """Fetch per-record detail (description, tool/cell/module) for each id.

    All ids are fetched in a single PowerShell subprocess (one HTTP call per
    id inside one process), since spawning a process per id would multiply
    the ~1s subprocess-startup cost by the record count. Returns a dict keyed
    by id; ids that fail individually are simply omitted rather than failing
    the whole batch.
    """
    timeout = timeout or settings.hsdes_timeout_seconds
    now = time.time()
    cache_fresh = use_cache and (now - _article_cache["fetched_at"]) < settings.hsdes_cache_seconds
    if cache_fresh:
        missing = [i for i in ids if i not in _article_cache["by_id"]]
    else:
        missing = list(ids)

    if missing:
        base = settings.hsdes_base_url.rstrip("/")
        ids_literal = ",".join(f"'{i}'" for i in missing)
        script = (
            "$ProgressPreference='SilentlyContinue';"
            f"$ids = @({ids_literal});"
            "$results = @();"
            "foreach ($rid in $ids) {"
            "  try {"
            f"    $r = Invoke-WebRequest -Uri \"{base}/rest/article/$rid`?fields={ARTICLE_FIELDS}\""
            f"      -UseBasicParsing -UseDefaultCredentials -TimeoutSec {timeout};"
            "    $j = $r.Content | ConvertFrom-Json;"
            "    if ($j.data) { $results += $j.data[0] }"
            "  } catch { }"
            "}"
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Write-Output (ConvertTo-Json -InputObject $results -Depth 5 -Compress)"
        )
        body = _run_powershell(script, timeout * max(len(missing), 1))
        try:
            fetched = json.loads(body) if body.strip() else []
        except ValueError as exc:
            raise HSDESUnavailable(f"HSD-ES article batch did not return JSON: {body[:200]}") from exc
        if isinstance(fetched, dict):  # a single result isn't wrapped in a list
            fetched = [fetched]
        for rec in fetched:
            rid = str(rec.get("id") or "")
            if rid:
                _article_cache["by_id"][rid] = rec
        _article_cache["fetched_at"] = now

    return {i: _article_cache["by_id"][i] for i in ids if i in _article_cache["by_id"]}


def _strip_html(text: str) -> str:
    text = TAG_RE.sub(" ", text or "")
    text = html_mod.unescape(text)
    return WS_RE.sub(" ", text).strip()


def _problem_summary(description_html: str, max_len: int = 220) -> str:
    """Pull the "Problem Description" line out of the rich-text article body.

    Submitters fill a templated form (Problem Description / Action Taken By
    OPS / Action Taken By Shift ET ...); only the first section is the actual
    issue, the rest is response narrative, so only that section is quoted.
    """
    plain = _strip_html(description_html)
    if not plain:
        return ""
    marker = "Problem Description"
    idx = plain.find(marker)
    if idx == -1:
        return plain[:max_len]
    after = plain[idx + len(marker):].lstrip(" :-")
    # Cut at the next templated heading, whichever comes first.
    for heading in ("Action Taken By", "Collateral ID", "ALARM/ERROR"):
        cut = after.find(heading)
        if cut != -1:
            after = after[:cut]
            break
    return after.strip()[:max_len]


# Submitters prefix the problem text with a work-week/shift/lot stamp that
# differs every time even when the underlying alarm is identical (e.g.
# "WW38.4N_ Alarm: ..." vs "WW 38.3N _ Alarm: ..."), so a literal string
# compare treats every occurrence as a distinct issue. Stripping that stamp
# lets the same recurring alarm be recognised as the same issue.
LEADING_STAMP_RE = re.compile(r"^WW\s*\d+(?:\.\d+)?[A-Za-z]?[_\s-]*", re.I)


def _issue_signature(problem: str) -> str:
    t = LEADING_STAMP_RE.sub("", problem or "").strip()
    # A short lot/run code (e.g. "P637166CR_6248_") sometimes sits between the
    # stamp and the actual "Alarm:"/"Encounter" marker - drop it too so only
    # the alarm signature itself is compared.
    for marker in ("Alarm:", "Encounter"):
        idx = t.find(marker)
        if 0 < idx <= 40:
            t = t[idx:]
            break
    return WS_RE.sub(" ", t).strip().lower()


def _tool_key(row: dict[str, Any], article: dict[str, Any] | None) -> str | None:
    if article:
        cell = (article.get("services_sys_val.support.tool_top_cell_id") or "").strip()
        if cell:
            return cell
        tool_id = (article.get("services_sys_val.support.tool_id") or "").strip()
        if tool_id:
            return tool_id
    m = TITLE_TOOL_RE.search(row.get("title", ""))
    return m.group(1) if m else None


def _module(article: dict[str, Any] | None) -> str:
    if not article:
        return ""
    main = (article.get("services_sys_val.support.main_module") or "").strip()
    sub = (article.get("services_sys_val.support.sub_module") or "").strip()
    if sub and not PLACEHOLDER_MODULE_RE.search(sub):
        return f"{main}/{sub}" if main else sub
    return main


def dt_latest_report(min_repeats: int | None = None, use_cache: bool = True) -> dict[str, Any]:
    """Group the HSD DT Latest query by canonical tool/cell asset id.

    Returns a ``repeat_offenders`` list of assets that recur at least
    ``min_repeats`` times, each carrying a one-line problem summary per
    record (extracted from the article body) so the follow-up list actually
    says *what* the repeated issue is, not just that it repeated.
    """
    min_repeats = min_repeats or settings.hsdes_min_repeats
    rows = fetch_query(use_cache=use_cache)
    ids = [str(r.get("id")) for r in rows if r.get("id")]
    articles = fetch_articles(ids, use_cache=use_cache) if ids else {}

    groups: dict[str, list[dict[str, Any]]] = {}
    unmatched = 0
    for r in rows:
        article = articles.get(str(r.get("id")))
        tool = _tool_key(r, article)
        if not tool:
            unmatched += 1
            continue
        enriched = {
            **r,
            "tool": tool,
            "module": _module(article),
            "problem": _problem_summary(article.get("description", "") if article else ""),
        }
        groups.setdefault(tool, []).append(enriched)

    offenders = []
    for tool, recs in groups.items():
        if len(recs) < min_repeats:
            continue
        recs_sorted = sorted(recs, key=lambda r: r.get("open_date") or "", reverse=True)

        # Same asset opening several tickets can be one issue recurring, or
        # several unrelated issues - only the former is a true "repeated
        # issue". Count identical problem text (normalized) to tell them
        # apart, so the follow-up highlights *which* issue is the repeat.
        norm_counts: dict[str, int] = {}
        for r in recs_sorted:
            key = _issue_signature(r["problem"])
            if key:
                norm_counts[key] = norm_counts.get(key, 0) + 1
        repeated_key = max(norm_counts, key=norm_counts.get) if norm_counts else None
        repeated_count = norm_counts.get(repeated_key, 0) if repeated_key else 0
        is_actual_repeat = repeated_count >= min_repeats
        repeated_problem = None
        if is_actual_repeat:
            repeated_problem = next(
                (r["problem"] for r in recs_sorted
                 if _issue_signature(r["problem"]) == repeated_key),
                None,
            )

        offenders.append({
            "tool": tool,
            "count": len(recs),
            "open_count": sum(1 for r in recs if (r.get("status") or "").lower() == "open"),
            "module": next((r["module"] for r in recs_sorted if r["module"]), ""),
            "issues": [
                {
                    "id": r.get("id", ""),
                    "title": r.get("title", "").strip(),
                    "problem": r["problem"] or "(no problem description on this ticket)",
                    "status": r.get("status", ""),
                    "priority": r.get("priority", ""),
                    "open_date": r.get("open_date", ""),
                    "submitted_by": r.get("submitted_by", ""),
                    "is_repeated_issue": (
                        is_actual_repeat
                        and _issue_signature(r["problem"]) == repeated_key
                    ),
                }
                for r in recs_sorted
            ],
            # Set only when the *same* problem text recurred >= min_repeats
            # times on this asset - i.e. one issue kept coming back, as
            # opposed to several different issues happening to hit the same
            # tool/cell.
            "repeated_issue": repeated_problem,
            "repeated_issue_count": repeated_count if is_actual_repeat else 0,
            "priorities": sorted({r.get("priority", "") for r in recs_sorted if r.get("priority")}),
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

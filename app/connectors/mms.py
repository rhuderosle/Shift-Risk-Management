"""Connector for an internal MMS Passdown system.

Scrapes the ASP.NET PassdownView page using the caller's Windows credentials,
extracts each passdown template section, and converts sections into risk records
for the Shift Risk Management System.

MMS content is free-form rich text authored by shift owners, so severity and
likelihood are *inferred* from keyword signals. Every imported row is flagged
`needs_review = 1` so a human confirms the scoring before it drives escalation.
"""

from __future__ import annotations

import html as html_mod
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .. config import settings

log = logging.getLogger(__name__)

TITLE_RE = re.compile(
    r'id="ctl00_ContentPlaceHolder1_Passdown1_(?P<fa>\d+)_(?P<gid>\d+)_(?P<idx>\d+)'
    r'_TemplateObjectTitle_ToTitle"[^>]*>(?P<title>.*?)</span>',
    re.DOTALL,
)
BODY_RE_TMPL = (
    r'id="ctl00_ContentPlaceHolder1_Passdown1_{fa}_{gid}_{idx}_lblHtmlView"[^>]*>'
    r'(?P<body>.*?)</span>\s*</td>'
)
UPDATED_RE_TMPL = (
    r'id="ctl00_ContentPlaceHolder1_Passdown1_{fa}_{gid}_{idx}_lblLastUpdatedBy"[^>]*>'
    r'\s*Last Updated By:\s*(?P<who>.*?)\s*\[\s*(?P<when>.*?)\s*\]\s*</span>'
)

# Keyword signals -> (severity floor, risk area tag).
SEVERITY_SIGNALS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"\b(safety|injury|ehs|near ?miss|evacuat|fire|chemical spill)\b", re.I), 5, "Safety"),
    (re.compile(r"\b(line ?down|tool ?down|down ?time|breakdown|stopper|halt|shutdown)\b", re.I), 4, "Downtime"),
    (re.compile(r"\b(excursion|escalat|quality hold|q-?hold|scrap|reject|failure|fail)\b", re.I), 4, "Quality"),
    (re.compile(r"\b(miss(?:es|ed)?|backlog|shortfall|behind|delay|late)\b", re.I), 3, "Delivery"),
    (re.compile(r"\b(risk|issue|problem|abnormal|alarm|deviat|concern)\b", re.I), 3, "General"),
]
LIKELIHOOD_SIGNALS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(recurring|repeat|again|multiple|chronic|ongoing|continue[sd]?)\b", re.I), 5),
    (re.compile(r"\b(open|pending|unresolved|monitor(?:ing)?|wip|in progress)\b", re.I), 4),
    (re.compile(r"\b(closed|resolved|completed|recovered|restored|clear(?:ed)?)\b", re.I), 2),
]
RESOLVED_RE = re.compile(r"\b(closed|resolved|completed|recovered|no issue|nil|none)\b", re.I)
MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:min(?:ute)?s?)\b", re.I)
HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hrs?|hours?)\b", re.I)
UNITS_RE = re.compile(r"(\d[\d,]{2,})\s*(?:units?|pcs|lots?|wafers?)\b", re.I)
# Durations only count as downtime when the same line talks about downtime.
DOWNTIME_CONTEXT_RE = re.compile(
    r"\b(down ?time|dt\b|line ?down|tool ?down|idle|stopper|breakdown|repair|outage)", re.I
)

SKIP_TITLES = re.compile(r"^\s*(jump to|top)\s*$", re.I)


@dataclass
class PassdownSection:
    index: str
    title: str
    text: str
    updated_by: str = ""
    updated_at: str = ""
    tags: list[str] = field(default_factory=list)


def _strip_html(fragment: str) -> str:
    """Flatten HTML to text, preserving table structure.

    Cells become " | " and table rows become their own line. Rows are marked with
    a sentinel first so that <br> inside a cell can't be mistaken for a row break
    — without this, wide passdown tables collapse into one giant line and
    per-row metrics (LIPAS, VPO) become unreadable.
    """
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    txt = re.sub(r"(?i)</tr\s*>", "\x00ROW\x00", txt)
    txt = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", txt)
    txt = re.sub(r"(?i)</t[dh]\s*>", " | ", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html_mod.unescape(txt).replace("\xa0", " ")
    txt = re.sub(r"[ \t]+", " ", txt)
    # Collapse newlines within a row, then turn sentinels into real row breaks.
    txt = "\n".join(
        re.sub(r"\s*\n\s*", " ", part).strip()
        for part in txt.split("\x00ROW\x00")
    )
    txt = re.sub(r"\n\s*\n\s*", "\n", txt)
    return txt.strip()


def fetch_passdown_html(url: str | None = None, timeout: int | None = None) -> str:
    """Fetch the MMS passdown page using Windows integrated authentication.

    MMS rejects Python's SSPI negotiate handshake (403), so we delegate the request
    to PowerShell's Invoke-WebRequest -UseDefaultCredentials, which authenticates
    with the logged-on user's Windows identity.
    """
    url = url or settings.mms_passdown_url
    timeout = timeout or settings.mms_timeout_seconds
    if not url:
        raise RuntimeError("MMS_PASSDOWN_URL is not configured")

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
    if proc.returncode == 0 and len(body) > 2000:
        return body
    raise RuntimeError(
        f"failed to fetch MMS passdown (rc={proc.returncode}, {len(body)} bytes): "
        f"{proc.stderr.decode(errors='replace')[:300]}"
    )


def parse_sections(page_html: str) -> list[PassdownSection]:
    sections: list[PassdownSection] = []
    for m in TITLE_RE.finditer(page_html):
        title = _strip_html(m.group("title"))
        if not title or SKIP_TITLES.match(title):
            continue
        fa, gid, idx = m.group("fa"), m.group("gid"), m.group("idx")

        body_m = re.search(BODY_RE_TMPL.format(fa=fa, gid=gid, idx=idx), page_html, re.DOTALL)
        upd_m = re.search(UPDATED_RE_TMPL.format(fa=fa, gid=gid, idx=idx), page_html, re.DOTALL)
        sections.append(PassdownSection(
            index=idx,
            title=title,
            text=_strip_html(body_m.group("body")) if body_m else "",
            updated_by=_strip_html(upd_m.group("who")) if upd_m else "",
            updated_at=_strip_html(upd_m.group("when")) if upd_m else "",
        ))
    sections.sort(key=lambda s: int(s.index))
    return sections


def _infer_scores(title: str, text: str) -> tuple[int, int, list[str]]:
    blob = f"{title}\n{text}"
    severity, tags = 2, []
    for pattern, sev, tag in SEVERITY_SIGNALS:
        if pattern.search(blob):
            severity = max(severity, sev)
            tags.append(tag)
    likelihood = 3
    for pattern, lik in LIKELIHOOD_SIGNALS:
        if pattern.search(blob):
            likelihood = lik
            break
    return severity, likelihood, tags


def _infer_impact(text: str) -> tuple[float, float]:
    """Sum durations only from lines that actually discuss downtime."""
    downtime = 0.0
    for line in text.splitlines():
        if not DOWNTIME_CONTEXT_RE.search(line):
            continue
        downtime += sum(float(v) for v in MINUTES_RE.findall(line)[:3])
        downtime += sum(float(v) * 60 for v in HOURS_RE.findall(line)[:3])
    units = sum(float(v.replace(",", "")) for v in UNITS_RE.findall(text)[:5])
    return min(downtime, 1440.0), min(units, 100000.0)


def sections_to_risks(sections: Iterable[PassdownSection], shift: str,
                      url: str | None = None) -> list[dict[str, Any]]:
    url = url or settings.mms_passdown_url
    risks: list[dict[str, Any]] = []
    for s in sections:
        body = s.text.strip()
        if len(body) < 15:
            continue  # empty or placeholder section
        severity, likelihood, tags = _infer_scores(s.title, body)
        if severity <= 2 and RESOLVED_RE.search(body) and not tags:
            continue  # informational only
        downtime, units = _infer_impact(body)
        risks.append({
            "title": s.title[:200],
            "description": re.sub(r"\s+", " ", body)[:600],
            "area": tags[0] if tags else "Class Test",
            "source": "MMS Passdown",
            "owner": s.updated_by,
            "shift": shift,
            "severity": severity,
            "likelihood": likelihood,
            "status": "open",
            "impact_units": units,
            "downtime_minutes": downtime,
            "action": "Review MMS passdown section and confirm containment",
            "external_ref": f"{url}#section-{s.index}",
            "needs_review": 1,
        })
    return risks


def sync(shift: str | None = None, url: str | None = None) -> dict[str, Any]:
    """Fetch, parse and upsert MMS passdown content as risks."""
    from ..db import execute, query, utcnow
    from ..risk_engine import current_shift

    shift = shift or current_shift()
    url = url or settings.mms_passdown_url
    sections = parse_sections(fetch_passdown_html(url))
    candidates = sections_to_risks(sections, shift, url)

    # Persist the full untruncated section text so metric lookups (LIPAS, VPO,
    # UTZ...) can read numbers that don't survive the risk description summary.
    for s in sections:
        if not s.text.strip():
            continue
        execute(
            "INSERT INTO source_documents (source, ref, title, body, updated_by, shift,"
            " fetched_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(ref) DO UPDATE SET title=excluded.title, body=excluded.body,"
            " updated_by=excluded.updated_by, shift=excluded.shift,"
            " fetched_at=excluded.fetched_at",
            ("MMS Passdown", f"{url}#section-{s.index}", s.title, s.text,
             s.updated_by, shift, utcnow()),
        )

    created = updated = 0
    for r in candidates:
        existing = query(
            "SELECT id FROM risks WHERE external_ref = ?",
            (r["external_ref"],),
        )
        if existing:
            execute(
                "UPDATE risks SET title = ?, description = ?, severity = ?, likelihood = ?,"
                " impact_units = ?, downtime_minutes = ?, owner = ?, source_link = ?,"
                " updated_at = ? WHERE id = ?",
                (r["title"], r["description"], r["severity"], r["likelihood"],
                 r["impact_units"], r["downtime_minutes"], r["owner"], r["external_ref"],
                 utcnow(), existing[0]["id"]),
            )
            updated += 1
        else:
            now = utcnow()
            execute(
                "INSERT INTO risks (title, description, area, source, owner, shift, severity,"
                " likelihood, status, impact_units, downtime_minutes, action, created_at,"
                " updated_at, external_ref, source_link, needs_review)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["title"], r["description"], r["area"], r["source"], r["owner"], r["shift"],
                 r["severity"], r["likelihood"], r["status"], r["impact_units"],
                 r["downtime_minutes"], r["action"], now, now, r["external_ref"],
                 r["external_ref"], r["needs_review"]),
            )
            created += 1

    log.info("MMS sync: %s sections, %s created, %s updated", len(sections), created, updated)
    return {
        "sections_found": len(sections),
        "risk_candidates": len(candidates),
        "created": created,
        "updated": updated,
        "shift": shift,
        "synced_at": datetime.now().isoformat(timespec="seconds"),
    }

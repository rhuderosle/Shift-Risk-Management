"""Connector for Outlook mail (local COM, no credentials required).

Reads recent mail from the running Outlook profile via PowerShell COM automation,
filters it to messages that look operationally risk-relevant, and converts those
into risk records.

Like the MMS connector, severity/likelihood are *inferred* from keyword signals, so
imported rows are flagged `needs_review = 1` for human confirmation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..config import settings

log = logging.getLogger(__name__)

# Subject/body signals that make a mail worth tracking as a risk.
# Deliberately narrow: broad words like "pending" or "impact" match almost every
# operational mail and flood the register with routine traffic.
RISK_KEYWORDS = [
    "line down", "tool down", "downtime", "breakdown", "outage", "stoppage", "stopper",
    "excursion", "escalat", "critical", "urgent", "immediate", "asap",
    "safety", "injury", "ehs", "near miss", "incident", "accident",
    "failure", "failed", "reject", "scrap", "quality hold", "q-hold", "hold lot",
    "abnormal", "alarm", "deviation", "violation", "blocker", "blocked",
    "shortage", "backlog", "misprocess", "contaminat", "risk of", "at risk",
]
KEYWORD_RE = re.compile("|".join(re.escape(k) for k in RISK_KEYWORDS), re.I)

# Only these justify importing a mail on body text alone.
STRONG_KEYWORD_RE = re.compile(
    r"\b(line ?down|tool ?down|excursion|safety incident|injury|near ?miss|"
    r"quality hold|q-?hold|hold lot|scrap|escalat\w*|line stoppage)\b", re.I
)

# Routine/automated traffic that should never become a risk, even if it trips a keyword.
NOISE_RE = re.compile(
    r"\b(newsletter|unsubscribe|out of office|automatic reply|survey|training|"
    r"invitation:|accepted:|declined:|tentative:|meeting forward|birthday|"
    r"webinar|subscription|password will expire|uploaded new files|"
    r"manager approval|approval requested|ship memo|shiftly report|shift report|"
    r"collaterals report|execution summary|daily report|weekly report|"
    r"new vpo request|vpo requests|modified by|published by|flow modification)\b",
    re.I,
)

# This system's own passdown/escalation mail lands in the same inbox it reads. Without
# this guard those mails become risks, which inflate the next passdown — a feedback loop.
SELF_GENERATED_RE = re.compile(
    r"Risk Passdown|\[ESCALATION\]|\[ACTION REQUIRED\]|\[TEST ->", re.I
)

SEVERITY_SIGNALS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"\b(safety|injury|ehs|near ?miss|evacuat|fire|spill)\b", re.I), 5, "Safety"),
    (re.compile(r"\b(line ?down|tool ?down|down ?time|breakdown|outage|stoppage|stopper)\b", re.I), 4, "Downtime"),
    (re.compile(r"\b(excursion|scrap|reject|quality hold|q-?hold|hold lot|failure|fail)\b", re.I), 4, "Quality"),
    (re.compile(r"\b(miss(?:es|ed)?|backlog|shortage|delay|late|behind)\b", re.I), 3, "Delivery"),
    (re.compile(r"\b(risk|issue|problem|abnormal|alarm|deviat|pending|blocker)\b", re.I), 3, "General"),
]
LIKELIHOOD_SIGNALS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(recurring|repeat|again|multiple|chronic|ongoing|still)\b", re.I), 5),
    (re.compile(r"\b(open|pending|unresolved|monitor(?:ing)?|in progress|wip)\b", re.I), 4),
    (re.compile(r"\b(closed|resolved|completed|recovered|restored|clear(?:ed)?|fixed)\b", re.I), 2),
]
URGENT_RE = re.compile(r"\b(urgent|critical|immediate|asap|escalat)\b", re.I)
MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:min(?:ute)?s?)\b", re.I)
HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hrs?|hours?)\b", re.I)
UNITS_RE = re.compile(r"(\d[\d,]{2,})\s*(?:units?|pcs|lots?|wafers?|duts?)\b", re.I)
DOWNTIME_CONTEXT_RE = re.compile(
    r"\b(down ?time|dt\b|line ?down|tool ?down|idle|stopper|breakdown|repair|outage)", re.I
)

# PowerShell script: pulls recent mail out of Outlook and emits JSON.
PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  $ol = New-Object -ComObject Outlook.Application
  $ns = $ol.GetNamespace('MAPI')
  if ('__FOLDER__' -ne '') {
    $root = $ns.GetDefaultFolder(6).Parent
    $folder = $null
    foreach ($f in $root.Folders) { if ($f.Name -eq '__FOLDER__') { $folder = $f } }
    if ($null -eq $folder) { throw "folder '__FOLDER__' not found" }
  } else {
    $folder = $ns.GetDefaultFolder(6)
  }
  $items = $folder.Items
  $items.Sort('[ReceivedTime]', $true)
  $cut = (Get-Date).AddHours(-__HOURS__).ToString('MM/dd/yyyy hh:mm tt')
  $filtered = $items.Restrict("[ReceivedTime] >= '$cut'")
  $out = New-Object System.Collections.ArrayList
  $n = 0
  foreach ($m in $filtered) {
    if ($n -ge __MAX__) { break }
    try {
      if ($m.Class -ne 43) { continue }   # olMail only
      $body = ''
      try { $body = $m.Body } catch {}
      if ($body.Length -gt 4000) { $body = $body.Substring(0, 4000) }
      $null = $out.Add([pscustomobject]@{
        subject  = [string]$m.Subject
        sender   = [string]$m.SenderName
        received = $m.ReceivedTime.ToString('yyyy-MM-ddTHH:mm:ss')
        body     = [string]$body
        entry_id = [string]$m.EntryID
        importance = [int]$m.Importance
        unread   = [bool]$m.UnRead
      })
      $n++
    } catch {}
  }
  $json = $out | ConvertTo-Json -Depth 3 -Compress
  if ($null -eq $json) { $json = '[]' }
  [System.IO.File]::WriteAllText('__OUTFILE__', $json, (New-Object System.Text.UTF8Encoding $false))
} catch {
  $err = @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText('__OUTFILE__', $err, (New-Object System.Text.UTF8Encoding $false))
}
"""


@dataclass
class MailItem:
    subject: str
    sender: str
    received: str
    body: str
    entry_id: str
    importance: int = 1
    unread: bool = False


def fetch_mail(hours: int | None = None, folder: str | None = None,
               max_items: int | None = None) -> list[MailItem]:
    """Read recent mail from the local Outlook profile via COM."""
    hours = hours or settings.outlook_lookback_hours
    folder = folder if folder is not None else settings.outlook_folder
    max_items = max_items or settings.outlook_max_items

    out_path = Path(tempfile.gettempdir()) / f"shiftrisk_mail_{os.getpid()}_{uuid.uuid4().hex}.json"
    script = (
        PS_SCRIPT.replace("__HOURS__", str(int(hours)))
        .replace("__MAX__", str(int(max_items)))
        .replace("__FOLDER__", folder.replace("'", "''"))
        .replace("__OUTFILE__", str(out_path).replace("\\", "\\\\"))
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
            capture_output=True,
            timeout=settings.outlook_timeout_seconds,
        )
        # The payload goes via a temp file, not stdout: a day's mail can exceed
        # 400 KB, which truncates unpredictably through a pipe.
        if not out_path.exists():
            raise RuntimeError(
                f"Outlook read produced no output (rc={proc.returncode}): "
                f"{proc.stderr.decode(errors='replace')[:300]}"
            )
        raw = out_path.read_text(encoding="utf-8").strip()
    finally:
        out_path.unlink(missing_ok=True)

    if not raw:
        raise RuntimeError("Outlook read returned an empty file")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unexpected Outlook output: {raw[:200]}") from exc

    if isinstance(data, dict):
        if "error" in data:
            raise RuntimeError(f"Outlook error: {data['error']}")
        data = [data]  # single mail comes back unwrapped
    return [
        MailItem(
            subject=(d.get("subject") or "").strip(),
            sender=(d.get("sender") or "").strip(),
            received=(d.get("received") or "").strip(),
            body=(d.get("body") or "").strip(),
            entry_id=(d.get("entry_id") or "").strip(),
            importance=int(d.get("importance") or 1),
            unread=bool(d.get("unread")),
        )
        for d in data
        if isinstance(d, dict)
    ]


def _clean_body(body: str) -> str:
    """Drop quoted replies, signatures and disclaimer noise."""
    cut_markers = [
        r"\nFrom:.*", r"\n-----Original Message-----.*", r"\n_{5,}.*",
        r"\nSent from my .*", r"\nThis e-?mail (?:and any attachments )?.*confidential.*",
    ]
    text = body
    for marker in cut_markers:
        text = re.split(marker, text, maxsplit=1, flags=re.I | re.S)[0]
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n", text)
    return text.strip()


def is_risk_relevant(mail: MailItem) -> bool:
    """Keep mail that signals a genuine operational risk.

    Subject matches are trusted; body-only matches must be strong signals, otherwise
    routine threads that merely mention a keyword in a quoted reply get pulled in.
    """
    if SELF_GENERATED_RE.search(mail.subject):
        return False  # never re-ingest our own output
    if NOISE_RE.search(mail.subject):
        return False
    if KEYWORD_RE.search(mail.subject):
        return True
    body = _clean_body(mail.body)[:1500]
    return bool(STRONG_KEYWORD_RE.search(body))


def _infer_scores(subject: str, text: str, importance: int) -> tuple[int, int, list[str]]:
    blob = f"{subject}\n{text}"
    severity, tags = 2, []
    for pattern, sev, tag in SEVERITY_SIGNALS:
        if pattern.search(blob):
            severity = max(severity, sev)
            tags.append(tag)
    if URGENT_RE.search(subject) or importance >= 2:
        severity = min(5, severity + 1)  # flagged high-importance by the sender
    likelihood = 3
    for pattern, lik in LIKELIHOOD_SIGNALS:
        if pattern.search(blob):
            likelihood = lik
            break
    return severity, likelihood, tags


def _infer_impact(text: str) -> tuple[float, float]:
    downtime = 0.0
    for line in text.splitlines():
        if not DOWNTIME_CONTEXT_RE.search(line):
            continue
        downtime += sum(float(v) for v in MINUTES_RE.findall(line)[:3])
        downtime += sum(float(v) * 60 for v in HOURS_RE.findall(line)[:3])
    units = sum(float(v.replace(",", "")) for v in UNITS_RE.findall(text)[:5])
    return min(downtime, 1440.0), min(units, 100000.0)


def _thread_key(subject: str) -> str:
    """Normalise a subject so a reply chain collapses into a single risk."""
    s = re.sub(r"^\s*(re|fw|fwd|aw|tr)\s*:\s*", "", subject, flags=re.I)
    while re.match(r"^\s*(re|fw|fwd|aw|tr)\s*:\s*", s, flags=re.I):
        s = re.sub(r"^\s*(re|fw|fwd|aw|tr)\s*:\s*", "", s, flags=re.I)
    s = re.sub(r"[\[\(].*?[\]\)]", " ", s)  # drop ticket/VPO ids that vary per mail
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def mail_to_risks(mails: Iterable[MailItem], shift: str) -> list[dict[str, Any]]:
    """Convert mail into risks, collapsing reply threads to one risk each.

    Mail arrives newest-first, so the first message seen for a thread is the latest;
    later replies only contribute their count.
    """
    by_thread: dict[str, dict[str, Any]] = {}
    for mail in mails:
        if not mail.subject or not is_risk_relevant(mail):
            continue
        key = _thread_key(mail.subject)
        if key in by_thread:
            by_thread[key]["_replies"] += 1
            continue

        body = _clean_body(mail.body)
        severity, likelihood, tags = _infer_scores(mail.subject, body, mail.importance)
        downtime, units = _infer_impact(body)
        summary = re.sub(r"\s+", " ", body)[:600] or "(no body text)"
        by_thread[key] = {
            "title": re.sub(r"^\s*(?:(?:re|fw|fwd|aw|tr)\s*:\s*)+", "", mail.subject,
                            flags=re.I)[:200],
            "description": f"From {mail.sender} ({mail.received[:16]}): {summary}"[:800],
            "area": tags[0] if tags else "Email",
            "source": "Outlook",
            "owner": mail.sender,
            "shift": shift,
            "severity": severity,
            "likelihood": likelihood,
            "status": "open",
            "impact_units": units,
            "downtime_minutes": downtime,
            "action": "Review email thread and confirm owner/containment",
            "external_ref": f"outlook:thread:{key[:120]}",
            # Clicking this in Outlook opens the original mail item.
            "source_link": f"outlook:{mail.entry_id}" if mail.entry_id else "",
            "needs_review": 1,
            "_replies": 0,
        }

    risks = []
    for r in by_thread.values():
        replies = r.pop("_replies")
        if replies >= 3:
            # An actively churning thread suggests an unresolved, recurring problem.
            r["likelihood"] = min(5, r["likelihood"] + 1)
            r["description"] = f"[{replies + 1} messages in thread] {r['description']}"[:800]
        elif replies:
            r["description"] = f"[{replies + 1} messages in thread] {r['description']}"[:800]
        risks.append(r)
    return risks


def sync(shift: str | None = None, hours: int | None = None,
         folder: str | None = None) -> dict[str, Any]:
    """Fetch, filter and upsert Outlook mail as risks."""
    from ..db import execute, query, utcnow
    from ..risk_engine import current_shift

    shift = shift or current_shift()
    mails = fetch_mail(hours=hours, folder=folder)
    candidates = mail_to_risks(mails, shift)

    created = updated = 0
    for r in candidates:
        existing = query(
            "SELECT id FROM risks WHERE external_ref = ?",
            (r["external_ref"],),
        )
        if existing:
            execute(
                "UPDATE risks SET title = ?, description = ?, severity = ?, likelihood = ?,"
                " impact_units = ?, downtime_minutes = ?, source_link = ?, updated_at = ?"
                " WHERE id = ?",
                (r["title"], r["description"], r["severity"], r["likelihood"],
                 r["impact_units"], r["downtime_minutes"], r["source_link"], utcnow(),
                 existing[0]["id"]),
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
                 r["source_link"], r["needs_review"]),
            )
            created += 1

    log.info("Outlook sync: %s mails scanned, %s created, %s updated",
             len(mails), created, updated)
    return {
        "mails_scanned": len(mails),
        "risk_candidates": len(candidates),
        "created": created,
        "updated": updated,
        "shift": shift,
        "folder": folder if folder is not None else (settings.outlook_folder or "Inbox"),
        "lookback_hours": hours or settings.outlook_lookback_hours,
        "synced_at": datetime.now().isoformat(timespec="seconds"),
    }

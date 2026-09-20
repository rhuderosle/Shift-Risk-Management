"""Orchestration: build shift reports, persist them, and trigger emails."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

from . import risk_engine as re_
from . import summarizer
from .config import settings
from .db import execute, get_setting, query, utcnow
from .emailer import send_email

log = logging.getLogger(__name__)


def build_report(shift: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    shift = shift or re_.current_shift(now)
    start, end = re_.shift_window(shift, now)

    risks = re_.fetch_window_risks(start, end)
    if not risks:
        # Fall back to all currently-open risks so a passdown is never empty of context.
        risks = re_.fetch_risks(include_closed=False)
    agg = re_.aggregate(re_.for_rollup(risks))

    bullets, generator = summarizer.summarize(shift, agg)
    html_body = summarizer.render_html(shift, agg, bullets, start, end, generator)
    text_body = summarizer.render_text(shift, agg, bullets, start, end)

    report_id = execute(
        "INSERT INTO shift_reports (shift, period_start, period_end, generated_at, generator,"
        " risk_count, critical_count, summary_html, summary_text)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (shift, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"),
         utcnow(), generator, agg["total"], agg["by_band"].get("Critical", 0),
         html_body, text_body),
    )
    return {
        "id": report_id, "shift": shift, "generator": generator,
        "period_start": start, "period_end": end,
        "aggregate": agg, "bullets": bullets,
        "summary_html": html_body, "summary_text": text_body,
    }


def passdown_recipients() -> list[str]:
    """Dashboard-saved recipients win over the .env default."""
    saved = get_setting("email_to", "")
    if saved.strip():
        return [a.strip() for a in saved.split(",") if a.strip()]
    return settings.recipients()


def send_passdown(shift: str | None = None, recipients: list[str] | None = None,
                  actor: str = "scheduler") -> dict[str, Any]:
    report = build_report(shift)
    agg = report["aggregate"]
    to = recipients or passdown_recipients()
    flag = "[ACTION REQUIRED] " if agg["by_band"].get("Critical", 0) else ""
    subject = (
        f"{flag}Shift {report['shift']} Risk Passdown — "
        f"{agg['open']} open, {agg['by_band'].get('Critical', 0)} critical "
        f"(health {agg['health']}/100)"
    )
    result = send_email("passdown", to, subject, report["summary_html"],
                        report["summary_text"], actor=actor)
    execute(
        "UPDATE shift_reports SET emailed_to = ?, email_status = ? WHERE id = ?",
        (", ".join(to), result["status"], report["id"]),
    )
    return {"report_id": report["id"], "shift": report["shift"], "subject": subject,
            "recipients": to, **result}


def scan_and_escalate(actor: str = "scheduler") -> dict[str, Any]:
    """Immediately email any newly-detected critical risk that hasn't been escalated yet."""
    rows = [re_.enrich(r) for r in query("SELECT * FROM risks WHERE status != 'closed' AND escalated_at IS NULL")]
    rows = re_.for_rollup(rows)
    critical = [r for r in rows if r["band"] == "Critical"]
    if not critical:
        return {"escalated": 0, "status": "none"}

    critical.sort(key=lambda r: r["score"], reverse=True)
    to = settings.escalation_recipients()
    items_html = "".join(
        f"<li style='margin-bottom:10px'><b>{html.escape(r['title'])}</b> "
        f"({html.escape(r['area'])}, owner {html.escape(r['owner'] or 'unassigned')}) — "
        f"score {r['score']}, sev {r['severity']}/5, likelihood {r['likelihood']}/5, "
        f"{r['downtime_minutes']:.0f} min downtime exposure.<br>"
        f"<span style='color:#555'>Action: {html.escape(r['action'] or 'assign owner and contain')}</span></li>"
        for r in critical
    )
    body = (
        "<div style=\"font-family:Segoe UI,Arial,sans-serif\">"
        "<h2 style='color:#b3261e;margin:0 0 8px'>Critical Shift Risk Escalation</h2>"
        f"<p style='margin:0 0 12px'>{len(critical)} critical risk(s) detected and require "
        "immediate shift-manager attention.</p>"
        f"<ul style='padding-left:18px'>{items_html}</ul>"
        "<p style='color:#8a94a6;font-size:11px'>Automated by the Shift Risk Management System.</p></div>"
    )
    text = "CRITICAL SHIFT RISK ESCALATION\n\n" + "\n".join(
        f"- {r['title']} ({r['area']}, {r['owner'] or 'unassigned'}) score={r['score']} "
        f"action={r['action'] or 'TBD'}" for r in critical
    )
    subject = f"[ESCALATION] {len(critical)} critical shift risk(s) require attention"
    result = send_email("escalation", to, subject, body, text, actor=actor)

    if result["status"] in ("sent", "file"):
        stamp = utcnow()
        for r in critical:
            execute("UPDATE risks SET escalated_at = ? WHERE id = ?", (stamp, r["id"]))
    return {"escalated": len(critical), "recipients": to, "subject": subject, **result}


def dashboard_context() -> dict[str, Any]:
    now = datetime.now()
    shift = re_.current_shift(now)
    risks = re_.fetch_risks(include_closed=True)
    active = [r for r in risks if r["status"] != "closed"]
    agg = re_.aggregate(re_.for_rollup(active))
    bullets, generator = summarizer.summarize(shift, agg)
    return {
        "title": settings.app_title,
        "shift": shift,
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "risks": risks,
        "agg": agg,
        "bullets": bullets,
        "focus_bullets": summarizer.focus_bullets(),
        "focus_order": re_.FOCUS_ORDER,
        "generator": generator,
        "band_colors": summarizer.BAND_COLORS,
        "shifts": list(re_.SHIFTS.keys()),
        "reports": query(
            "SELECT id, shift, generated_at, generator, risk_count, critical_count,"
            " emailed_to, email_status FROM shift_reports ORDER BY id DESC LIMIT 10"
        ),
        "emails": query(
            "SELECT sent_at, kind, recipients, subject, status, detail"
            " FROM email_log ORDER BY id DESC LIMIT 10"
        ),
        "email_enabled": settings.email_enabled,
        "email_to": ", ".join(passdown_recipients()),
        "email_transport": settings.email_transport,
        "email_redirect_to": settings.email_redirect_to,
        "cron": f"{settings.passdown_cron_hours} @ :{settings.passdown_cron_minute:02d}",
    }

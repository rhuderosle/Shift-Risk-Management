"""SMTP email delivery with a safe file-based outbox fallback."""

from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from .config import settings
from .db import execute, utcnow

log = logging.getLogger(__name__)


def _log_email(kind: str, recipients: list[str], subject: str, status: str,
               detail: str = "", actor: str = "") -> None:
    execute(
        "INSERT INTO email_log (sent_at, kind, recipients, subject, status, detail, actor)"
        " VALUES (?,?,?,?,?,?,?)",
        (utcnow(), kind, ", ".join(recipients), subject, status, detail, actor),
    )


def _write_outbox(subject: str, html_body: str) -> Path:
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", subject)[:80]
    path = settings.outbox_dir / f"{utcnow().replace(':', '-')}_{slug}.html"
    path.write_text(html_body, encoding="utf-8")
    return path


def send_email(kind: str, recipients: list[str], subject: str,
               html_body: str, text_body: str, actor: str = "") -> dict[str, str]:
    """Send an email. Returns {'status': sent|file|error, 'detail': ...}."""
    if not recipients:
        _log_email(kind, [], subject, "error", "no recipients configured", actor)
        return {"status": "error", "detail": "no recipients configured"}

    redirect = [a.strip() for a in settings.email_redirect_to.split(",") if a.strip()]
    if redirect:
        subject = f"[TEST -> {', '.join(recipients)}] {subject}"
        recipients = redirect

    if not settings.email_enabled:
        path = _write_outbox(subject, html_body)
        _log_email(kind, recipients, subject, "file", str(path), actor)
        log.info("EMAIL_ENABLED=false -> wrote %s", path)
        return {"status": "file", "detail": str(path)}

    if settings.email_transport.lower() == "outlook":
        from .mail_outlook import send_via_outlook

        result = send_via_outlook(recipients, subject, html_body)
        _log_email(kind, recipients, subject, result["status"],
                   result.get("detail", ""), actor)
        if result["status"] != "sent":
            log.error("Outlook send failed: %s", result.get("detail"))
        return result

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.email_from
    msg["To"] = ", ".join(recipients)
    if actor and settings.email_reply_to_actor and "@" in actor:
        # Replies go to the person who triggered it, not the shared service mailbox.
        msg["Reply-To"] = actor
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as srv:
            if settings.smtp_use_tls:
                srv.starttls()
            if settings.smtp_user:
                srv.login(settings.smtp_user, settings.smtp_password)
            srv.send_message(msg)
        _log_email(kind, recipients, subject, "sent", "", actor)
        return {"status": "sent", "detail": ""}
    except Exception as exc:  # noqa: BLE001
        log.exception("email send failed")
        _log_email(kind, recipients, subject, "error", str(exc), actor)
        return {"status": "error", "detail": str(exc)}

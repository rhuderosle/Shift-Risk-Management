"""Send mail through the local Outlook profile via COM automation.

Used instead of SMTP when EMAIL_TRANSPORT=outlook. Intel's SMTP endpoints reject
basic auth, so this sends as the logged-on Windows user with no stored password.

Requires Outlook installed and signed in as the account running the app; it will
not work from a service account or a headless host.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

# The body is passed via a UTF-8 temp file rather than inline, so HTML quoting and
# non-ASCII characters can't corrupt the PowerShell command line.
PS_SEND = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  $html = [IO.File]::ReadAllText('__BODYFILE__', [Text.Encoding]::UTF8)
  $ol = New-Object -ComObject Outlook.Application
  $mail = $ol.CreateItem(0)
  $mail.Subject = [IO.File]::ReadAllText('__SUBJFILE__', [Text.Encoding]::UTF8)
  $mail.To = '__TO__'
  $mail.HTMLBody = $html
  __FROMLINE__
  $mail.Send()
  ConvertTo-Json -Compress @{ status = 'sent' }
} catch {
  ConvertTo-Json -Compress @{ error = $_.Exception.Message }
}
"""


def send_via_outlook(recipients: list[str], subject: str, html_body: str) -> dict[str, str]:
    """Send one HTML mail. Returns {'status': 'sent'} or {'status': 'error', ...}."""
    tmp = Path(tempfile.mkdtemp(prefix="shiftrisk_mail_"))
    body_file = tmp / "body.html"
    subj_file = tmp / "subject.txt"
    try:
        body_file.write_text(html_body, encoding="utf-8")
        subj_file.write_text(subject, encoding="utf-8")

        from_line = ""
        if settings.outlook_send_account:
            # Pick the matching account so the mail leaves the intended mailbox.
            acct = settings.outlook_send_account.replace("'", "''")
            from_line = (
                "foreach ($a in $ol.Session.Accounts) {"
                f" if ($a.SmtpAddress -eq '{acct}') {{ $mail.SendUsingAccount = $a }} }}"
            )

        script = (
            PS_SEND.replace("__BODYFILE__", str(body_file).replace("'", "''"))
            .replace("__SUBJFILE__", str(subj_file).replace("'", "''"))
            .replace("__TO__", "; ".join(recipients).replace("'", "''"))
            .replace("__FROMLINE__", from_line)
        )

        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
            capture_output=True,
            timeout=settings.outlook_timeout_seconds,
        )
        raw = proc.stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not raw:
            return {
                "status": "error",
                "detail": f"Outlook send failed (rc={proc.returncode}): "
                          f"{proc.stderr.decode(errors='replace')[:300]}",
            }
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"status": "error", "detail": f"unexpected Outlook output: {raw[:200]}"}

        if "error" in data:
            return {"status": "error", "detail": str(data["error"])[:300]}
        return {"status": "sent", "detail": "via Outlook"}
    finally:
        for f in (body_file, subj_file):
            f.unlink(missing_ok=True)
        tmp.rmdir()

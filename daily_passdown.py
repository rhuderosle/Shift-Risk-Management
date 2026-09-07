"""Standalone daily passdown job.

Runs the full pipeline once and exits — no web server, no host required:

    sync MMS  ->  sync Outlook  ->  build report  ->  email it

This is the "no hosting" delivery model. Colleagues never visit a website; they
receive a self-contained HTML email with clickable links back to MMS and to the
original Outlook items. Schedule it with deploy/install-task.ps1.

Exit codes: 0 = emailed, 1 = failed. Connector failures are non-fatal (the
report is still built from whatever data is already in the database), because a
partial passdown is far more useful than none.
"""
from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

LOG_DIR = Path(__file__).resolve().parent / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"passdown-{datetime.now():%Y%m}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("daily_passdown")


def _sync(name: str, fn) -> None:
    """Run a connector; never let its failure abort the passdown."""
    try:
        result = fn()
        log.info("%s sync ok: %s", name, result)
    except Exception as exc:  # noqa: BLE001 - deliberately broad
        log.warning("%s sync FAILED (continuing with existing data): %s", name, exc)


def main(shift: str | None = None) -> int:
    log.info("=" * 68)
    log.info("Daily passdown starting (shift=%s)", shift or "auto")

    from app.db import init_db
    from app.config import settings

    init_db()

    if settings.mms_sync_enabled:
        from app.connectors.mms import sync as sync_mms
        _sync("MMS", sync_mms)
    else:
        log.info("MMS sync disabled")

    if settings.outlook_enabled:
        from app.connectors.outlook import sync as sync_outlook
        _sync("Outlook", sync_outlook)
    else:
        log.info("Outlook sync disabled")

    from app.reporting import send_passdown

    result = send_passdown(shift=shift, actor="scheduled-task")
    log.info(
        "Report %s | shift %s | to=%s | status=%s | %s",
        result.get("report_id"), result.get("shift"),
        ", ".join(result.get("recipients") or []) or "(none)",
        result.get("status"), result.get("detail", ""),
    )

    if result.get("status") == "sent":
        log.info("Daily passdown COMPLETE")
        return 0

    log.error("Passdown was NOT sent (status=%s). Check EMAIL_ENABLED and "
              "that Outlook is running if EMAIL_TRANSPORT=outlook.",
              result.get("status"))
    return 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        raise SystemExit(main(arg))
    except SystemExit:
        raise
    except Exception:
        log.error("Daily passdown crashed:\n%s", traceback.format_exc())
        raise SystemExit(1)

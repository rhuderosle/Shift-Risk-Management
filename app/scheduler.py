"""APScheduler jobs that automate passdown emails and critical-risk escalation."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .reporting import scan_and_escalate, send_passdown

log = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone="UTC")


def _passdown_job() -> None:
    try:
        result = send_passdown()
        log.info("scheduled passdown: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("scheduled passdown failed")


def _escalation_job() -> None:
    try:
        result = scan_and_escalate()
        if result.get("escalated"):
            log.info("escalation triggered: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("escalation scan failed")


def _mms_sync_job() -> None:
    try:
        from .connectors import mms

        result = mms.sync()
        log.info("scheduled MMS sync: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("scheduled MMS sync failed")


def _outlook_sync_job() -> None:
    try:
        from .connectors import outlook

        result = outlook.sync()
        log.info("scheduled Outlook sync: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("scheduled Outlook sync failed")


def _hdmx_sync_job() -> None:
    try:
        from .connectors import hdmx

        result = hdmx.sync()
        log.info("scheduled HDMX sync: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("scheduled HDMX sync failed")


def start_scheduler() -> None:
    if not settings.scheduler_enabled or scheduler.running:
        return
    if settings.passdown_auto_send:
        scheduler.add_job(
            _passdown_job,
            CronTrigger(hour=settings.passdown_cron_hours, minute=settings.passdown_cron_minute),
            id="shift_passdown", replace_existing=True, misfire_grace_time=600,
        )
    if settings.escalation_auto_send:
        scheduler.add_job(
            _escalation_job,
            IntervalTrigger(minutes=max(1, settings.escalation_scan_minutes)),
            id="critical_escalation", replace_existing=True, misfire_grace_time=120,
        )
    if settings.mms_sync_enabled:
        scheduler.add_job(
            _mms_sync_job,
            IntervalTrigger(minutes=max(5, settings.mms_sync_minutes)),
            id="mms_sync", replace_existing=True, misfire_grace_time=300,
        )
    if settings.outlook_sync_enabled:
        scheduler.add_job(
            _outlook_sync_job,
            IntervalTrigger(minutes=max(5, settings.outlook_sync_minutes)),
            id="outlook_sync", replace_existing=True, misfire_grace_time=300,
        )
    if settings.hdmx_sync_enabled:
        scheduler.add_job(
            _hdmx_sync_job,
            IntervalTrigger(minutes=max(5, settings.hdmx_sync_minutes)),
            id="hdmx_sync", replace_existing=True, misfire_grace_time=300,
        )
    scheduler.start()
    log.info("scheduler started: passdown auto-send=%s (hours %s), escalation auto-send=%s (every %s min)",
             settings.passdown_auto_send, settings.passdown_cron_hours,
             settings.escalation_auto_send, settings.escalation_scan_minutes)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def jobs_info() -> list[dict[str, str]]:
    if not scheduler.running:
        return []
    return [
        {"id": j.id, "next_run": str(getattr(j, "next_run_time", "") or "")}
        for j in scheduler.get_jobs()
    ]

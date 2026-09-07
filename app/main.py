from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import settings
from . import auth
from .db import execute, init_db, query, set_setting, utcnow
from .models import ChatRequest, IngestBatch, RiskIn, RiskUpdate
from .connectors import mms, outlook
from . import chat as chat_agent
from .reporting import build_report, dashboard_context, scan_and_escalate, send_passdown
from .scheduler import jobs_info, start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    auth.startup_check()
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title=settings.app_title, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# Central access control. Applying this as middleware rather than per-route means a
# newly added endpoint is protected by default instead of silently public.
PUBLIC_PATHS = {"/healthz"}


@app.middleware("http")
async def access_control(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    try:
        # Any non-GET is treated as state-changing and requires write permission.
        ident = (auth.current_user(request) if request.method in ("GET", "HEAD")
                 else auth.require_admin(request))
    except HTTPException as exc:
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
    request.state.user = ident
    return await call_next(request)


# --------------------------------------------------------------- dashboard
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    ctx = dashboard_context()
    ctx["jobs"] = jobs_info()
    ident = request.state.user
    ctx["current_user"] = ident.username
    ctx["is_admin"] = ident.is_admin
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.post("/risks/form")
def create_risk_form(
    title: str = Form(...),
    description: str = Form(""),
    area: str = Form("General"),
    owner: str = Form(""),
    shift: str = Form("1"),
    severity: int = Form(3),
    likelihood: int = Form(3),
    status: str = Form("open"),
    impact_units: float = Form(0.0),
    downtime_minutes: float = Form(0.0),
    action: str = Form(""),
):
    risk = RiskIn(
        title=title, description=description, area=area, owner=owner, shift=shift,
        severity=severity, likelihood=likelihood, status=status,  # type: ignore[arg-type]
        impact_units=impact_units, downtime_minutes=downtime_minutes, action=action,
        source="dashboard",
    )
    _insert_risk(risk)
    return RedirectResponse("/", status_code=303)


@app.post("/risks/{risk_id}/status")
def set_status_form(risk_id: int, status: str = Form(...)):
    if status not in ("open", "mitigating", "closed"):
        raise HTTPException(400, "invalid status")
    execute("UPDATE risks SET status = ?, updated_at = ? WHERE id = ?", (status, utcnow(), risk_id))
    return RedirectResponse("/", status_code=303)


@app.post("/risks/{risk_id}/delete")
def delete_risk_form(risk_id: int):
    execute("DELETE FROM risks WHERE id = ?", (risk_id,))
    return RedirectResponse("/", status_code=303)


@app.post("/actions/send-passdown")
def action_send_passdown(request: Request, shift: str = Form("")):
    send_passdown(shift or None, actor=request.state.user.username)
    return RedirectResponse("/", status_code=303)


@app.post("/actions/save-recipients")
def action_save_recipients(email_to: str = Form("")):
    cleaned = ", ".join(a.strip() for a in email_to.split(",") if a.strip())
    set_setting("email_to", cleaned)
    return RedirectResponse("/", status_code=303)


@app.post("/actions/escalate")
def action_escalate(request: Request):
    scan_and_escalate(actor=request.state.user.username)
    return RedirectResponse("/", status_code=303)


@app.post("/actions/sync-mms")
def action_sync_mms():
    try:
        mms.sync()
    except Exception as exc:  # noqa: BLE001 - surface failure on the dashboard
        logging.getLogger(__name__).error("MMS sync failed: %s", exc)
    return RedirectResponse("/", status_code=303)


@app.post("/api/mms/sync")
def api_sync_mms(shift: str | None = None):
    try:
        return mms.sync(shift)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"MMS sync failed: {exc}") from exc


@app.get("/api/mms/preview")
def api_preview_mms(shift: str | None = None):
    """Parse MMS without writing anything — useful for tuning inference rules."""
    from .risk_engine import current_shift

    try:
        sections = mms.parse_sections(mms.fetch_passdown_html())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"MMS fetch failed: {exc}") from exc
    return {
        "sections": [
            {"index": s.index, "title": s.title, "chars": len(s.text),
             "updated_by": s.updated_by, "updated_at": s.updated_at}
            for s in sections
        ],
        "candidates": mms.sections_to_risks(sections, shift or current_shift()),
    }


@app.get("/api/metrics/lipas")
def api_metrics_lipas(section: str = ""):
    """LIPAS attainment extracted from stored passdown tables."""
    from . import metrics

    return metrics.lipas_report(section)


@app.get("/api/metrics/vpo")
def api_metrics_vpo():
    """VPO miss signals and scheduled/completed rows from the passdown."""
    from . import metrics

    return metrics.vpo_report()


@app.get("/api/metrics/focus")
def api_metrics_focus():
    """The full shift-review agenda: LIPAS, VPO, USDT, hot swap, conversion, HDMX DT."""
    from . import metrics, summarizer

    return {"bullets": summarizer.focus_bullets(), "detail": metrics.focus_report()}


@app.get("/api/metrics/search")
def api_metrics_search(term: str):
    """Free-text lookup across full passdown text."""
    from . import metrics

    if not term.strip():
        raise HTTPException(400, "term is required")
    return {"term": term, "hits": metrics.find_metric(term)}


@app.get("/api/hdmx/status")
def api_hdmx_status():
    """Is the HDMX file share reachable, and which files are present?"""
    from .connectors import hdmx

    return hdmx.share_status()


@app.get("/api/hdmx/focus")
def api_hdmx_focus():
    """USDT, hot swap and waiting-for-spare read straight from the HDMX CSVs."""
    from .connectors import hdmx

    try:
        return hdmx.focus_report()
    except hdmx.ShareUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/hdmx/sync")
def api_hdmx_sync():
    from .connectors import hdmx

    try:
        return hdmx.sync()
    except hdmx.ShareUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"HDMX read failed: {exc}") from exc


@app.post("/actions/sync-hdmx")
def action_sync_hdmx():
    from .connectors import hdmx

    try:
        hdmx.sync()
    except Exception as exc:  # noqa: BLE001 - surface on the dashboard, don't 500
        logging.getLogger(__name__).error("HDMX sync failed: %s", exc)
    return RedirectResponse("/", status_code=303)


@app.post("/actions/sync-outlook")
def action_sync_outlook():
    allowed, why = auth.outlook_read_allowed()
    if not allowed:
        logging.getLogger(__name__).warning("Outlook sync refused: %s", why)
        return RedirectResponse("/", status_code=303)
    try:
        outlook.sync()
    except Exception as exc:  # noqa: BLE001 - surface failure on the dashboard
        logging.getLogger(__name__).error("Outlook sync failed: %s", exc)
    return RedirectResponse("/", status_code=303)


@app.post("/api/outlook/sync")
def api_sync_outlook(shift: str | None = None, hours: int | None = None,
                     folder: str | None = None):
    allowed, why = auth.outlook_read_allowed()
    if not allowed:
        raise HTTPException(409, why)
    try:
        return outlook.sync(shift, hours, folder)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Outlook sync failed: {exc}") from exc


@app.get("/api/outlook/preview")
def api_preview_outlook(shift: str | None = None, hours: int | None = None,
                        folder: str | None = None):
    """Show what Outlook mail would be imported, without writing anything."""
    from .risk_engine import current_shift

    allowed, why = auth.outlook_read_allowed()
    if not allowed:
        raise HTTPException(409, why)
    try:
        mails = outlook.fetch_mail(hours=hours, folder=folder)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Outlook read failed: {exc}") from exc
    candidates = outlook.mail_to_risks(mails, shift or current_shift())
    return {
        "mails_scanned": len(mails),
        "candidates": candidates,
        "excluded_sample": [
            {"subject": m.subject, "sender": m.sender}
            for m in mails if not outlook.is_risk_relevant(m)
        ][:15],
    }


@app.get("/reports/{report_id}", response_class=HTMLResponse)
def view_report(report_id: int):
    rows = query("SELECT summary_html FROM shift_reports WHERE id = ?", (report_id,))
    if not rows:
        raise HTTPException(404, "report not found")
    return HTMLResponse(rows[0]["summary_html"])


# --------------------------------------------------------------------- API
def _insert_risk(r: RiskIn) -> int:
    now = utcnow()
    return execute(
        "INSERT INTO risks (title, description, area, source, owner, shift, severity,"
        " likelihood, status, impact_units, downtime_minutes, action, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r.title, r.description, r.area, r.source, r.owner, r.shift, r.severity,
         r.likelihood, r.status, r.impact_units, r.downtime_minutes, r.action, now, now),
    )


@app.get("/api/risks")
def api_list_risks(include_closed: bool = True):
    from .risk_engine import fetch_risks

    return fetch_risks(include_closed=include_closed)


@app.post("/api/risks", status_code=201)
def api_create_risk(risk: RiskIn):
    return {"id": _insert_risk(risk)}


@app.patch("/api/risks/{risk_id}")
def api_update_risk(risk_id: int, patch: RiskUpdate):
    fields = {k: v for k, v in patch.model_dump(exclude_none=True).items()}
    if not fields:
        raise HTTPException(400, "no fields to update")
    sets = ", ".join(f"{k} = ?" for k in fields)
    execute(
        f"UPDATE risks SET {sets}, updated_at = ? WHERE id = ?",
        (*fields.values(), utcnow(), risk_id),
    )
    return {"updated": risk_id, "fields": list(fields)}


@app.delete("/api/risks/{risk_id}")
def api_delete_risk(risk_id: int):
    execute("DELETE FROM risks WHERE id = ?", (risk_id,))
    return {"deleted": risk_id}


@app.post("/api/ingest", status_code=201)
def api_ingest(batch: IngestBatch):
    """Automated data aggregation endpoint for upstream systems."""
    ids = []
    for r in batch.risks:
        r.source = r.source or batch.source
        ids.append(_insert_risk(r))
    return {"ingested": len(ids), "ids": ids}


@app.get("/api/summary")
def api_summary(shift: str | None = None):
    rep = build_report(shift)
    return {
        "report_id": rep["id"], "shift": rep["shift"], "generator": rep["generator"],
        "period_start": rep["period_start"].isoformat(),
        "period_end": rep["period_end"].isoformat(),
        "bullets": rep["bullets"], "aggregate": {
            k: v for k, v in rep["aggregate"].items() if k not in ("critical", "top")
        },
    }


@app.post("/api/passdown/send")
def api_send_passdown(request: Request, shift: str | None = None):
    return send_passdown(shift, actor=request.state.user.username)


@app.post("/api/escalate")
def api_escalate(request: Request):
    return scan_and_escalate(actor=request.state.user.username)


@app.get("/api/jobs")
def api_jobs():
    return {"scheduler_enabled": settings.scheduler_enabled, "jobs": jobs_info()}


@app.get("/api/email-log")
def api_email_log(limit: int = 50):
    return query("SELECT * FROM email_log ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz():
    return "ok"


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    history = [{"role": m.role, "content": m.content} for m in req.history]
    return chat_agent.answer(req.message, history)

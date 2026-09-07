"""Conversational assistant that answers questions about current shift risk data.

Two modes, same grounding:
  * rules  — deterministic intent matching over the live risk register (default)
  * llm    — when LLM_PROVIDER is configured, the model answers using a compact
             data context built from the same register, so replies stay grounded.

The assistant never invents risks: every answer is derived from rows in the database.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .config import settings
from . import risk_engine as re_
from . import metrics
from .db import query

log = logging.getLogger(__name__)

CHAT_SYSTEM_PROMPT = (
    "You are the Shift Risk Management assistant for a semiconductor test floor. "
    "Answer ONLY from the shift risk data provided in the user message. If the data "
    "does not contain the answer, say so plainly rather than guessing. Be concise "
    "(under 120 words), specific, and quote the numbers given. No markdown headers."
)

SUGGESTIONS = [
    "Give me the shift review focus",
    "What is the LIPAS for class test?",
    "Any VPO miss?",
    "USDT by area",
    "Hot swap trend and waiting for spare",
    "HDMX DT trend",
]


def _fmt_risk(r: dict[str, Any], with_action: bool = False) -> str:
    line = (
        f"[{r['band']}] {r['title']} — {r['area']}, "
        f"owner {r['owner'] or 'unassigned'}, score {r['score']}"
    )
    if with_action and r.get("action"):
        line += f". Next action: {r['action']}"
    return line


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {ln}" for ln in lines)


# ------------------------------------------------------- metric handlers
def _answer_lipas(m: str) -> str:
    """LIPAS attainment, read from the stored passdown tables."""
    hint = ""
    if re.search(r"\bclass\s*test\b", m):
        hint = "class test"
    elif re.search(r"\b(bi|burn\s*in)\b", m):
        hint = "BI"
    elif re.search(r"\bppv\b", m):
        hint = "PPV"

    secs = metrics.lipas_report(hint)["sections"]
    if not secs:
        return (
            "I don't have LIPAS figures loaded"
            + (f" for {hint}" if hint else "")
            + ". Run 'Sync MMS Passdown' on the dashboard, then ask again."
        )

    out: list[str] = []
    for s in secs:
        total = s["grand_total"]
        if total:
            vals = ", ".join(f"{k}: {v}" for k, v in total.items())
            out.append(f"{s['section']} — Grand Total {vals}")

        misses = []
        for p in s["products"]:
            nums = [n for n in (metrics._pct(v) for k, v in p.items() if k != "product")
                    if n is not None]
            if nums and min(nums) < 100:
                misses.append(p)

        if misses:
            names = ", ".join(
                f"{p['product']} ("
                + ", ".join(f"{k}={v}" for k, v in p.items() if k != "product") + ")"
                for p in misses[:5]
            )
            out.append(f"{len(misses)} product(s) below 100%: {names}")
        elif total:
            out.append(f"All {len(s['products'])} reporting products are at 100%.")

        if not total and s["products"]:
            real = [p for p in s["products"]
                    if any(metrics._pct(v) is not None
                           for k, v in p.items() if k != "product")]
            if real:
                shown = [f"{p['product']}: "
                         + ", ".join(f"{k}={v}" for k, v in p.items() if k != "product")
                         for p in real[:6]]
                out.append(f"{s['section']} —\n" + _bullets(shown))

    return "\n".join(out) if out else "LIPAS columns were found but held no values."


def _fmt_vpo_miss(x: dict) -> str:
    """One miss line: heading, affected products and quantities."""
    prods = ", ".join(f"{p['product']} ({p['qty']})" for p in x.get("products", []))
    base = f"{x['section']}: {x['detail']}"
    return f"{base} — {prods}" if prods else base


def _answer_vpo(m: str) -> str:
    rep = metrics.vpo_report()
    misses = rep["misses"]
    templates = rep.get("templates", [])

    if re.search(r"\b(miss|missed|shortfall|not\s*meet)\b", m):
        if not misses:
            reply = "No VPO miss with affected products is listed in the current passdown."
            if templates:
                reply += (f"\n\nCaution: {len(templates)} VPO miss heading(s) are present "
                          "but their tables are blank, so this may mean 'not filled in' "
                          "rather than 'no miss'.")
            return reply
        qty = rep.get("total_miss_qty", 0)
        reply = (f"{len(misses)} VPO miss entr(y/ies) affecting {qty} unit(s):\n"
                 + _bullets([_fmt_vpo_miss(x) for x in misses[:6]]))
        if templates:
            reply += (f"\n\nAlso {len(templates)} heading(s) with a blank table "
                      f"({templates[0]['section']}) — status not stated.")
        return reply

    lines = [_fmt_vpo_miss(x) for x in misses[:4]]
    lines += [f"{r['section']} / {r['row']}: "
              + ", ".join(f"{k}={v}" for k, v in list(r["values"].items())[:4])
              for r in rep["execution"][:6]]
    if not lines:
        return "No VPO data is loaded. Run 'Sync MMS Passdown' and ask again."
    return "VPO status from the passdown:\n" + _bullets(lines)


def _answer_utz() -> str:
    hits = metrics.find_metric("UTZ")
    if not hits:
        return "No UTZ figures are loaded. Run 'Sync MMS Passdown' and ask again."
    return "UTZ mentions in the passdown:\n" + _bullets(
        [f"{h['section']}: {h['line']}" for h in hits[:8]]
    )


def _hdmx_or_none():
    """Return the hdmx connector when enabled and readable, else None."""
    from .config import settings

    if not settings.hdmx_enabled:
        return None
    try:
        from .connectors import hdmx

        if hdmx.share_status()["reachable"]:
            return hdmx
    except Exception:  # noqa: BLE001 - fall back to passdown-derived figures
        pass
    return None


def _answer_usdt() -> str:
    hdmx = _hdmx_or_none()
    if hdmx:
        try:
            d = hdmx.usdt()
            if d["available"]:
                o = d["overall"]
                out = [
                    f"Fleet USDT: {o['usdt_pct']}% unscheduled downtime "
                    f"(scheduled {o['sdt_pct']}%, production {o['production_pct']}%) "
                    f"over {d['rows']:,} tool-shifts",
                ]
                out += [
                    f"{a['area']}: {a['usdt_pct']}% ({a['down_hours']}h across {a['tools']} tools)"
                    for a in d["areas"] if a["usdt_pct"] is not None
                ]
                t = hdmx.usdt_trend()
                if len(t.get("points") or []) >= 2:
                    out.append("Trend: " + " → ".join(
                        f"WW{p['week'][-2:]} {p['usdt_pct']}%" for p in t["points"]))
                return ("USDT (from the HDMX utilisation monitor, grouped by "
                        f"{d['grouped_by']}):\n" + _bullets(out))
        except Exception:  # noqa: BLE001
            pass

    rep = metrics.usdt_by_area()
    if not rep["available"]:
        return "No USDT data is loaded. Run 'Sync MMS Passdown' and ask again."
    out = []
    for r in rep["rates"]:
        out.append(f"{r['period']}: "
                   + ", ".join(f"{k} {v}" for k, v in r.items() if k != "period"))
    for a in rep["areas"]:
        line = f"{a['area']}: {a['detail']}"
        if a["help_needed"]:
            line += f" | HELP NEEDED: {a['help_needed']}"
        out.append(line)
    return "USDT (from " + rep["section"] + "):\n" + _bullets(out)


def _answer_hot_swap(m: str) -> str:
    want_spare = re.search(r"\bspare|waiting\b", m)
    want_swap = re.search(r"\bhot\s*swap|swap|trend\b", m)

    hdmx = _hdmx_or_none()
    if hdmx:
        try:
            out: list[str] = []
            if want_swap or not want_spare:
                hs = hdmx.hot_swap_trend()
                if hs["available"]:
                    out += [
                        f"WW{w['week'][-2:]}: {w['hot_swap']:g} hot swaps, "
                        f"UDT {w['udt_pct']}%, CS score {w['cs_score_pct']}%"
                        for w in hs["weeks"]
                    ]
                    top = [p for p in hs["products"] if p["hot_swap"]][:6]
                    if top:
                        out.append("By product (latest week): " + ", ".join(
                            f"{p['product']} {p['hot_swap']:g}" for p in top))
            if want_spare or not want_swap:
                ws = hdmx.waiting_for_spare()
                if ws["available"]:
                    out.append(
                        f"Waiting for spare: {ws['count']} open event(s), "
                        f"{ws['total_hours']}h total across {ws['tools_affected']} tools")
                    out += [
                        f"{i['tool']} {i['cell']} — {i['waiting_hours']}h "
                        f"({i['problem'][:90]})"
                        for i in ws["items"][:8]
                    ]
                else:
                    out.append("Waiting for spare: no open events in CMMS.")
            if out:
                return "Hot swap / spares (from the HDMX CMMS export):\n" + _bullets(out)
        except Exception:  # noqa: BLE001
            pass

    rep = metrics.hot_swap_and_spares()
    if not rep["available"]:
        return "No hot swap / spares data is loaded. Run 'Sync MMS Passdown' and ask again."
    out = []
    if want_swap or not want_spare:
        out += [f"{t['week']}: {t['detail']}" for t in rep["trend"]]
    if want_spare or not want_swap:
        if rep["waiting"]:
            out += [f"Waiting for spare — {w['item']}: {w['status'] or 'status not stated'}"
                    for w in rep["waiting"]]
        else:
            out.append("Waiting for spare — none listed in the passdown.")
    return "Hot swap / spares:\n" + _bullets(out) if out else "No entries found."


def _answer_conversion() -> str:
    rep = metrics.conversion_status()
    if not rep["available"]:
        return "No conversion data is loaded. Run 'Sync MMS Passdown' and ask again."
    target = rep["target"] or ">95%"
    mods = ", ".join(f"{x['module']} {x['rate']}" for x in rep["modules"])
    below = [x["module"] for x in rep["modules"]
             if (metrics._pct(x["rate"]) or 0) < (metrics._pct(target) or 95)]
    tail = f"Below target: {', '.join(below)}." if below else "All modules at or above target."
    return f"Conversion success rate (target {target}): {mods}. {tail}"


def _answer_hdmx_dt() -> str:
    rep = metrics.hdmx_dt_trend()
    if not rep["available"]:
        return "No HDMX DT data is loaded. Run 'Sync MMS Passdown' and ask again."
    out = [f"{p['product']}: {p['detail']}" for p in rep["products"]]
    out += [f"{c['unit']}: {c['detail']}" for c in rep["chillers"]]
    return "HDMX DT trend and chiller status:\n" + _bullets(out)


def _answer_focus() -> str:
    from .summarizer import focus_bullets

    return "Shift review focus:\n" + _bullets(focus_bullets())


# ----------------------------------------------------------------- intents
def _intent_answer(msg: str, risks: list[dict[str, Any]], agg: dict[str, Any],
                   shift: str) -> str | None:
    m = msg.lower().strip()
    # The register is ordered by review agenda, so re-sort by score here: several
    # answers below quote "the highest risk" and must not depend on agenda order.
    active = sorted(
        (r for r in risks if r["status"] != "closed"),
        key=lambda r: r["score"], reverse=True,
    )

    def band_of(name: str) -> list[dict[str, Any]]:
        return [r for r in active if r["band"].lower() == name]

    # ---- metric lookups, answered from full passdown tables ----
    # These run first: terms like "class test" are also area names, so the area
    # intent would otherwise swallow a question about LIPAS attainment. A single
    # message may ask about several metrics ("LIPAS for class test? any VPO
    # miss?"), so matching handlers are collected and their answers joined.
    metric_parts: list[str] = []

    if re.search(r"\b(focus|agenda|shift\s*review|review\s*focus)\b", m):
        return _answer_focus()

    if re.search(r"\blipas\b", m):
        metric_parts.append(_answer_lipas(m))
    if re.search(r"\bvpo\b", m):
        metric_parts.append(_answer_vpo(m))
    if re.search(r"\busdt\b|\bunscheduled\s+down", m):
        metric_parts.append(_answer_usdt())
    if re.search(r"\bhot\s*swap\b|waiting\s+for\s+spare|\bspare[s]?\b", m):
        metric_parts.append(_answer_hot_swap(m))
    if re.search(r"\bconversion\b", m):
        metric_parts.append(_answer_conversion())
    if re.search(r"\bhdmx\b.*\b(dt|downtime|trend|chiller)\b|\bchiller\b", m):
        metric_parts.append(_answer_hdmx_dt())
    if re.search(r"\b(utz|utilization|utilisation)\b", m):
        metric_parts.append(_answer_utz())
    if metric_parts:
        return "\n\n".join(metric_parts)

    # provenance — checked early so "where is this from" doesn't match the area intent
    if re.search(r"\b(source|sources|mms|provenance|sync)\b", m) or re.search(
        r"\bwhere\b.*\b(from|come|data)\b", m
    ):
        rows = query(
            "SELECT source, COUNT(*) c, MAX(updated_at) last FROM risks"
            " WHERE status != 'closed' GROUP BY source"
        )
        if rows:
            return "Risks by source:\n" + _bullets(
                [f"{r['source']}: {r['c']} risk(s), last updated {r['last'][:16]}" for r in rows]
            )
        return "No risks are currently loaded."

    # greetings / help
    if re.fullmatch(r"(hi|hello|hey|yo|help|what can you do\??)[.!]?", m):
        return (
            f"Hi — I'm the shift risk assistant for Shift {shift}. I can answer from the "
            f"live register ({len(active)} open risks). Try:\n" + _bullets(SUGGESTIONS[:4])
        )

    # counts
    if re.search(r"\bhow many\b", m):
        if "critical" in m:
            return f"There are {len(band_of('critical'))} critical risks on Shift {shift}."
        if "high" in m:
            return f"There are {len(band_of('high'))} high risks on Shift {shift}."
        if "open" in m or "risk" in m:
            return f"There are {len(active)} open risks on Shift {shift}."

    # critical / high / top
    if "critical" in m:
        rows = band_of("critical")
        if not rows:
            return (
                f"No critical risks right now on Shift {shift}. Highest is "
                f"{_fmt_risk(active[0])}." if active else "No risks are currently logged."
            )
        return f"{len(rows)} critical risk(s):\n" + _bullets(
            [_fmt_risk(r, with_action=True) for r in rows]
        )

    if "high" in m and "highest" not in m:
        rows = band_of("high")
        return (f"{len(rows)} high risk(s):\n" + _bullets([_fmt_risk(r) for r in rows])
                if rows else "No high-band risks right now.")

    if re.search(r"\b(top|worst|highest|biggest|priorit)", m):
        rows = active[:5]
        return (f"Top {len(rows)} risks on Shift {shift}:\n"
                + _bullets([_fmt_risk(r, with_action=True) for r in rows])
                if rows else "No risks are currently logged.")

    # ownership
    if re.search(r"\b(unassigned|no owner|without owner|unowned)\b", m):
        rows = [r for r in active if not r["owner"]]
        return (f"{len(rows)} risk(s) have no owner:\n" + _bullets([_fmt_risk(r) for r in rows])
                if rows else "Every open risk has an owner assigned.")

    owner_m = re.search(r"\b(?:owned by|owner|assigned to)\s+([a-z][a-z .,'-]{1,40})", m)
    if owner_m:
        needle = owner_m.group(1).strip()
        rows = [r for r in active if needle in (r["owner"] or "").lower()]
        return (f"{len(rows)} risk(s) owned by '{needle}':\n"
                + _bullets([_fmt_risk(r) for r in rows])
                if rows else f"No open risks are owned by '{needle}'.")

    # needs review (MMS-inferred scoring)
    if re.search(r"\b(needs? review|review|unverified|inferred)\b", m):
        rows = [r for r in active if r.get("needs_review")]
        if not rows:
            return "Nothing is pending review — all open risks have confirmed scoring."
        return (
            f"{len(rows)} risk(s) have auto-inferred scoring and need a human check "
            f"(imported from MMS):\n" + _bullets([_fmt_risk(r) for r in rows[:8]])
        )

    # downtime / impact
    if re.search(r"\b(downtime|impact|units|exposure|minutes)\b", m):
        top = [r for r in active if (r.get("downtime_minutes") or 0) > 0][:5]
        base = (
            f"Cumulative exposure on Shift {shift}: "
            f"{agg['total_downtime_minutes']:.0f} downtime minutes and "
            f"{agg['total_impact_units']:.0f} impacted units."
        )
        if top:
            base += "\nContributors:\n" + _bullets(
                [f"{r['title']} — {r['downtime_minutes']:.0f} min" for r in top]
            )
        return base

    # area queries
    if re.search(r"\b(area|by area|concentration)\b", m):
        if not agg["by_area"]:
            return "No risks are logged, so there is no area breakdown yet."
        return "Risk by area:\n" + _bullets(
            [f"{a}: {c}" for a, c in list(agg["by_area"].items())[:8]]
        )

    # source-specific queries (before area matching, since an area may be named "Email")
    if re.search(r"\b(outlook|inbox|e-?mails?|mails?)\b", m):
        rows = [r for r in active if r["source"] == "Outlook"]
        if not rows:
            return "No risks have been imported from Outlook yet. Use 'Sync Outlook' on the dashboard."
        return (f"{len(rows)} risk(s) from Outlook mail:\n"
                + _bullets([_fmt_risk(r, with_action=True) for r in rows[:8]]))

    if re.search(r"\b(mms|passdown view|passdown page)\b", m):
        rows = [r for r in active if r["source"] == "MMS Passdown"]
        if not rows:
            return "No risks have been imported from MMS Passdown yet. Use 'Sync MMS Passdown' on the dashboard."
        return (f"{len(rows)} risk(s) from MMS Passdown:\n"
                + _bullets([_fmt_risk(r, with_action=True) for r in rows[:8]]))

    for area in {r["area"] for r in active}:
        if area.lower() in m:
            rows = [r for r in active if r["area"] == area]
            return (f"{len(rows)} risk(s) in {area}:\n"
                    + _bullets([_fmt_risk(r, with_action=True) for r in rows]))

    # health / status / summary
    if re.search(r"\b(health|posture|status|how are we|overall)\b", m):
        return (
            f"Shift {shift} health index is {agg['health']}/100 "
            f"(average risk score {agg['avg_score']}). "
            f"{agg['open']} open risks: {agg['by_band'].get('Critical', 0)} critical, "
            f"{agg['by_band'].get('High', 0)} high, "
            f"{agg['by_band'].get('Medium', 0)} medium, {agg['by_band'].get('Low', 0)} low."
        )

    if re.search(r"\b(summary|summarise|summarize|passdown|handover|brief|overview)\b", m):
        from .summarizer import summarize

        bullets, _ = summarize(shift, agg)
        return f"Shift {shift} executive summary:\n" + _bullets(bullets)

    # actions
    if re.search(r"\b(action|do next|next step|what should)\b", m):
        rows = [r for r in active if r.get("action")][:5]
        return (f"Priority actions for Shift {shift}:\n"
                + _bullets([f"{r['title']}: {r['action']}" for r in rows])
                if rows else "No next actions have been recorded on the open risks.")

    # free-text search across the register
    words = [w for w in re.findall(r"[a-z0-9]{4,}", m)
             if w not in {"risk", "risks", "shift", "what", "show", "tell", "about",
                          "there", "which", "have", "with", "from", "that", "this"}]
    if words:
        hits = [
            r for r in active
            if any(w in f"{r['title']} {r['description']}".lower() for w in words)
        ]
        if hits:
            return (f"{len(hits)} risk(s) mention {', '.join(words[:3])}:\n"
                    + _bullets([_fmt_risk(r, with_action=True) for r in hits[:5]]))
    return None


# --------------------------------------------------------------------- LLM
def _build_context(risks: list[dict[str, Any]], agg: dict[str, Any], shift: str) -> str:
    lines = [
        f"Current shift: {shift}",
        f"Health index: {agg['health']}/100, average score {agg['avg_score']}",
        f"Open risks: {agg['open']}; bands: {agg['by_band']}",
        f"Downtime minutes: {agg['total_downtime_minutes']}, "
        f"impacted units: {agg['total_impact_units']}",
        "Risk register (highest score first):",
    ]
    for r in risks[:25]:
        lines.append(
            f"- {r['title']} | band={r['band']} score={r['score']} area={r['area']} "
            f"owner={r['owner'] or 'unassigned'} status={r['status']} "
            f"sev={r['severity']} lik={r['likelihood']} "
            f"downtime={r['downtime_minutes']}min source={r['source']} "
            f"needs_review={r.get('needs_review', 0)} "
            f"action={r['action'] or 'none'} | {r['description'][:180]}"
        )
    return "\n".join(lines)


def _llm_answer(message: str, context: str, history: list[dict[str, str]]) -> str | None:
    provider = (settings.llm_provider or "none").lower()
    if provider not in ("openai", "azure") or not settings.openai_api_key:
        return None

    messages: list[dict[str, str]] = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for turn in history[-6:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": str(turn.get("content", ""))[:1500]})
    messages.append({
        "role": "user",
        "content": f"SHIFT RISK DATA:\n{context}\n\nQUESTION: {message}",
    })

    try:
        if provider == "azure":
            if not (settings.azure_openai_endpoint and settings.azure_openai_deployment):
                return None
            url = (
                f"{settings.azure_openai_endpoint.rstrip('/')}/openai/deployments/"
                f"{settings.azure_openai_deployment}/chat/completions"
                f"?api-version={settings.azure_openai_api_version}"
            )
            headers = {"api-key": settings.openai_api_key}
            body = {"messages": messages, "temperature": 0.2}
        else:
            url = f"{settings.openai_base_url.rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {settings.openai_api_key}"}
            body = {"model": settings.openai_model, "messages": messages, "temperature": 0.2}

        resp = httpx.post(url, json=body, headers=headers, timeout=45)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - degrade to rules
        log.warning("chat LLM call failed, falling back to rules: %s", exc)
        return None


# -------------------------------------------------------------------- entry
def answer(message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Answer a user question about the current risk picture."""
    message = (message or "").strip()
    if not message:
        return {"reply": "Ask me anything about the current shift risks.",
                "engine": "rules", "suggestions": SUGGESTIONS}

    shift = re_.current_shift()
    risks = re_.fetch_risks(include_closed=False)
    agg = re_.aggregate(risks)

    llm_reply = _llm_answer(message, _build_context(risks, agg, shift), history or [])
    if llm_reply:
        return {"reply": llm_reply, "engine": settings.llm_provider.lower(),
                "suggestions": SUGGESTIONS}

    reply = _intent_answer(message, risks, agg, shift)
    if reply is None:
        reply = (
            f"I couldn't match that to the risk data. I answer from the live register "
            f"({agg['open']} open risks on Shift {shift}). Try asking about critical risks, "
            f"owners, downtime, areas, what needs review, or ask for the shift summary."
        )
    return {"reply": reply, "engine": "rules", "suggestions": SUGGESTIONS}

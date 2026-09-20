# Shift Risk Management System Dashboard

**AI-Powered Shift Manager — Auto-Compilation & Risk Summarization Platform**

An AI agent that automatically aggregates shift risks, prioritises them, writes an executive
passdown summary, and triggers the passdown/escalation emails on a schedule — no manual
report compilation.

## What it does

| Capability | Implementation |
|---|---|
| **Automated data aggregation** | **Live MMS Passdown connector** (`app/connectors/mms.py`) plus `POST /api/ingest` for other systems and a dashboard form for manual entry |
| **Intelligent risk prioritisation** | 0–100 composite score from severity × likelihood plus downtime and unit-impact weighting, banded Critical/High/Medium/Low ([risk_engine.py](app/risk_engine.py)) |
| **AI executive summary** | Rule-based narrative agent by default; optionally OpenAI or Azure OpenAI with automatic fallback ([summarizer.py](app/summarizer.py)) |
| **Automated email triggering** | Cron-scheduled shift passdown emails + interval scan that instantly escalates new critical risks ([scheduler.py](app/scheduler.py)) |
| **Executive dashboard** | Health index, KPIs, prioritised register, area concentration, passdown history, email trigger log |
| **Chat assistant** | Ask questions about the live register in natural language ([chat.py](app/chat.py)) |

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env      # then edit .env
.\.venv\Scripts\python.exe -m app.seed          # optional demo data
.\.venv\Scripts\python.exe run.py
```

Open <http://127.0.0.1:8086/>. `run.py` honours `BIND_HOST`/`BIND_PORT` from `.env`.

### Starting it without a terminal

`launcher.py` is a double-click starter: it opens the dashboard in your browser if it's
already running, otherwise starts it detached (so closing the launcher does not stop the
server) and then opens the browser. Build it once into a standalone `.exe`:

```powershell
.\deploy\build-launcher.ps1
```

This produces `StartShiftRisk.exe` in the project root — not committed to git, since it's a
compiled binary and this repo is public. Rebuild it after pulling changes to `launcher.py`.
Pin it to the taskbar or make a desktop shortcut for one-click access.

> **Sharing this with other Intel users?** The app runs unauthenticated by default and is
> only safe on `127.0.0.1`. See [deploy/README.md](./deploy/README.md) — it also covers two
> things that *break* rather than merely weaken on a server: Outlook COM email, and the
> Outlook inbox connector.

## Configuration (`.env`)

Email is **disabled by default** — generated messages are written to `data/outbox/*.html` so you
can preview them safely. There are two ways to send for real.

**Outlook transport (recommended).** Sends through your signed-in local Outlook profile via COM,
so no password is stored anywhere. Intel's SMTP endpoints reject basic auth, so this is normally
the only route that works. It requires Outlook to be installed and running as the same Windows
user as the app — it will not work from a service account or a headless host.

```ini
EMAIL_ENABLED=true
EMAIL_TRANSPORT=outlook
OUTLOOK_SEND_ACCOUNT=you@yourco.com   # blank = default account
EMAIL_TO=shift.leads@yourco.com,ops.exec@yourco.com
EMAIL_ESCALATION_TO=ops.exec@yourco.com
# SAFETY VALVE: while set, ALL mail goes here instead of the recipients above and
# the subject is prefixed with the intended recipients. Clear it to go live.
EMAIL_REDIRECT_TO=you@yourco.com
```

**SMTP transport.** Requires credentials, and will fail on tenants with basic auth disabled:

```ini
EMAIL_ENABLED=true
EMAIL_TRANSPORT=smtp
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_USER=your-account
SMTP_PASSWORD=your-app-password
EMAIL_FROM=shift-risk-bot@yourco.com
```

Every attempt is written to the `email_log` table with status `sent`, `file`, or `error`,
along with the `actor` who triggered it (`scheduler` when automated).

**Recipients** can be edited directly on the dashboard ("Send passdown to") and are stored in
the `app_settings` table. A saved value overrides `EMAIL_TO`, so you don't need to edit `.env`
and restart to change the distribution list.

**Traceability.** Each risk stores a `source_link`, and the top-risk titles in the passdown email
are hyperlinks to it: MMS risks deep-link to their section on the passdown page, and Outlook
risks use an `outlook:<EntryID>` URL that opens the original mail item in the Outlook client.
The plain-text alternative lists the same URLs. Outlook links only resolve on a machine with
that mailbox configured, so external recipients will see the MMS links work but not the mail ones.

Automated triggers:

```ini
PASSDOWN_CRON_HOURS=7,19         # one email at each 7-to-7 handover
PASSDOWN_CRON_MINUTE=0
ESCALATION_SCAN_MINUTES=10       # scan for new critical risks every 10 min
# Automatic sending is opt-in and separate from EMAIL_ENABLED, so you can use the
# dashboard button first. Escalation mail is driven by *heuristic* scoring that is
# flagged needs_review, so leave it off until the scoring has been human-validated.
PASSDOWN_AUTO_SEND=false
ESCALATION_AUTO_SEND=false
SCHEDULER_ENABLED=true
```

Optional LLM narrative (leave `none` for the deterministic engine):

```ini
LLM_PROVIDER=openai              # or: azure
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Outlook source:

```ini
OUTLOOK_ENABLED=true
OUTLOOK_FOLDER=                  # blank = Inbox
OUTLOOK_LOOKBACK_HOURS=24
OUTLOOK_MAX_ITEMS=200
OUTLOOK_TIMEOUT_SECONDS=180
OUTLOOK_SYNC_ENABLED=true        # background sync job
OUTLOOK_SYNC_MINUTES=30
```

## MMS Passdown connector

The primary live data source is an internal MMS passdown page:

```
http://<mms-host>/PassdownView.aspx?tabid=<n>&FAID=<n>&GID=<n>
```

[mms.py](app/connectors/mms.py) fetches that page, parses each passdown template
section (title, rich-text body, "Last Updated By" owner and timestamp), and converts
sections into risk records.

```ini
MMS_PASSDOWN_URL=http://<mms-host>/PassdownView.aspx?tabid=<n>&FAID=<n>&GID=<n>
MMS_SYNC_ENABLED=true     # adds a scheduled sync job
MMS_SYNC_MINUTES=30
MMS_TIMEOUT_SECONDS=90
```

Trigger it via the **Sync MMS Passdown** dashboard button, `POST /api/mms/sync`, or the
scheduled job. Use `GET /api/mms/preview` to see what would be imported without writing.

**Authentication.** MMS requires Windows integrated auth and rejects both
`curl --negotiate` and Python's SSPI negotiate handshake with 403. The connector therefore
shells out to PowerShell's `Invoke-WebRequest -UseDefaultCredentials`, so **the app must run
as a Windows account entitled to view the passdown**.

**Idempotency.** Each risk stores an `external_ref` of `<url>#section-<N>`, unique-indexed in
SQLite. Re-syncing updates existing open rows rather than creating duplicates.

### Important caveat on inferred scoring

MMS sections are free-form rich text, so severity and likelihood are **inferred from keyword
signals**, not read from structured fields:

- Severity floors: safety/injury → 5, line-down/downtime → 4, excursion/scrap/quality-hold → 4,
  misses/backlog/delay → 3, generic risk/issue/alarm → 3.
- Likelihood: recurring/repeat → 5, open/pending/monitoring → 4, closed/resolved → 2.
- Downtime minutes are only counted from lines that also mention downtime context, so headings
  like "LAST 24HR PERFORMANCE" don't register as 1440 minutes of downtime.

These heuristics are approximations of human judgement. Every imported row is flagged
`needs_review` and shown with a **needs review** badge in the register — a shift manager should
confirm the scoring before it is trusted for escalation. Refine the signal lists in
[mms.py](app/connectors/mms.py) as you learn how your team writes passdowns.

## Outlook connector

A second source reads risk-relevant mail from your **local Outlook profile** and folds it into
the same register. Click **Sync Outlook** on the dashboard, or `POST /api/outlook/sync`.

### How it reads mail

Outlook is driven through COM automation from PowerShell (`New-Object -ComObject
Outlook.Application`), so:

- **Outlook must be installed and signed in as the same Windows user that runs this app.**
  There is no service-account or shared-mailbox mode — a headless server will not work.
- The default folder is the Inbox (`OUTLOOK_FOLDER=` blank). Set a top-level folder name to
  read somewhere else.
- Only mail received in the last `OUTLOOK_LOOKBACK_HOURS` (default 24) is scanned, capped at
  `OUTLOOK_MAX_ITEMS`.

### How mail is filtered

Most operational mail is not a risk, so a naive keyword match is far too noisy (an early pass
flagged 71 of 128 mails). The filter has three parts, all in
[outlook.py](app/connectors/outlook.py):

- `RISK_KEYWORDS` — matched against the **subject**.
- `STRONG_KEYWORD_RE` — a stricter list; a **body-only** match must hit one of these, so passing
  mentions in long threads don't create risks.
- `NOISE_RE` — explicit exclusions (ship memos, shiftly reports, approval requests, VPO requests).

Reply chains are then collapsed by `_thread_key()`, which strips repeated `Re:`/`Fw:` prefixes
and bracketed ticket IDs. Without this, one issue produced a risk per reply. A thread with 3 or
more replies gets a **+1 likelihood** bump, on the assumption that churn means it is unresolved.

Use `GET /api/outlook/preview` to see what would be imported without writing anything. It also
returns an `excluded_sample` so you can check for false negatives before tuning the lists.

### Caveats

Severity and likelihood are inferred with the same keyword heuristics as the MMS connector, so
every imported mail is flagged `needs_review`. Re-syncing is idempotent: risks are keyed on
`external_ref = outlook:thread:<key>`, so an existing risk is updated rather than duplicated.

## HSD-ES connector ("HSD DT Latest")

A read-only connector reads a saved HSD-ES community query and surfaces tools/cells that have
opened **repeat** downtime tickets, so they can be called out for follow-up instead of getting
lost among one-off entries. Click **Sync HSD-ES (DT Latest)** on the dashboard, or
`POST /api/hsdes/sync`.

- Set `HSDES_QUERY_ID` to your saved query's numeric id (from the HSD-ES query URL/execution
  link). Left blank by default so this connector stays opt-in.
- Authenticated with your Windows identity the same way as the MMS connector (`Invoke-WebRequest
  -UseDefaultCredentials` via PowerShell) — plain username/password isn't supported by the API.
- The query itself has no dedicated tool/cell column, so the identifier is extracted from the
  free-text title (e.g. `"HDMX 7168 TOOL DOWN"` → `7168`) and records are grouped by it. A
  tool/cell is called out once it has `HSDES_MIN_REPEATS` (default 2) or more tickets.
- Each PowerShell round-trip costs a few seconds, so the result is cached for
  `HSDES_CACHE_SECONDS` (default 300s) rather than re-fetched on every dashboard render. The
  **Sync HSD-ES** button always forces a fresh fetch.
- Nothing is written to the local database — this is read-only, same as HDMX.

## Shift review focus

The review runs against a fixed agenda, so the app is built around it:

| Topic | Read from |
|---|---|
| LIPAS | `CLASS TEST` / `PPV` / `BI OPERATION UPDATE` attainment tables |
| USDT by area | `MEOS UPDATE` — rate table plus per-area handler/collateral issues |
| Hot swap trend | `Last 24hrs Collaterals Update` — grouped by work week |
| Waiting for spare | same section, the "Waiting for Spare" table |
| Conversion | `HDMX CONVERSION STATUS` — per-module rate vs. target |
| HDMX DT trend | `HDMX DT TREND AND CHILLER STATUS` — PG12 product issues and chiller units |
| USDT (live) | HDMX utilisation CSVs, scoped to PG12 — overall %, top offending tools, trend |
| HSD DT Latest | HSD-ES saved query — tools/cells with 2+ repeat DT tickets, needs follow-up |

This drives three places at once:

- **Shift Review Focus card** on the dashboard, in agenda order.
- **Prioritised Risk Register**, which now sorts by agenda topic first and score
  second, tags each row with the topics it touches, and shows per-topic counts. The
  executive summary still headlines by score, so agenda ordering can't bury a high risk.
- **Passdown email**, which carries the same focus block in both HTML and plain text.

Ask the assistant *"give me the shift review focus"* for all of it, or a single topic
("USDT by area", "hot swap trend", "conversion status", "HDMX DT trend"). JSON is at
`GET /api/metrics/focus`.

Topics with no data say so explicitly rather than being dropped, so a missing or renamed
passdown section is visible at handover instead of silently absent. Topic tagging is a
keyword match over each risk's own text — see `FOCUS_PATTERNS` in
[risk_engine.py](app/risk_engine.py) to adjust it.

## Metric questions (LIPAS, VPO, UTZ)

Risk rows only store a 600-character summary of each passdown section, which is fine for triage
but drops the numbers buried in the passdown's wide HTML tables. The full text of every section
is therefore also stored in a `source_documents` table, and [metrics.py](app/metrics.py) reads
figures back out of it. The assistant answers questions like:

- *What is the LIPAS for class test?* → Grand Total plus any product below 100%
- *Any VPO miss?* → miss lines quoted from the passdown
- *Show me UTZ* → matching lines with their section

A single message may ask several at once ("what is the LIPAS for class test? any VPO miss?") —
each matching metric is answered in turn.

These answers are **extracted, never inferred**: figures are read verbatim from the passdown
table cells, and if a number isn't in the source the assistant says so instead of estimating.
That is the opposite of the risk *scoring*, which is heuristic — see the caveats above.

Also available as JSON: `GET /api/metrics/lipas?section=class%20test`, `GET /api/metrics/vpo`,
and `GET /api/metrics/search?term=<anything>`.

## Chat assistant

Click **💬 Ask the risk assistant** at the bottom-right of the dashboard, or call
`POST /api/chat`. It answers questions about the current risk picture:

- "What are the critical risks?" / "How many open risks?"
- "Which risks have no owner?" / "What's owned by Ismail?"
- "What needs review?" — lists MMS rows with auto-inferred scoring
- "Show downtime exposure" / "Risk by area" / "What's the shift health?"
- "Give me the shift summary" — runs the same summarizer used for the passdown email
- "Where does this data come from?" — source and last-sync breakdown
- Any other keywords fall back to a free-text search across titles and descriptions

**Grounding.** Answers are derived only from rows in the database. With `LLM_PROVIDER=none`
(the default) it uses deterministic intent matching, so it cannot hallucinate. If you configure
an LLM, the model is given a compact context built from the same register and instructed to
answer only from it — but as with any LLM, treat its wording as a summary to verify, not
gospel. It falls back to the rule-based engine if the API call fails.

The dashboard's 120-second auto-refresh pauses while the chat panel is open so a conversation
is never interrupted.

```json
POST /api/chat
{"message": "what are the critical risks?", "history": []}
```

## Scoring model

```
score = 70 × (severity × likelihood)/25
      + 20 × min(downtime_minutes/240, 1)
      + 10 × min(impact_units/5000, 1)
```

Status multipliers: `mitigating` ×0.85, `closed` ×0.25.
Bands: Critical ≥75, High ≥50, Medium ≥25, else Low.
Shift health index = `100 − avg_score − 5 × critical_count`, clamped 0–100.

Shift windows are defined in `SHIFTS` in [risk_engine.py](app/risk_engine.py): four crews on a
12-hour 7-to-7 rotation — Shifts 1 and 3 are day crews (07:00–19:00), Shifts 2 and 4 are night
crews (19:00–07:00).

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/api/risks` | List enriched, prioritised risks |
| POST | `/api/risks` | Create one risk |
| PATCH | `/api/risks/{id}` | Update fields |
| DELETE | `/api/risks/{id}` | Remove a risk |
| POST | `/api/ingest` | Batch ingest from upstream systems |
| POST | `/api/mms/sync` | Fetch MMS passdown and upsert as risks |
| GET | `/api/mms/preview` | Parse MMS without writing (tune inference rules) |
| POST | `/api/outlook/sync` | Scan Outlook mail and upsert as risks |
| GET | `/api/outlook/preview` | Parse Outlook without writing (tune keyword filters) |
| GET | `/api/metrics/lipas` | LIPAS attainment from passdown tables (`?section=`) |
| GET | `/api/metrics/vpo` | VPO miss lines and scheduled/completed rows |
| GET | `/api/metrics/focus` | Full shift-review agenda (bullets + structured detail) |
| GET | `/api/metrics/search` | Free-text lookup across full passdown text (`?term=`) |
| GET | `/api/summary?shift=1` | Generate + persist an AI shift summary |
| POST | `/api/passdown/send` | Build report and trigger passdown email |
| POST | `/api/escalate` | Scan and email any un-escalated critical risks |
| GET | `/api/jobs` | Scheduler status and next run times |
| GET | `/api/email-log` | Email trigger audit trail |
| GET | `/reports/{id}` | View a stored passdown as HTML |
| GET | `/healthz` | Health probe |
| POST | `/api/chat` | Ask the assistant about current risks |

Interactive docs at `/docs`.

Batch ingest example:

```json
POST /api/ingest
{
  "source": "MES",
  "risks": [
    {"title": "CDA compressor trip", "area": "Facilities", "severity": 5,
     "likelihood": 3, "downtime_minutes": 60, "action": "Switch to backup compressor"}
  ]
}
```

## Escalation behaviour

The escalation job only emails risks that are Critical **and** have `escalated_at IS NULL`,
stamping them once delivered — so a given risk escalates exactly once and the interval scan
never spams recipients.

## Notes

- Data is stored in SQLite at `data/shift_risk.db` (WAL mode).
- Every generated passdown is persisted in `shift_reports` and every send attempt in `email_log`,
  giving a full audit trail of automated triggers.
- The dashboard auto-refreshes every 120 seconds.

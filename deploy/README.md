# Deploying for multiple Intel users

The app ships in **single-user mode**: no authentication, bound to `127.0.0.1`. That is
safe on your own machine and unsafe anywhere else. This guide covers what to change.

Need a server first? See [HOSTING-REQUEST.md](./HOSTING-REQUEST.md) for a ready-made
specification to send to whoever provisions servers for your org.

## What must change, and why

| Area | Local default | Shared deployment |
|---|---|---|
| Authentication | none | IIS Windows Auth in front, `AUTH_MODE=header` |
| Email transport | `outlook` (COM) | `smtp` via a service mailbox |
| Outlook connector | reads your inbox | **disable**, unless a shared mailbox |
| Database | `data/` under OneDrive | local path, e.g. `C:\Apps\ShiftRisk\data` |
| Process | terminal | Windows Service (NSSM) |

Three of these need explanation:

1. **Outlook COM email cannot run on a server.** It needs an interactive desktop
   session with Outlook open, and sends as the host account — so every user's
   passdown would appear to come from you. The app refuses to start in this
   combination rather than failing at 07:00.
2. **The Outlook *connector* reads one specific mailbox.** On a server that is the
   service account's, not each viewer's. If you point it at a personal mailbox, its
   contents become visible to everyone using the dashboard. The app logs a warning.
3. **MMS data is fetched with the service account's Windows identity**, not each
   viewer's. In this deployment the intended audience already has MMS access to the
   same passdown page, so the dashboard is effectively a cache of data those users
   could read directly — not an escalation of privilege.

   Two things still follow from it. Restrict `AUTH_ALLOWED_USERS` to that audience,
   so the "everyone here can already see this" assumption stays true as the user list
   grows. And if you later point the connector at a different FAID/GID, re-check that
   assumption — the app cannot detect that the audience changed.

## Steps

### 1. Move off OneDrive

```powershell
robocopy "<current path>" "C:\Apps\ShiftRisk" /E
```

Set `DB_PATH=C:\Apps\ShiftRisk\data\shift_risk.db`. SQLite in WAL mode plus a file
sync client is a known corruption risk once writes are concurrent.

### 2. Configure `.env`

```ini
AUTH_MODE=header
AUTH_USER_HEADER=X-Remote-User
AUTH_ADMINS=your.wsid,shift.manager.wsid   # who may send mail and delete risks
# Restrict reads to people who already have MMS access to the configured FAID/GID.
# Leaving this empty grants any authenticated Intel user access to the cached data.
AUTH_ALLOWED_USERS=wsid1,wsid2,wsid3
BIND_HOST=127.0.0.1                        # proxy is the only ingress - do not change
BIND_PORT=8086

EMAIL_TRANSPORT=smtp
SMTP_HOST=<smtp-relay-host>
EMAIL_FROM=shift-risk-bot@example.com      # a service mailbox, not a person
EMAIL_ENABLED=true
EMAIL_REDIRECT_TO=your.name@example.com    # keep set until you've verified content

OUTLOOK_ENABLED=false
OUTLOOK_SYNC_ENABLED=false
```

### 3. Put IIS in front

Install IIS with **URL Rewrite 2.1** and **ARR**, enable the ARR proxy, then create a
site using [`web.config`](./web.config). On that site enable **Windows Authentication**
and disable **Anonymous Authentication**.

> **The one thing not to get wrong:** the app trusts `X-Remote-User` completely. It must
> be unreachable except through the proxy. Keep `BIND_HOST=127.0.0.1`, and make sure the
> `web.config` rule that overwrites the header is present — otherwise any user can forge
> another identity by sending the header themselves.

Verify: `curl http://<server>:8086/` directly from another machine should fail to
connect. If it returns HTML, the app is exposed and identity can be spoofed.

### 4. Install the service

```powershell
.\deploy\install-service.ps1 -ServiceAccount "AMR\svc-shiftrisk"
Start-Service ShiftRiskDashboard
```

Logs land in `logs\service.*.log`.

### 5. Verify before announcing

- Browse from *another* user's machine — the header shows their WSID, not yours.
- A non-admin sees the dashboard but gets 403 on send/delete.
- Send one passdown; confirm `email_log.actor` records who triggered it.
- Only then clear `EMAIL_REDIRECT_TO`.

## Scaling

SQLite is fine to roughly 50 light users. Beyond that, or if you see
`database is locked`, move to PostgreSQL. The scheduler assumes a **single instance** —
running two would double every passdown.

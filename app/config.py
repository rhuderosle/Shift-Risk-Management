from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_title: str = "Shift Risk Management System"
    db_path: str = "data/shift_risk.db"

    # ---- Multi-user access control ----
    # single_user : no auth, safe only when bound to 127.0.0.1 (default, local dev)
    # header      : trust an upstream reverse proxy (IIS/nginx) that performs
    #               Windows/Kerberos SSO and passes the user in AUTH_USER_HEADER.
    auth_mode: str = "single_user"
    auth_user_header: str = "X-Remote-User"
    # WSIDs/usernames allowed to change data (delete risks, send mail, edit settings).
    # Empty means everyone authenticated may write — fine for a small trusted team.
    auth_admins: str = ""
    # Comma-separated allowlist. Empty means any authenticated user may read.
    auth_allowed_users: str = ""
    # Refuse to start if the app is exposed on a network interface without auth.
    bind_host: str = "127.0.0.1"
    bind_port: int = 8086

    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "shift-risk-bot@example.com"
    email_to: str = ""
    email_escalation_to: str = ""
    email_enabled: bool = False
    # smtp | outlook — outlook sends through the local profile, no password needed.
    email_transport: str = "smtp"
    outlook_send_account: str = ""
    # Set Reply-To to the user who triggered the send, so replies reach a person
    # rather than the shared service mailbox.
    email_reply_to_actor: bool = True
    # Safety valve: when set, every outgoing mail goes here instead of the real
    # recipients. Lets you enable sending without mailing the whole shift team.
    email_redirect_to: str = ""

    passdown_cron_hours: str = "7,19"
    passdown_cron_minute: int = 0
    escalation_scan_minutes: int = 10
    # Automated sending is opt-in and separate from EMAIL_ENABLED, so the manual
    # dashboard button can be used before trusting the timers.
    passdown_auto_send: bool = False
    escalation_auto_send: bool = False
    scheduler_enabled: bool = True

    # Site-specific; set MMS_PASSDOWN_URL in .env. Left blank by default so the
    # connector fails loudly rather than silently pointing at the wrong site.
    mms_passdown_url: str = ""
    mms_sync_enabled: bool = False
    mms_sync_minutes: int = 30
    mms_timeout_seconds: int = 90

    outlook_enabled: bool = True
    outlook_folder: str = ""          # blank = Inbox
    outlook_lookback_hours: int = 24
    outlook_max_items: int = 200
    outlook_timeout_seconds: int = 180
    outlook_sync_enabled: bool = False
    outlook_sync_minutes: int = 30

    # ---- HDMX file-share connector -------------------------------------
    # Structured CSVs produced by the utilisation monitor and the CMMS/HSD
    # jobs. These are the same files the "HDMX Performance KPI" Power BI
    # report reads, so numbers here match that dashboard.
    hdmx_enabled: bool = True
    # Site-specific UNC path; set HDMX_SHARE in .env.
    hdmx_share: str = ""
    hdmx_sync_enabled: bool = False
    hdmx_sync_minutes: int = 60
    # How many recent work weeks of utilisation archive to read for trends.
    hdmx_trend_weeks: int = 8

    # ---- HSD-ES "HSD DT Latest" query (repeat tool-down follow-up) ------
    # Authenticated with the caller's Windows identity, same pattern as MMS.
    # Set HSDES_QUERY_ID to your saved query's numeric id (from the HSD-ES
    # community query URL). Left blank by default so this stays opt-in.
    hsdes_enabled: bool = True
    hsdes_base_url: str = "https://hsdes-api.intel.com"
    hsdes_query_id: str = ""
    hsdes_timeout_seconds: int = 60
    # A tool/cell is called out as a "repeat offender" once it has at least
    # this many DT records in the query result.
    hsdes_min_repeats: int = 2
    # A PowerShell subprocess round-trip costs ~5s, so the query result is
    # cached for this long rather than re-fetched on every dashboard render.
    hsdes_cache_seconds: int = 300

    llm_provider: str = "none"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    azure_openai_endpoint: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def outbox_dir(self) -> Path:
        return BASE_DIR / "data" / "outbox"

    def recipients(self) -> list[str]:
        return [a.strip() for a in self.email_to.split(",") if a.strip()]

    def escalation_recipients(self) -> list[str]:
        addrs = [a.strip() for a in self.email_escalation_to.split(",") if a.strip()]
        return addrs or self.recipients()

    @staticmethod
    def _norm_user(name: str) -> str:
        """Normalise DOMAIN\\user or user@intel.com down to a bare lowercase login."""
        name = (name or "").strip().lower()
        if "\\" in name:
            name = name.rsplit("\\", 1)[1]
        if "@" in name:
            name = name.split("@", 1)[0]
        return name

    def admin_users(self) -> set[str]:
        return {self._norm_user(u) for u in self.auth_admins.split(",") if u.strip()}

    def allowed_users(self) -> set[str]:
        return {self._norm_user(u) for u in self.auth_allowed_users.split(",") if u.strip()}


settings = Settings()

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS risks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT 'General',
    source TEXT NOT NULL DEFAULT 'manual',
    owner TEXT NOT NULL DEFAULT '',
    shift TEXT NOT NULL DEFAULT '1',
    severity INTEGER NOT NULL DEFAULT 3,      -- 1 (low) .. 5 (catastrophic)
    likelihood INTEGER NOT NULL DEFAULT 3,    -- 1 (rare) .. 5 (almost certain)
    status TEXT NOT NULL DEFAULT 'open',      -- open | mitigating | closed
    impact_units REAL NOT NULL DEFAULT 0,
    downtime_minutes REAL NOT NULL DEFAULT 0,
    action TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    escalated_at TEXT,
    external_ref TEXT,
    source_link TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS shift_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    generator TEXT NOT NULL DEFAULT 'rules',
    risk_count INTEGER NOT NULL DEFAULT 0,
    critical_count INTEGER NOT NULL DEFAULT 0,
    summary_html TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    emailed_to TEXT NOT NULL DEFAULT '',
    email_status TEXT NOT NULL DEFAULT 'not_sent'
);

CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL,
    kind TEXT NOT NULL,          -- passdown | escalation | test
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL,        -- sent | file | error
    detail TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT ''   -- who triggered it ('scheduler' when automated)
);

CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- MMS Passdown | Outlook
    ref TEXT NOT NULL UNIQUE,       -- stable key, e.g. <url>#section-<n>
    title TEXT NOT NULL,
    body TEXT NOT NULL,             -- full untruncated text
    updated_by TEXT NOT NULL DEFAULT '',
    shift TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_risks_status ON risks(status);
CREATE INDEX IF NOT EXISTS idx_risks_updated ON risks(updated_at);
CREATE INDEX IF NOT EXISTS idx_srcdoc_source ON source_documents(source);

-- Runtime-editable settings that override .env, so recipients can be maintained
-- from the dashboard instead of by editing files and restarting.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_file, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Lightweight migrations for databases created by earlier versions.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(risks)").fetchall()}
        if "external_ref" not in cols:
            conn.execute("ALTER TABLE risks ADD COLUMN external_ref TEXT")
        if "needs_review" not in cols:
            conn.execute("ALTER TABLE risks ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
        if "source_link" not in cols:
            conn.execute("ALTER TABLE risks ADD COLUMN source_link TEXT")
        mail_cols = {r["name"] for r in conn.execute("PRAGMA table_info(email_log)").fetchall()}
        if "actor" not in mail_cols:
            conn.execute("ALTER TABLE email_log ADD COLUMN actor TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_risks_extref"
            " ON risks(external_ref) WHERE external_ref IS NOT NULL"
        )


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or cur.rowcount


def get_setting(key: str, default: str = "") -> str:
    rows = query("SELECT value FROM app_settings WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
        " updated_at = excluded.updated_at",
        (key, value, utcnow()),
    )

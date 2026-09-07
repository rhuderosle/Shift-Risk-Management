"""Identity and access control for multi-user deployments.

Two modes:

* ``single_user`` — no authentication. Only safe when bound to 127.0.0.1; the app
  refuses to start in this mode on a public interface.
* ``header`` — trust an upstream reverse proxy (IIS with Windows Authentication,
  or nginx with Kerberos) that has already authenticated the caller and passes the
  login in ``AUTH_USER_HEADER``.

Header mode is only as trustworthy as the network path: if a client can reach the
app directly, it can forge the header. The app must be bound to localhost (or a
firewalled interface) with the proxy as the sole ingress. This is called out in
the deployment docs because it's the single easiest way to get this wrong.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from .config import settings

log = logging.getLogger(__name__)

ANONYMOUS = "anonymous"


class Identity:
    __slots__ = ("username", "is_admin", "authenticated")

    def __init__(self, username: str, is_admin: bool, authenticated: bool) -> None:
        self.username = username
        self.is_admin = is_admin
        self.authenticated = authenticated

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Identity({self.username!r}, admin={self.is_admin})"


def _resolve(request: Request) -> Identity:
    mode = settings.auth_mode.lower()

    if mode == "single_user":
        # Local single-user mode: the OS session is the security boundary.
        return Identity(ANONYMOUS, is_admin=True, authenticated=False)

    raw = request.headers.get(settings.auth_user_header, "")
    user = settings._norm_user(raw)
    if not user:
        raise HTTPException(
            status_code=401,
            detail=(
                f"Not authenticated: upstream proxy did not supply "
                f"'{settings.auth_user_header}'."
            ),
        )

    allowed = settings.allowed_users()
    if allowed and user not in allowed:
        log.warning("access denied for %s (not in AUTH_ALLOWED_USERS)", user)
        raise HTTPException(status_code=403, detail=f"{user} is not authorised for this system.")

    admins = settings.admin_users()
    # With no admin list configured, any authenticated user may write. That suits a
    # small trusted team; set AUTH_ADMINS to lock writes down.
    return Identity(user, is_admin=(not admins) or user in admins, authenticated=True)


def current_user(request: Request) -> Identity:
    """FastAPI dependency: resolve the caller, enforcing the read allowlist."""
    return _resolve(request)


def require_admin(request: Request) -> Identity:
    """FastAPI dependency for state-changing routes."""
    ident = _resolve(request)
    if not ident.is_admin:
        raise HTTPException(
            status_code=403,
            detail=f"{ident.username} does not have permission to modify this system.",
        )
    return ident


def outlook_read_allowed() -> tuple[bool, str]:
    """Whether reading the host mailbox is permissible in the current mode.

    Outlook COM always reads the mailbox of the Windows account the *server*
    process runs under -- never the browsing user's. In multi-user mode that
    means one person's inbox would be exposed to everyone, so reads are refused
    outright rather than left to configuration discipline.
    """
    if settings.auth_mode.lower() == "single_user":
        return True, ""
    return False, (
        "Outlook reads are disabled in multi-user mode. COM reads the mailbox of "
        "the account running the server, not yours, so syncing here would expose "
        "that person's mail to every user. Use a shared operational mailbox with "
        "EMAIL_TRANSPORT=smtp, or run the passdown as a scheduled task."
    )


def startup_check() -> None:
    """Refuse to serve an unauthenticated app on a network interface."""
    if settings.auth_mode.lower() == "single_user" and settings.bind_host not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise RuntimeError(
            f"Refusing to start: AUTH_MODE=single_user exposes every endpoint, but "
            f"BIND_HOST={settings.bind_host} is not loopback. Set AUTH_MODE=header "
            f"and put an authenticating reverse proxy in front, or bind to 127.0.0.1."
        )

    multi_user = settings.auth_mode.lower() != "single_user"
    if multi_user and settings.email_transport.lower() == "outlook":
        # Outlook COM needs an interactive desktop session with Outlook running, and
        # would send every user's passdown from the host owner's mailbox.
        raise RuntimeError(
            "EMAIL_TRANSPORT=outlook cannot be used in a multi-user deployment: it "
            "requires an interactive Outlook session and sends as the host user. "
            "Use EMAIL_TRANSPORT=smtp with a service mailbox instead."
        )
    if multi_user and settings.outlook_sync_enabled:
        log.warning(
            "OUTLOOK_SYNC_ENABLED=true in multi-user mode: Outlook reads are blocked "
            "at the route level because COM would read the host account's mailbox and "
            "expose it to every user. Set OUTLOOK_SYNC_ENABLED=false to silence this."
        )

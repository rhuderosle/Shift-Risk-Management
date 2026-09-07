"""Entry point that honours BIND_HOST/BIND_PORT from configuration.

Using this instead of a bare `uvicorn app.main:app --host 0.0.0.0` keeps the
bind address and the auth mode in one place, so the startup safety check in
app/auth.py can actually see the interface the server will listen on.

    python run.py
"""

from __future__ import annotations

import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )

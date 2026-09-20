"""Double-click launcher for the Shift Risk Management System.

- If the app is already running on BIND_HOST:BIND_PORT, just opens the
  dashboard in the default browser.
- Otherwise starts it as a fully detached background process (so closing
  this launcher, or even signing out, does not stop the server), waits for
  it to come up, then opens the dashboard.
- Logs to data/logs/launcher.log so a failed start can be diagnosed without
  a console window (this is built with --noconsole).
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

# When frozen by PyInstaller, __file__ points into a temporary extraction
# folder, not the real install location - use the .exe's own folder instead.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
PYTHON = APP_DIR / ".venv" / "Scripts" / "python.exe"
RUN_PY = APP_DIR / "run.py"
LOG_DIR = APP_DIR / "data" / "logs"
LOG_FILE = LOG_DIR / "launcher.log"
HOST = "127.0.0.1"
PORT = 8086
URL = f"http://{HOST}:{PORT}/"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def is_up() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5):
            return True
    except OSError:
        return False


def start_server() -> None:
    if not PYTHON.exists():
        log(f"ERROR: venv python not found at {PYTHON}")
        raise SystemExit(1)
    log(f"starting server: {PYTHON} {RUN_PY}")
    subprocess.Popen(
        [str(PYTHON), str(RUN_PY)],
        cwd=str(APP_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        close_fds=True,
    )


def main() -> None:
    if is_up():
        log("already running, opening browser")
        webbrowser.open(URL)
        return

    start_server()

    for _ in range(30):  # up to ~30s for uvicorn + scheduler startup
        time.sleep(1)
        if is_up():
            log("server responded, opening browser")
            webbrowser.open(URL)
            return

    log("ERROR: server did not come up within 30s")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # last-resort visibility for a --noconsole build
        log(f"FATAL: {type(exc).__name__}: {exc}")
        raise


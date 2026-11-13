"""Serve the website on this PC and keep its data fresh.

While this is running:
  - the page is available at http://localhost:8765
  - the cut probabilities are recalculated every few minutes
    (often while games are being played, rarely otherwise)

Stop it with Ctrl+C or by closing the window.
"""

import contextlib
import functools
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cut_odds

PORT = 8765
LIVE_MINUTES = 5    # refresh interval while any starter's game is in progress
IDLE_MINUTES = 30   # refresh interval the rest of the time
ERROR_MINUTES = 5   # wait this long before retrying after a failure

SITE_DIR = Path(__file__).resolve().parent / "site"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # don't print a line for every page request
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # always serve the newest data
        super().end_headers()


def start_web_server():
    handler = functools.partial(QuietHandler, directory=str(SITE_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def refresh():
    """Recalculate once. Returns minutes to wait before the next refresh."""
    stamp = datetime.now().strftime("%a %I:%M %p")
    try:
        with open(os.devnull, "w", encoding="utf-8") as hush, contextlib.redirect_stdout(hush):
            cut_odds.main()
    except SystemExit as stop:  # raised for login problems; the message says what to do
        print(f"[{stamp}] Refresh failed: {stop}", flush=True)
        return ERROR_MINUTES
    except Exception as error:  # e.g. no internet, ESPN hiccup - keep going and retry
        print(f"[{stamp}] Refresh failed: {type(error).__name__}: {error}", flush=True)
        return ERROR_MINUTES

    data = json.loads(cut_odds.OUTPUT_FILE.read_text(encoding="utf-8"))
    top = data["teams"][0]
    live = any(p["status"] == "playing" for t in data["teams"] for p in t["starters"])
    wait = LIVE_MINUTES if live else IDLE_MINUTES
    print(
        f"[{stamp}] Updated. Most at risk: {top['team_name']} {top['cut_probability'] * 100:.1f}%. "
        f"{'Games in progress' if live else 'No games in progress'}, next refresh in {wait} min.",
        flush=True,
    )
    return wait


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    SITE_DIR.mkdir(exist_ok=True)
    start_web_server()
    print(f"Site running at http://localhost:{PORT}  (Ctrl+C to stop)", flush=True)
    try:
        while True:
            time.sleep(refresh() * 60)
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()

"""
collectors.health_receiver — webhook for the Health Auto Export iOS app.

    POST /api/v1/health-sync   Authorization: Bearer $SPINE_HEALTH_TOKEN

Not a scheduled job: the phone pushes, so this is a small long-lived
listener instead of a cron entry. Stdlib `http.server`, not FastAPI (CLAUDE.md
5): FastAPI + uvicorn would be ~50 MB of idle RSS for one route, against a
budget of 30. One thread, one request at a time — the phone syncs a few
times a day.

Fails closed: with no SPINE_HEALTH_TOKEN it refuses to start, because a
default token in a public repo is no token. Binds 0.0.0.0:8123 by default so
the phone can reach it over Tailscale; set SPINE_HEALTH_BIND to the box's
Tailscale IP to keep it off other interfaces.

    python3 -m collectors.health_receiver
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from core import health, paths

# Registered so the registry sees it (CLAUDE.md 8), but disabled: the
# listener is pushed to, not scheduled, so it gets no crontab line. run()
# is a cheap status read, usable by hand through bin/run.sh.
META = {
    "id":          "health_receiver",
    "schedule":    "0 12 * * *",
    "timeout":     30,
    "ram_mb":      30,
    "window":      "any",
    "weight":      "light",
    "data":        "private",
    "enabled":     False,
    "description": "Health Auto Export webhook on :8123 (long-lived; not cron)",
}

ROUTE = "/api/v1/health-sync"


def run(ctx) -> dict:
    """Row counts and latest dates in var/health.db. Reads only."""
    conn = health.connect()
    try:
        return {
            "daily_vitals": conn.execute("SELECT COUNT(*) FROM daily_vitals").fetchone()[0],
            "sleep_sessions": conn.execute("SELECT COUNT(*) FROM sleep_sessions").fetchone()[0],
            "workouts": conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0],
            "latest_vitals": conn.execute("SELECT MAX(date) FROM daily_vitals").fetchone()[0],
        }
    finally:
        conn.close()
MAX_BODY = 25 * 1024 * 1024  # a multi-month backfill is a few MB


def load_token() -> str | None:
    """SPINE_HEALTH_TOKEN from the environment, else from the repo .env."""
    token = os.environ.get("SPINE_HEALTH_TOKEN")
    if token:
        return token.strip()
    try:
        with open(os.path.join(paths.root(), ".env")) as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key.strip() == "SPINE_HEALTH_TOKEN" and value:
                    return value.strip().strip("'\"")
    except OSError:
        pass
    return None


def make_handler(token: str, db_path: str | None = None):
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "spine-health"
        sys_version = ""

        def _reply(self, status: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply(404 if self.path != "/healthz" else 200,
                        {"ok": self.path == "/healthz"})

        def do_POST(self):
            if self.path.split("?")[0] != ROUTE:
                return self._reply(404, {"error": "not found"})
            auth = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(auth, expected):
                return self._reply(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self._reply(413 if length > MAX_BODY else 400,
                                   {"error": "bad content length"})
            try:
                payload = json.loads(self.rfile.read(length))
                conn = health.connect(db_path)
                try:
                    counts = health.ingest(payload, conn)
                finally:
                    conn.close()
            except (ValueError, UnicodeDecodeError) as exc:
                return self._reply(400, {"error": f"bad payload: {exc}"})
            self._reply(200, {"ok": True, "upserted": counts})

        def log_message(self, fmt, *args):
            sys.stderr.write("health_receiver: %s %s\n" % (self.address_string(), fmt % args))

    return Handler


def serve(host: str | None = None, port: int | None = None):
    token = load_token()
    if not token:
        sys.exit("health_receiver: SPINE_HEALTH_TOKEN is not set (env or .env); refusing to start")
    # Comma-separated, e.g. "127.0.0.1,100.x.y.z": loopback plus Tailscale only.
    hosts = [h.strip() for h in (host or os.environ.get("SPINE_HEALTH_BIND", "0.0.0.0")).split(",") if h.strip()]
    port = port or int(os.environ.get("SPINE_HEALTH_PORT", "8123"))
    health.connect().close()  # initialise var/health.db before the first sync
    servers = [HTTPServer((h, port), make_handler(token)) for h in hosts]
    for h in hosts:
        sys.stderr.write(f"health_receiver: listening on {h}:{port}{ROUTE}\n")
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.server_close()


if __name__ == "__main__":
    serve()

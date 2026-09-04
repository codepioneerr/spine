"""
core.http — the small amount of HTTP a collector should ever need.

stdlib urllib, no requests, no httpx. CLAUDE.md section 5: a dependency needs
a written justification, and "fetch some JSON" does not clear that bar.

Deliberately narrow: GET/POST JSON with a timeout and a real User-Agent.
Anything fancier (retries with backoff, pagination, auth flows) belongs in
the collector that needs it, where it can be read alongside the API it is
talking to.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "spine/0.1 (+https://github.com/codepioneerr/spine)"


class HttpError(RuntimeError):
    """Non-2xx, timeout, or unparseable body. Carries the status when there
    is one, because 429 and 500 mean different things to a caller."""

    def __init__(self, message, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


class Http:
    def __init__(self, timeout: int = 30, user_agent: str = USER_AGENT):
        self.timeout = timeout
        self.user_agent = user_agent

    def _open(self, req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise HttpError(f"HTTP {exc.code} for {req.full_url}",
                            status=exc.code, body=body) from exc
        except Exception as exc:
            raise HttpError(f"{type(exc).__name__}: {exc}") from exc

    def get_json(self, url, params=None, headers=None, timeout=None):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent,
                          "Accept": "application/json", **(headers or {})})
        raw = self._open(req, timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpError(f"not JSON from {url}: {raw[:200]}") from exc

    def post_json(self, url, payload, headers=None, timeout=None):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"User-Agent": self.user_agent,
                     "Content-Type": "application/json",
                     "Accept": "application/json", **(headers or {})},
            method="POST")
        raw = self._open(req, timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpError(f"not JSON from {url}: {raw[:200]}") from exc

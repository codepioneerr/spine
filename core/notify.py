"""
core.notify — hand a finished message to hermes for delivery.

Spine does not talk to Telegram. CLAUDE.md 6: hermes runs the message
gateway, it works, and rebuilding one is not on the critical path. So the
only credential Spine holds is an HMAC secret for one localhost route, and
the bot token stays where it already was. If this module ever grows a
TELEGRAM_BOT_TOKEN, something has gone wrong with that reasoning.

## The route

hermes exposes /webhooks/spine-brief, registered with --deliver-only, which
means the rendered payload is relayed verbatim to the Telegram DM with no
agent invocation and no model cost. A brief is a finished artifact by the
time it gets here; handing it to an LLM to be restated would add latency,
spend and a chance of it being reworded wrongly.

## Signing

X-Webhook-Signature-V2: hex HMAC-SHA256 over "<unix seconds>.<raw body>",
keyed with the route secret, plus X-Webhook-Timestamp. hermes also accepts a
body-only V1 signature and warns that it is replay-vulnerable; there is no
reason to send the weaker one.

## Failure is loud

send() raises. It does not return False and carry on, because the caller
that matters is a cron job at 05:40 and the only two useful outcomes are "the
brief is on the phone" and "something is written down about why it is not".
A silent False is how darkweb-jobs delivered nothing for a month without
anyone noticing -- its notify returns False when unconfigured, and its
TELEGRAM_BOT_TOKEN was never set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

# Telegram hard-limits a message at 4096 characters. Truncate below that so a
# long brief is shortened rather than rejected outright.
MAX_CHARS = 3900

URL_ENV = "SPINE_BRIEF_WEBHOOK_URL"
SECRET_ENV = "SPINE_BRIEF_WEBHOOK_SECRET"


class NotifyError(RuntimeError):
    """Delivery did not happen. Never raised for an empty message."""


class Notifier:
    """Posts a message to one signed hermes route."""

    def __init__(self, url=None, secret=None, timeout=15, secrets=None):
        get = (secrets.get if secrets is not None else os.environ.get)
        self.url = url or get(URL_ENV)
        self._secret = secret or get(SECRET_ENV)
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.url and self._secret)

    def send(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        if not self.configured():
            raise NotifyError(
                "delivery is not configured: set " + URL_ENV + " and " + SECRET_ENV
                + " in .env. Register the route first with: hermes webhook"
                + " subscribe spine-brief --deliver telegram --deliver-only")

        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n... truncated, see bin/items"

        body = json.dumps({"text": text}).encode()
        stamp = str(int(time.time()))
        sig = hmac.new(self._secret.encode(),
                       stamp.encode() + b"." + body,
                       hashlib.sha256).hexdigest()

        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json",
                     "X-Webhook-Timestamp": stamp,
                     "X-Webhook-Signature-V2": sig},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            hint = ""
            if exc.code == 401:
                hint = (" -- the route secret does not match; compare "
                        + SECRET_ENV + " against ~/.hermes/webhook_subscriptions.json")
            raise NotifyError("hermes returned HTTP " + str(exc.code) + " " + detail + hint) from exc
        except Exception as exc:
            raise NotifyError(
                "cannot reach hermes at " + str(self.url) + ": "
                + type(exc).__name__ + ": " + str(exc)
                + " -- is hermes-gateway running?") from exc

        if payload.get("status") != "delivered":
            raise NotifyError("hermes accepted the post but did not deliver: "
                              + json.dumps(payload)[:200])
        return True


def send(text: str, secrets=None) -> bool:
    """Module-level convenience for one-off sends."""
    return Notifier(secrets=secrets).send(text)

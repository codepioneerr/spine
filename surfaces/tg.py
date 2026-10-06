"""
surfaces.tg — the smallest Telegram Bot API client that does the job.

stdlib only (CLAUDE.md 5). Methods used: getUpdates (long poll),
sendMessage, editMessageReplyMarkup, answerCallbackQuery, sendPhoto,
setMyCommands, getMe. Every text is sent with parse_mode=HTML and must
already be escaped by the caller via esc() — untrusted text (headlines,
addresses, quoted user text) is never interpolated raw.

The token appears only in the request URL and is scrubbed from every
error message this module raises.
"""

from __future__ import annotations

import html
import json
import uuid
import urllib.error
import urllib.request

API = "https://api.telegram.org"
MAX_TEXT = 4096


class TgError(RuntimeError):
    pass


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def kb(rows) -> dict:
    """rows: [[(label, callback_data_or_url), ...], ...]"""
    out = []
    for row in rows:
        r = []
        for label, val in row:
            if str(val).startswith("http"):
                r.append({"text": label, "url": val})
            else:
                r.append({"text": label, "callback_data": val})
        out.append(r)
    return {"inline_keyboard": out}


def split(text: str, limit: int = MAX_TEXT - 96) -> list[str]:
    """Split on paragraph/line boundaries so HTML tags are not cut mid-line."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


class Bot:
    def __init__(self, token: str, timeout: int = 20, opener=None):
        if not token:
            raise TgError("no bot token configured")
        self._token = token
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _scrub(self, s: str) -> str:
        return s.replace(self._token, "<token>")

    def call(self, method: str, payload: dict | None = None, timeout=None, files=None):
        url = f"{API}/bot{self._token}/{method}"
        if files:
            body, ctype = _multipart(payload or {}, files)
        else:
            body, ctype = json.dumps(payload or {}).encode(), "application/json"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with self._open(req, timeout=timeout or self.timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise TgError(self._scrub(f"{method}: HTTP {exc.code} {detail}")) from None
        except Exception as exc:
            raise TgError(self._scrub(f"{method}: {type(exc).__name__}: {exc}")) from None
        if not data.get("ok"):
            raise TgError(self._scrub(f"{method}: {data.get('description')}"))
        return data["result"]

    def send(self, chat_id, text, buttons=None, reply_to=None, silent=False):
        """Sends text (split if long). Buttons attach to the LAST part.
        Returns the last message dict."""
        parts = split(text)
        msg = None
        for i, part in enumerate(parts):
            p = {"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                 "link_preview_options": {"is_disabled": True},
                 "disable_notification": bool(silent)}
            if reply_to and i == 0:
                p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if buttons and i == len(parts) - 1:
                p["reply_markup"] = buttons
            msg = self.call("sendMessage", p)
        return msg

    def photo(self, chat_id, png: bytes, caption="", buttons=None, silent=False):
        p = {"chat_id": str(chat_id), "caption": caption[:1000], "parse_mode": "HTML",
             "disable_notification": "true" if silent else "false"}
        if buttons:
            p["reply_markup"] = json.dumps(buttons)
        return self.call("sendPhoto", p, files={"photo": ("chart.png", png, "image/png")})

    def answer(self, cb_id, text="", alert=False):
        try:
            return self.call("answerCallbackQuery",
                             {"callback_query_id": cb_id, "text": text[:190], "show_alert": alert})
        except TgError:
            return None     # an expired query id must not break handling

    def clear_buttons(self, chat_id, msg_id):
        try:
            return self.call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id,
                                                        "reply_markup": {"inline_keyboard": []}})
        except TgError:
            return None

    def updates(self, offset=None, timeout=25):
        p = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            p["offset"] = offset
        return self.call("getUpdates", p, timeout=timeout + 10)


def _multipart(fields: dict, files: dict):
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out += [f"--{b}".encode(), f'Content-Disposition: form-data; name="{k}"'.encode(), b"",
                str(v).encode()]
    for k, (fname, data, ctype) in files.items():
        out += [f"--{b}".encode(),
                f'Content-Disposition: form-data; name="{k}"; filename="{fname}"'.encode(),
                f"Content-Type: {ctype}".encode(), b"", data]
    out += [f"--{b}--".encode(), b""]
    return b"\r\n".join(out), f"multipart/form-data; boundary={b}"

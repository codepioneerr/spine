"""
core.models — the router. Jobs ask for a tier; they never name a model.

Three things happen here, in this order, before any prompt leaves the box:

    1. PRIVACY  — may this job's data go to this provider at all?
    2. BUDGET   — would this call breach the monthly cap?
    3. FALLBACK — try providers in order; log every attempt.

Only the third is ordinary plumbing. The first two are refusals, and both
fail closed.

## Why the privacy gate exists

From the Sept 3 budget memo: *"nothing sensitive goes to a free endpoint,
ever."* Free endpoints sit at the back of a queue and some of them log
prompts and may train on them. That is fine for public headlines and
disqualifying for Fordham advising mail, dean correspondence, and job
applications — which is exactly what the Phase 5 email collector will read.

A rule that lives only in a document is not a safeguard. So every provider
declares `logs_prompts`, every job declares `data`, and **`data` defaults to
`private`**. A collector must actively opt in to reach a logging endpoint.
If a private job's tier resolves to nothing but logging providers, the call
**raises**. It does not quietly downgrade, and it does not warn-and-proceed.

The failure mode being designed against is not malice. It is someone adding
a collector in six months, not thinking about it, and having private mail
land on a training endpoint. Silence must fail closed.

## Why the cap is pre-flight

Same reasoning as the RAM guard: refuse cheaply rather than discover
expensively. A worst-case estimate is computed before the request, and a
call that would cross `SPINE_MONTHLY_USD_CAP` never goes out.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Sequence

from core import costs

DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TOKENS = 1024

TIER_NAMES = ("bulk", "smart", "frontier")


class ModelError(RuntimeError):
    """Any failure to obtain a completion."""


class PrivacyRefusal(ModelError):
    """A private job had no non-logging provider available. Fails closed."""


class BudgetRefusal(ModelError):
    """The call would breach the monthly cap. Refused before sending."""


class NoProviders(ModelError):
    """The tier exists but nothing is wired behind it."""


@dataclass
class Completion:
    text: str
    tokens_in: int
    tokens_out: int
    provider: str
    model: str
    latency_ms: int
    usd: float


# ─────────────────────────────────────────────────────────────────────────────
# providers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Provider:
    """One way to get a completion.

    `logs_prompts` is the field that matters. It is not about whether the
    provider is free — it is about whether the prompt leaves our control and
    might be retained or trained on. A local mock is free AND private; a free
    hosted endpoint is neither.
    """
    name: str
    model: str
    free: bool = False
    logs_prompts: bool = True
    key_env: str | None = None
    # What core.costs bills this call as, when that differs from the model
    # string sent to the API. A free tier costs $0 no matter which model it
    # serves; billing it at list rate would inflate month-to-date and make
    # the cap refuse calls that cost nothing.
    price_model: str | None = None

    @property
    def billed_as(self) -> str:
        return self.price_model or self.model

    def available(self, secrets=None) -> tuple[bool, str]:
        if self.key_env is None:
            return True, "no key required"
        val = (secrets.get(self.key_env) if secrets
               else os.environ.get(self.key_env))
        if not val:
            return False, f"{self.key_env} is not set"
        return True, "key present"

    def complete(self, prompt: str, *, max_tokens: int, timeout: int,
                 secrets=None) -> tuple[str, int, int]:
        raise NotImplementedError


class MockProvider(Provider):
    """Deterministic, offline, and — importantly — **local**.

    Nothing leaves the box, so `logs_prompts=False` and private jobs may use
    it. That is what makes the whole private path testable without a paid
    endpoint.

    Never registered in a production tier: a fallback that invents plausible
    answers is worse than an outage, for the same reason the console refuses
    to invent numbers.
    """

    def __init__(self, name="mock", reply=None, fail=False, delay=0.0):
        super().__init__(name=name, model="mock", free=True,
                         logs_prompts=False, key_env=None)
        self._reply = reply
        self._fail = fail
        self._delay = delay

    def complete(self, prompt, *, max_tokens, timeout, secrets=None):
        if self._delay:
            time.sleep(self._delay)
        if self._fail:
            raise ModelError(f"{self.name}: simulated provider failure")
        text = self._reply if self._reply is not None else f"[mock] {prompt[:60]}"
        return text, max(1, len(prompt) // 4), max(1, len(text) // 4)


class GeminiProvider(Provider):
    """Google AI Studio, free tier. Stdlib urllib — no SDK.

    Free tier: prompts may be used to improve the product. `logs_prompts` is
    True and must stay True; flipping it is how private data leaks.
    """

    ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
                "models/{model}:generateContent")

    def __init__(self, model="gemini-3.5-flash-lite",
                 key_env="GEMINI_API_KEY", name="gemini_free", free=True):
        # 2.5-flash-lite returned 404 against Nick's AI Studio key on
        # Sept 4 2026 — the model string is not universally available. 3.5
        # is what that key actually serves.
        super().__init__(name=name, model=model, free=free,
                         logs_prompts=True, key_env=key_env,
                         price_model="gemini-free-tier" if free else None)

    def complete(self, prompt, *, max_tokens, timeout, secrets=None):
        key = (secrets.get(self.key_env) if secrets
               else os.environ.get(self.key_env))
        if not key:
            raise ModelError(f"{self.name}: {self.key_env} is not set")

        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }).encode()

        req = urllib.request.Request(
            self.ENDPOINT.format(model=self.model),
            data=body,
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": key},   # header, never the query string
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise ModelError(f"{self.name}: HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            raise ModelError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise ModelError(
                f"{self.name}: unexpected response shape: "
                f"{json.dumps(payload)[:200]}") from exc

        usage = payload.get("usageMetadata", {})
        return (text,
                int(usage.get("promptTokenCount", len(prompt) // 4)),
                int(usage.get("candidatesTokenCount", len(text) // 4)))


# ─────────────────────────────────────────────────────────────────────────────
# the router
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Router:
    """Resolves a tier to a provider, enforcing privacy and budget first.

    `job` and `data` are bound at construction (see Ctx.build), not passed at
    the call site. A collector cannot forget to declare its data class,
    because it never gets the chance to — the value comes from its META.
    """
    tiers: dict[str, Sequence[Provider]]
    job: str = "adhoc"
    data: str = "private"
    secrets: object | None = None
    conn: object | None = None
    log: object | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: int = DEFAULT_TIMEOUT
    _tried: list = field(default_factory=list, repr=False)

    # ── gates ────────────────────────────────────────────────────────────

    def eligible(self, tier: str) -> list[Provider]:
        """Providers this job is allowed to use, in order.

        Raises rather than returning empty, because "no eligible provider"
        has two very different causes and the caller must be told which.
        """
        if tier not in self.tiers:
            raise ModelError(
                f"unknown tier {tier!r}. Valid tiers: {sorted(self.tiers)}. "
                "Jobs request a tier, never a model name.")

        chain = list(self.tiers[tier])
        if not chain:
            raise NoProviders(
                f"tier {tier!r} has no providers configured. Wire one in "
                "core.models.default_tiers() or pass tiers= explicitly.")

        if self.data == "private":
            allowed = [p for p in chain if not p.logs_prompts]
            if not allowed:
                names = ", ".join(f"{p.name}(logs)" for p in chain)
                raise PrivacyRefusal(
                    f"job {self.job!r} handles PRIVATE data and tier {tier!r} "
                    f"offers only logging providers [{names}]. Refusing to "
                    "send. Either wire a non-logging provider into this tier, "
                    "or set META['data'] = 'public' if this job genuinely "
                    "handles nothing of Nick's.")
            return allowed

        return chain

    def check_budget(self, model: str, prompt: str) -> None:
        spent = costs.month_to_date(conn=self.conn)
        cap = costs.cap_usd()
        worst = costs.estimate(model, prompt, self.max_tokens)
        if spent + worst > cap:
            raise BudgetRefusal(
                f"refused before sending: month-to-date ${spent:.4f} + this "
                f"call's worst case ${worst:.4f} exceeds the ${cap:.2f} cap. "
                "Raise SPINE_MONTHLY_USD_CAP deliberately, or wait for the "
                "month to roll.")

    # ── the call ─────────────────────────────────────────────────────────

    def complete(self, prompt: str, *, tier: str = "bulk",
                 max_tokens: int | None = None,
                 timeout: int | None = None) -> Completion:
        max_tokens = max_tokens or self.max_tokens
        timeout = timeout or self.timeout
        chain = self.eligible(tier)
        self._tried = []
        last: Exception | None = None

        for provider in chain:
            ok, why = provider.available(self.secrets)
            if not ok:
                self._note(tier, provider, "skipped", why)
                last = ModelError(f"{provider.name}: {why}")
                continue

            self.check_budget(provider.billed_as, prompt)

            started = time.monotonic()
            try:
                text, tin, tout = provider.complete(
                    prompt, max_tokens=max_tokens, timeout=timeout,
                    secrets=self.secrets)
            except Exception as exc:
                ms = int((time.monotonic() - started) * 1000)
                self._note(tier, provider, "fallback", str(exc)[:300], ms)
                last = exc
                continue

            ms = int((time.monotonic() - started) * 1000)
            usd = costs.record(
                self.job, tier, provider.name, provider.billed_as,
                tokens_in=tin, tokens_out=tout, latency_ms=ms,
                outcome="ok", conn=self.conn)
            if self.log:
                self.log(f"model ok  tier={tier} provider={provider.name} "
                         f"in={tin} out={tout} ${usd:.6f} {ms}ms")
            return Completion(text, tin, tout, provider.name, provider.model,
                              ms, usd)   # .model = what was actually called

        raise ModelError(
            f"tier {tier!r}: every provider failed. Last error: {last}")

    def _note(self, tier, provider, outcome, error, ms=0):
        self._tried.append((provider.name, outcome, error))
        costs.record(self.job, tier, provider.name, provider.billed_as,
                     outcome=outcome, error=error, latency_ms=ms,
                     conn=self.conn)
        if self.log:
            self.log(f"model {outcome} provider={provider.name} — {error}")


# ─────────────────────────────────────────────────────────────────────────────
# wiring
# ─────────────────────────────────────────────────────────────────────────────

def default_tiers() -> dict[str, list[Provider]]:
    """The production chain.

    `smart` and `frontier` are deliberately EMPTY as of Phase 1. The
    Anthropic Console balance is $0 and no NVIDIA NIM key exists, so there is
    no non-logging endpoint on the box. Leaving them empty means a private
    job asking for `smart` gets a clear NoProviders error instead of being
    silently routed somewhere it should not go.

    MockProvider is never listed here. A production fallback that fabricates
    answers is worse than an outage.
    """
    return {
        "bulk": [GeminiProvider()],
        "smart": [],
        "frontier": [],
    }


def for_job(job, secrets=None, log=None, tiers=None) -> Router:
    """Bind a router to one job, taking its privacy class from META."""
    return Router(
        tiers=tiers or default_tiers(),
        job=getattr(job, "id", str(job)),
        data=getattr(job, "data", "private"),
        secrets=secrets,
        log=log,
    )

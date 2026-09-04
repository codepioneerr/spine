<div align="center">

```
 ███████╗██████╗ ██╗███╗   ██╗███████╗
 ██╔════╝██╔══██╗██║████╗  ██║██╔════╝
 ███████╗██████╔╝██║██╔██╗ ██║█████╗
 ╚════██║██╔═══╝ ██║██║╚██╗██║██╔══╝
 ███████║██║     ██║██║ ╚████║███████╗
 ╚══════╝╚═╝     ╚═╝╚═╝  ╚═══╝╚══════╝
```

**A personal operating system that runs on an old laptop.**

</div>

---

Spine is a small framework for running your own life on hardware you already
own. It schedules jobs, keeps every result in one queryable table, rents
reasoning from an LLM API by the token, and asks before it ever does
anything irreversible.

It is built for an 8 GB machine, on purpose.

## Why it's shaped like this

Most personal-automation setups die the same way: every new idea brings its
own scheduler, its own storage, its own secrets convention, its own way of
notifying you. Six ideas in, you have six half-working silos and no way to
ask a question that spans them.

Spine's answer is that **a new idea is only allowed in as a collector** —
one file, one schedule, one output shape, writing into one table. If an idea
can't be expressed that way, it doesn't get built until it can. That
restriction is the product.

The hardware constraint does similar work. The target is a Dell Latitude
3500 with 8 GB of RAM. That rules out concurrent agent swarms and local 7B
models, which sound impressive and deliver least. What's left — a reliable
scheduler, a clean data layer, and one well-aimed model call — is the part
that actually runs every day. And it means anyone with an old laptop or a
Raspberry Pi can run this, which is the difference between a framework and
a personal snowflake.

## Architecture

```
5. SURFACES     bin/darkweb (console) · Telegram · dashboard
4. ASSISTANT    one model call over an item-store query
3. ITEM STORE   items(...) — one SQLite table everything feeds
2. COLLECTORS   one file, one schedule, one output shape
1. CORE         runner · db · http · models · notify · secrets · costs · registry
```

**Core** serializes everything behind a global `flock`, generates the crontab
from a registry so nobody hand-edits cron again, routes model calls by *tier*
(`bulk` / `smart` / `frontier`) rather than by model name, and logs the
dollar cost of every call.

**Collectors** are one deterministic file each. They declare a `META` block
and a `run(ctx)` function, and they never name a model.

**The item store** is a single table. A recorded deed, a prediction-market
move, an unread email, an assignment due, a mispriced listing — all the same
shape. Dedup and retention are solved once, for everything.

**The assistant** reads across it and produces judgment: a morning brief,
alerts worth interrupting you for, drafts awaiting approval. One model call
over one query, not an agent per domain.

## Resource discipline

Jobs declare what they cost and when they may run:

```python
META = {
    "id": "acris", "schedule": "0 6 * * *", "timeout": 900,
    "ram_mb": 150, "window": "night", "weight": "light",
    "tier": None, "data": "private",
}
```

Heavy jobs run only in the night window. A pre-flight RAM guard skips a job
rather than letting the box swap. Daytime is reserved for light work so the
interactive path stays responsive. A skipped run is recoverable; an
OOM-killed box at 3am is not.

## Privacy is enforced, not documented

Every provider declares whether it retains prompts. Every job declares
whether its data is private — and **private is the default**. A job handling
your own data cannot be routed to a logging endpoint; the call raises rather
than quietly downgrading. Opting *out* is the deliberate act, because the
mistake worth designing against is forgetting.

The spend cap works the same way: month-to-date plus a pessimistic estimate
is checked *before* the request is sent, not reported after the bill arrives.

## Safety posture

- The assistant **observes and proposes. You act.**
- No send, no buy, no post, no delete in v1.
- Email is **draft-only, permanently** — structural, not a setting.
- Secrets live in `.env` at mode 600 and never appear in a prompt.

## Status

Early. This repo is public from its first commit, which is a forcing
function for clean secrets handling rather than a claim that it's finished.

| Phase | | |
|---|---|---|
| Step 1 | repo, rules, console surface | ✅ |
| 0 | job contract · crontab registry · runner · RAM guard | ✅ |
| 1 | model router · cost accounting · `bin/status` | ✅ |
| 2 | the item store | next |
| 3 | proptech collector | |
| 4 | assistant v1 — daily brief | |
| 5 | email + calendar, read-only | |
| 6 | trading + reselling · docs | |

## Quick look

```bash
git clone <this repo> && cd spine
cp .env.example .env && chmod 600 .env
bin/darkweb                          # the console
bin/darkweb --watch                  # live
bin/status                           # same data, plain text, greppable
python3 -m core.costs --month        # spend by job and tier
python3 -m core.registry --list      # what is registered
python3 -m core.registry --diff      # what installing would change
python3 -m core.registry --install   # write the crontab (asks first)
bin/run.sh heartbeat --dry-run       # exercise the gates
python3 -m unittest discover -s tests
```

Requires Python 3.9+. No dependencies.

See [`CLAUDE.md`](CLAUDE.md) for the full system rules.

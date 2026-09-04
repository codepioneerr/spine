# CLAUDE.md — system rules for Spine

**Spine** is the framework. **NickOS** is Nick's deployment of it.
Read this file before writing any code in this repo. These rules are not
style preferences; several of them are the difference between a box that
stays up and a box that gets OOM-killed at 3am.

---

## 0. Vocabulary (read this first)

The framework is called **Spine**. Layer 3 — the single SQLite table every
collector writes into — is called **the item store**, or just `items`.

Do not call layer 3 "the spine." The name collides with the framework and
makes "did the spine break?" ambiguous six months from now. *(Deviation from
the Sept 3 PRD, which named both. Flagged to Nick; revert if he disagrees.)*

---

## 1. The hardware is the architecture

Target: a Dell Latitude 3500. **8 GB RAM, 8 cores, 232 GB disk**, Ubuntu,
headless, reachable over Tailscale as `darkweb`. Measured idle usage is
~936 MiB, leaving ~6.7 GiB.

**The Dell is a scheduler and a stateful data store. It is not a compute
cluster.** Reasoning is rented by the token over an API. The box holds the
state, runs the clock, and does the cheap deterministic work.

This is not a compromise forced by budget — it is what makes Spine reusable.
Anyone with an old laptop or a Raspberry Pi can run it. If it needed 64 GB,
nobody could follow along. **The constraint is the feature.**

### Hard RAM rules

| Thing | Active RAM | Rule |
|---|---|---|
| Ubuntu + Tailscale idle | ~0.9–1.2 GB | baseline, always on |
| Hermes gateway | ~0.3–0.5 GB | already running, do not touch |
| A Python collector | < 200 MB | dozens of these, fine |
| SQLite | negligible | 207 GB free |
| Headless browser / Playwright | ~1.5 GB | **serialized only, night only** |
| Local 3–4B model @ Q4 | 2.5–3.5 GB | one at a time, never with a browser |
| A 7B+ local model | 6 GB+ | **FORBIDDEN** |
| Concurrent LLM agent loops | contending | **FORBIDDEN** |

Daytime total must stay under ~2 GB. A night window may use up to ~3.5 GB
for one heavy job, leaving ~2.5 GB headroom.

---

## 2. Serialization is enforced, not encouraged

Every job runs through `bin/run.sh`, which takes a **global `flock`**. Two
jobs never run at once. This is the single mechanism that keeps an 8 GB box
alive, and it is not optional, not per-job, and not configurable per
collector.

If you find yourself wanting concurrency, you want a bigger box. Say so
instead of removing the lock.

---

## 3. Resource-aware scheduling (a core feature, not a convention)

Nick's constraint, verbatim: *"I just don't want it to run twenty four hours
where I don't have space on my CPU or my RAM... I want the computer to be
smart about what it is building."* And: **Telegram must stay responsive all
day.**

Every job declares two fields beyond its schedule:

```python
META = {
    "id":       "acris",
    "schedule": "0 6 * * *",   # UTC. Generated into crontab; never hand-edited.
    "timeout":  900,
    "ram_mb":   150,           # honest peak estimate
    "window":   "night",       # night | day | any
    "weight":   "light",       # light (<200 MB) | heavy (browser, model, big batch)
    "tier":     None,          # None | "bulk" | "smart" | "frontier"
    "data":     "private",     # public | private  — DEFAULTS TO PRIVATE
}
```

The runner enforces four rules:

1. **`heavy` jobs run only in the night window** (default 01:00–06:00 local).
   Browser work, local-model work, and large batches land here.
2. **Pre-flight RAM guard.** Before starting a heavy job, compare free memory
   against `ram_mb` + `SPINE_RAM_HEADROOM_MB`. If it's tight, **skip and
   log**. Never swap. Never OOM. *A skipped run is recoverable; an
   OOM-killed box at 3am is not.*
3. **Global `flock` always.** Nothing heavy ever overlaps.
4. **Daytime is `light` work only.** Interactive responsiveness is a hard
   guarantee, not best-effort.

---

## 4. Secrets

- `.env` only, mode `600`, never committed. `.gitignore` already covers it.
- **No secret ever appears in a prompt.** Not once, not for testing.
- Secrets reach code through `ctx.secrets`, never `os.environ` scattered
  through collectors.
- The repo is **public from day one**. Assume every commit is read by a
  recruiter and by an attacker. Both assumptions are correct.

---

## 5. Zero bloat

Python **standard library and SQLite**. That is the default and the strong
preference.

**Do not install:** LangChain, LlamaIndex, n8n, Celery, Redis, Airflow,
Postgres, Docker orchestration, or any agent framework. Each is a second
scheduler, a second state store, or 350 MB of idle RSS — and the entire
point of this project is that Nick currently has four schedulers and one
corpse.

A third-party dependency needs a written justification in the PR: what it
does, what it costs in RAM, and why stdlib can't. `httpx`/`requests` and
`playwright` (night-window only) are the expected exceptions.

---

## 6. Reversibility

- Every phase ends in a commit that leaves a **working system**.
- **Nothing is deleted until its replacement is verified against real output.**
- `darkweb-jobs` and its crontab are **not touched** by this repo until its
  three collectors are reimplemented here and verified. Nick chose to retire
  it; the retirement happens as one deliberate commit at Phase 2, not as a
  side effect of scaffolding.
- **Hermes is not touched.** It runs Telegram, it works, and rebuilding a
  message gateway is not on the critical path. Spine talks *to* it via
  `notify`.

---

## 7. Permissions — v1 is read-and-recommend

The assistant **observes and proposes. Nick acts.**

- No send, no buy, no post, no delete. Ever, in v1.
- Anything side-effectful becomes a proposed item awaiting approval from
  Telegram.
- **Email is draft-only, permanently.** This is structural, not a setting.
- Write verbs get added one at a time, after the item store is proven.

---

## 8. Layer map

```
5. SURFACES     bin/darkweb (TUI) · Telegram · dashboard
4. ASSISTANT    one model call over an item-store query
3. ITEM STORE   items(...) — one table everything feeds
2. COLLECTORS   one file, one schedule, one output shape
1. CORE         runner · db · http · models · notify · secrets · costs · registry
```

**The scope-discipline mechanism:** a new idea is only allowed in as a
**collector** — one file, one `META`, one `run(ctx)`. If an idea cannot be
expressed that way, it does not get built until it can. This is the rule
that stops silo #11.

---

## 9. Build phases

| Phase | What | Status |
|---|---|---|
| **Step 1** | Repo, rules, `bin/darkweb` cyber surface | ✅ this commit |
| **0** | Job contract · registry generates crontab · `run.sh` + flock · windows + RAM guard | ✅ complete |
| **1** | Model router (tiers, not model names) · cost accounting · `bin/status` | ✅ complete |
| **2** | Item store · reimplement the 3 collectors · retire darkweb-jobs | awaiting approval |
| **3** | **Proptech collector** (ACRIS/PLUTO depth) | *order changed by Nick, Sept 3* |
| **4** | Assistant v1 — daily brief → Telegram | |
| **5** | Email + calendar collectors, read-only | |
| **6** | Trading + reselling migration · docs · publish | |

**Stop and ask for code review when a phase is complete.** Do not roll into
the next phase unprompted.

---

## 10. Model routing (Phase 1, stated here so nothing pre-empts it)

Jobs request a **tier**, never a model name.

```python
TIERS = {
  "bulk":     [...],  # classification, extraction — Gemini free tier
  "smart":    [...],  # judgment, drafting
  "frontier": [...],  # rare, explicit, capped
}
```

No collector may name a model. Swapping models is a one-line change in one
file. Every call logs tokens and dollars to `costs`. Target recurring spend:
**under $5/month.** Hard cap in `.env` before any key is live.

### The two refusals

`core.models` runs three gates before a prompt leaves the box: privacy,
budget, then fallback. The first two **fail closed** and both raise — neither
warns and proceeds, and neither silently degrades.

**Privacy.** Every provider declares `logs_prompts`. Every job declares
`data`, which **defaults to `private`**. A private job cannot be routed to a
logging provider; if its tier offers only logging providers, the call raises
`PrivacyRefusal`.

The rule comes from the Sept 3 budget memo — *"nothing sensitive goes to a
free endpoint, ever"* — and it exists because by Phase 5 the email collector
reads dean correspondence and job applications. The failure being designed
against is not malice, it is omission: someone adds a collector in six
months and doesn't think about it. So omission must be the safe case.

Note the flag is `logs_prompts`, not `free`. A local mock is free *and*
private. A free hosted endpoint is neither.

**Budget.** Month-to-date plus a pessimistic estimate of this call is
checked against `SPINE_MONTHLY_USD_CAP` **before** the request is sent. Over
the line raises `BudgetRefusal` and nothing goes out. Same discipline as the
RAM guard: refuse cheaply rather than discover expensively.

**As shipped:** `bulk` is Gemini free tier (logs prompts, public data only).
`smart` and `frontier` are deliberately EMPTY — the Anthropic Console
balance is $0 and no NIM key exists, so there is no non-logging endpoint on
the box. Empty gives a clear `NoProviders` error; a stand-in would give a
leak. `MockProvider` is never registered in a production tier, for the same
reason the console refuses to invent numbers.

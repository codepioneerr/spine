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
- `darkweb-jobs` and its crontab are **not touched** by this repo.

  *Revised Sept 7 2026.* The original plan reimplemented its three
  collectors here and then retired it. Phase 2b instead reads its SQLite
  files read-only (`core.bridge`), because rewriting three working
  Socrata/Gamma clients against APIs this code has never called, then
  trusting them on the first unattended 06:00 run, is the opposite of
  "nothing is deleted until its replacement is verified against real
  output."

  **Consequence: darkweb-jobs is the permanent fetch layer and Spine the
  item layer on top.** That is a real architectural change from the Sept 3
  plan, not a delay. Revisit only after `bin/compare-migration` runs clean
  for a week — at which point retirement is a decision made with data.
- **Hermes is not touched.** It runs Telegram, it works, and rebuilding a
  message gateway is not on the critical path. Spine talks *to* it via
  `notify`.

  *Amended Sept 30 2026.* Delivery landed, and it goes through Hermes rather
  than around it: one webhook route registered with `--deliver-only`, which
  relays the rendered brief verbatim with no agent invocation and no model
  cost. Spine holds an HMAC secret for a loopback route and no bot token. The
  one thing this did require was enabling Hermes's webhook platform, which
  meant two gateway restarts — taken with a config backup and Telegram
  verified afterwards each time.
- **`personal_os` is not touched either, and it is a peer, not a silo.** A
  second system runs on this box as user `Codex`: stdlib-only core, immutable
  proposals requiring explicit confirmation, unknown formats rejected rather
  than interpreted by a model, an atomic $5/month reservation ledger, delivery
  receipts. That is this document's philosophy, arrived at independently. It
  would be a mistake to absorb it.

  **The boundary, decided Sept 30 2026:**

  | | Owner | Delivers | Remit |
  |---|---|---|---|
  | 05:40 ET | **Spine** | `collectors/brief.py` | market and property signal — deeds, prediction-market moves, paper positions |
  | 07:30 ET | **personal_os** | `personal-os-morning.timer` | calendar, tasks, email review |

  Two morning messages is a deliberate division of remit, not drift. Written
  down because §5 exists to stop silo #11, and the honest risk here is not
  that these two overlap today — it is that in six months nobody remembers the
  split was chosen. If the remits start bleeding into each other, that is the
  signal to revisit, and the integration pattern is already proven: read the
  other system's state read-only through `core.bridge`, exactly as Phase 2b
  does with darkweb-jobs. It needs an ACL granting `pioneer` read on
  `/home/Codex/personal-os-dev-state`, and deliberately **not** on its
  `private/` directory, which holds a bot token and a health token.
- **The local model is borrowed, and that is the safe choice.** `smart` runs
  against `personal-os-ollama.service` — Codex's systemd unit, bound to
  `127.0.0.1:11434`, with the weights under `/home/Codex/.local/share/personal-os/models`
  at mode 700, so `pioneer` cannot read them. Spine is an anonymous localhost
  client.

  Giving Spine its own daemon was considered and rejected. It needs a second
  3.2 GB copy of the weights, and two instances each honouring
  `OLLAMA_MAX_LOADED_MODELS=1` can still hold one model *each* — about 7.2 GB
  on a 7.6 GB box, which is the swap-or-OOM outcome §1 forbids. **The shared
  single instance is what keeps an 8 GB box safe**, so the coupling is a
  feature bought with a dependency, not an oversight. If Codex stops the
  service or removes the model, `available()` reports it and the brief ships
  layer 1 without commentary — which is why layer 1 does not depend on a
  model.

### Model spend is not fully metered

Three things on this box can spend model budget and only two of them are
counted:

| | Budget | Metered by |
|---|---|---|
| Spine | `SPINE_MONTHLY_USD_CAP` $25, target $5, pre-flight | `core.costs` |
| personal_os | $5/month atomic reservation ledger | its own ledger |
| **Hermes** | none | **nothing** |

Both ledgers currently read $0 — Spine's calls are local and personal_os
reports no paid call made. Hermes is the one actually consuming capacity: it
is hitting Gemini `429 RESOURCE_EXHAUSTED` on the free tier, and
personal_os's README says plainly that "existing Hermes conversation costs are
not metered here." Spine does not meter it either.

So the first number that will surprise Nick is the one neither system watches.
Unifying the ledgers is not worth building while both read zero; noticing that
Hermes is unmetered **is** worth writing down, because a cap that misses the
only live spender is not a cap.

---

## 7. Permissions — v1 is read-and-recommend

The assistant **observes and proposes. Nick acts.**

- No send, no buy, no post, no delete. Ever, in v1.
- Anything side-effectful becomes a proposed item awaiting approval from
  Telegram.
- **Email is draft-only, permanently.** This is structural, not a setting.
- Write verbs get added one at a time, after the item store is proven.

---

## 7a. The item store

One table. Every collector writes into it; the assistant reads across it.

    items(id, ts, updated_ts, source, kind, key, title, body, url,
          data_json, importance, status, acted_at)
          kind:   signal | deal | task | alert | fact
          status: new | seen | acted | dismissed
          UNIQUE(source, key)

**An item is something Nick might act on.** Not everything a collector
knows belongs here. `heartbeat` wrote one row every 30 minutes through all
of Phase 2a; by the time Phase 3 was scoped those 97 telemetry rows were
100% of the store, and a brief selecting the top 25 unacted items would
have been 25 heartbeats. Infrastructure telemetry goes to a state file and
surfaces in the console. If a collector's output would never be acted on,
it is not an item.

**`UNIQUE(source, key)` is what lets collectors be dumb.** They emit
everything they see on every run; the store decides what is actually new.
Choosing `key` is therefore the only hard part of writing a collector: it
must be stable across runs for the same real-world thing. A document number
is a good key. A row index is not.

**On conflict, `ts` / `status` / `acted_at` are preserved.** First-seen
stays first-seen, so a contract that moves every 30 minutes does not keep
resetting its own age. And something already acted on does not return to
the queue because a collector re-emitted it — if it did, everything Nick
had dealt with would reappear in tomorrow's brief, and he would stop
reading the brief.

`ctx.db` is bound to the job, so `source` comes from the job id. A
collector cannot mislabel where its items came from, because it never gets
to say — the same reasoning as `ctx.models` taking privacy from META.

Retention is per-kind (`core.store.RETENTION_DAYS`). **Acted items are
never pruned**: they are the record of what Nick actually did, they cost
nothing to keep, and they would hurt to lose.

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
| **2a** | Item store (`items`), `ctx.db`, `bin/items`, heartbeat migrated | ✅ complete |
| **2b** | Read-only bridge to acris/polymarket/eventbot · `bin/compare-migration` · heartbeat off the store | ✅ burn-in Sept 30 – Oct 6: acris 7/7 clean, eventbot 6/7 (one miss, day 2), **polymarket unverified** (window empty every run) |
| **3** | **Proptech collector** (ACRIS/PLUTO depth) | ✅ Oct 6 — reviewed and merged (ce0e038); 3b (b176e00): condo unit lots, ACRIS staleness alert; AVM v0 merged gated (804c0bd) |
| **4** | Assistant v1 — daily brief → Telegram | 🟡 `collectors/brief` scheduled 09:40 UTC; model step RAM-gated in-run (083fd78) |
| **5** | Email + calendar collectors, read-only | |
| **6** | Trading + reselling migration · docs · publish | |

**Stop and ask for code review when a phase is complete.** Do not roll into
the next phase unprompted.

**Phase 3, as built (Oct 6 2026).** Two thresholds, set from
`--preview`: `SPINE_PROPTECH_STORE_MIN` ($1M, ~85 docs/day) is what Spine
keeps in its own history tables; `SPINE_PROPTECH_MIN_AMOUNT` ($5M, ~12/day)
is what `acris` puts in the brief. History lives in `acris_docs`,
`acris_doc_parcels` and `parcels` in spine.db — reference data beside
`items`, not in it (7a). `collectors/proptech` fills PLUTO facts by BBL
lookup against NYC's public dataset for the ~94% of parcels the darkweb-jobs
slice misses: a deliberate, narrow exception to "darkweb-jobs is the fetch
layer", reasoned in `core/proptech.py`. It emits repeat-sale signals
(single-parcel, whole-interest deeds only).

**Phase 3b (Oct 6).** Condo unit lots (1001–6999) are mapped through the
Digital Tax Map datasets into `condo_lots` (unit → base → billing lot);
PLUTO files a condo under its *billing* lot (75xx), and `parcels_for`
answers a unit BBL with that building's facts. `acris` also emits a
`feed-stale:<date>` alert when the newest recorded date is >7 days old
(`SPINE_ACRIS_STALE_DAYS`); the city's feed sat at 2026-08-31 on Oct 6.

**AVM v0 (Oct 6).** `core.avm`, comps-based with a leave-one-out backtest.
Nothing surfaces until its level backtests under 25% MdAPE
(`surfaceable()`); keep it that way until the error drops.

**The brief and RAM (Oct 6).** `brief` is a light job. Its model step
(qwen3.5:4b, ~3.6 GB resident) checks 3500 MB + headroom itself, retries
3× 60 s apart, and is skipped with a note if RAM never frees — the brief is
always sent. Do not lower that threshold: 3.6 GB into ~1 GB free is an OOM.

`bin/proptech-report` shows job outcomes, PLUTO coverage, condo mapping and
open alerts on one screen.

**Phase 2b is not complete when the tests pass.** Every test in
`tests/test_phase2b.py` runs against synthetic databases built from dumped
schemas — that proves the code is self-consistent, not that it is right
about the world. `bin/compare-migration` is the gate, and it needs a week of
real output. It also answers the one number Phase 3 cannot guess:
`SPINE_PROPTECH_MIN_AMOUNT` is a placeholder, and `--preview` shows what
each threshold costs per day.

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

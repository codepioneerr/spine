# Telegram assistant: discovery, design, handoff

Branch `feat/telegram-assistant`. Staged, not activated. Discovery date: 2026-10-06.

## What exists today (discovered)

| Thing | Reality |
|---|---|
| Hermes (`@hermnick_bot`) | Third-party agent gateway (`hermes-gateway.service`). It polls its own token and answers with Gemini flash-lite on the **free tier, which logs prompts**. It also relays Spine's brief through a `--deliver-only` webhook route. The raw JSON / "unknown event" messages come from the `orch-notify` route: its `prompt` is empty, so Hermes relays the payload verbatim. |
| Quant (`@dellquanttrade_bot`) | darkweb-jobs `common/notify.py`. Send-only: nothing polls this token (no webhook, no getUpdates consumer). Sends eventbot's 07:00 ET report. `EXECUTOR=sim`. |
| Health | Health Auto Export (iOS) → `spine-health.service` (:8123, Tailscale) → `var/health.db`. Daily aggregates per metric. As of Oct 6: **2 days** (Oct 5–6), Apple Watch + iPhone, **no sleep records, no workouts**. HRV is Apple SDNN. |
| Health brief | `collectors/health_brief.py`, 09:10 UTC. Kept on disk; Telegram send is off unless `SPINE_HEALTH_BRIEF_TELEGRAM=1`. |
| Market/property brief | `collectors/brief.py`, 09:40 UTC, via Hermes webhook. |
| Local model | qwen3.5:4b needs ~3.6 GB. MemAvailable is ~1.4 GB during the day; `cloudcode_cli` holds ~3.5 GB RSS. That is why "plan skipped: 2.4 GB free, needs 4.5 GB" kept appearing. |
| Governor | `orch gov preflight` OK (claude and codex workers; qwen unavailable for RAM). `gov create` requires the interactive nonce phrase, so this work was done directly on a feature branch instead. |

## Screenshot problems, traced

| Symptom | Cause | Fix |
|---|---|---|
| "DEALS" for DEED and MTGE | `core/brief.KIND_LABEL` + `acris.title_of` used raw codes | Labels are now "Property records", "Deed (ownership transfer)" and "Mortgage (loan, not a sale)". |
| ACRIS stale (Aug 31 newest record on Oct 6) | **Upstream**: NYC dataset `bnx9-e6tj` rowsUpdatedAt 2026-09-08, max recorded 2026-08-31. Our fetch runs nightly (`feed=feed_paused`). | The assistant explains it once ("city-side pause") and stops repeating it. |
| Sept 30 market in Oct 6 brief | `jumps` rows had no end date. A jump recorded at 18:30 on a market that ended at 16:00 is settlement. | `core/market_filter` + polymarket collector drop expired/settling jumps. The assistant re-checks end dates at read time. |
| Health numbers without context | No coverage, no sample counts, 1-day "baselines" | `core/health_facts`: complete vs partial days (NY timezone, DST), baselines only with ≥5/≥14 complete days and n shown, missing ≠ 0. |
| "plan skipped … MB" inside the health brief | A system failure was written into lifestyle text | Moved to the log and the `/status` operator view. |
| Quant numbers don't reconcile | No stated basis | `core/quant_facts.accounting`: start 10,000; equity = cash + marked value; realized + unrealized + residual. Real data reconciles to a **$0.10 unexplained residual** (shown, not hidden). |
| Score "85" | `acris.importance`: dollar bands + unit nudge | Explained as a priority score by dollar size. It is not a probability. |

New finding: simulated BTC/ETH positions 250/251 were opened by `crypto_mention` on a Polymarket "Bitcoin Up or Down 8AM–12PM" market dropping to 0.01, which is the market settling at its end. The assistant flags this on "Why opened?". Changing eventbot's rules belongs in darkweb-jobs and is left to Nick.

## Design (smallest thing that works)

* **One backend, in Spine** (`surfaces/`), stdlib only, no new dependencies, no model in the answer path.
* **Entry point: the Quant bot token**, polled by `surfaces/bot.py` (systemd `spine-assist`, MemoryMax 128M). It is the only existing bot nobody polls, so there is no getUpdates conflict. eventbot keeps sending through it. Hermes is not modified.
* Data services are deterministic and read-only: `core/health_facts`, `core/property_facts`, `core/quant_facts`, `core/market_filter`.
* `var/assist.db` holds preferences, focus subscriptions, cards (opaque ID → exact object + snapshot + Telegram message_id), single-use callback tokens (7-day expiry), update dedupe, self-reported feedback, telemetry (no payloads), the brief log, and saved research questions.
* Free text is routed by keyword rules (`surfaces/router.py`). Unmatched questions get "not covered yet, saved for research", never a guess.
* Evidence: `surfaces/evidence.py` contains only pages that were fetched on 2026-10-06, each with the claim it supports, its limits and a refresh policy.
* Charts: a stdlib PNG renderer. Gaps are drawn as ×, never as zero or interpolated. Partial days use a lighter tint.
* Daily brief: `surfaces/daily.py`, about 120 words, at most three data lines plus focus lines. It is sent at the configured local time by the service's scheduler (no crontab change) and logged once per day so it is never duplicated. It is **off by default**.
* No Mini App: no HTTPS hosting or auth exists on the box, and a public health dashboard is out of scope. Charts and buttons cover the core experience.

## Activation (needs Nick's approval)

1. Merge `feat/telegram-assistant` into `main`.
2. Add to `spine/.env` (mode 600): `SPINE_ASSIST_BOT_TOKEN=<Quant bot token from darkweb-jobs/.env>` and `SPINE_ASSIST_ALLOWED_ID=<your numeric id>`.
3. `python3 -m surfaces.bot --set-commands`, then install `deploy/spine-assist.service`.
4. In Telegram: `/settings brief on`, `/settings brief 07:30`.
5. Optional, to avoid two morning messages: unset the Hermes-delivered `brief` cron after a week of overlap.
6. Optional Hermes clean-up (config change): give the `orch-notify` subscription `prompt: "{text}"` (or have orch send a `text` field) so raw JSON stops appearing.

## Known gaps

* Health baselines start after ~5 complete days. Sleep requires enabling Sleep Analysis in Health Auto Export.
* There is no hosted research model. Novel questions are saved locally rather than researched live.
* The PLUTO data-dictionary version was not machine-verified.

## Claim/evidence review (2026-10-06)

Claims in fixed answer text, checked against the page each one cites. Verdicts: **supported** (the page says it), **reworded** (changed to match the page), **labelled** (kept, but marked as interpretation), **removed**.

| Answer | Claim | Source | Verdict |
|---|---|---|---|
| activity | 150 min/week moderate (or 75 vigorous), plus 2 days of strength work | CDC Adult Activity (2023-12-20) | supported |
| sleep | Adults 18–60: 7+ hours | CDC About Sleep (2024-05-15) | supported |
| sleep | Fixed wake time, earplugs/eye mask | none | labelled as interpretation |
| sexual | Inactivity, smoking, heavy drinking, drug use and blood-vessel disease are linked to ED | NIDDK Symptoms & Causes (2024-10) | supported |
| sexual | "don't vape nicotine" | not on the page | removed |
| sexual | "condoms" | CDC prevention page not retrieved | removed |
| sexual | See a clinician for persistent change; ED can be a sign of another problem | NIDDK | reworded to match the page |
| sexual | "blood in urine or semen" as a red flag | not retrieved | removed |
| sexual | Kegels aren't for everyone; check with a clinician first | NIDDK Kegel (2021-11) | supported |
| sexual | STI testing schedules depend on group | CDC STI Testing (2026-03-17) | supported |
| HRV | Apple Watch HRV is SDNN, from irregular short readings | validation literature (PMC) via search; not in the registry | supported as a description; not cited in the bot |
| HRV/RHR | "Lower RHR tracks fitness", "varies a lot between people" | none | removed / labelled as interpretation |
| mobility | No peer-reviewed controlled trials of GOATA found | search 2026-10-06 + goatamovement.com | supported, scoped to that search |
| mobility | "no routine realigns bones" | none | reworded: "I found no evidence that…" |
| workouts | Doses and technique | no cited source | general routine; the safety stop rule is generic |
| property | DEED = grantor → grantee; MTGE = mortgagor/mortgagee loan | ACRIS Document Control Codes | supported |
| property | Mortgage "ownership did not change", "routine refinance" | unsupported | removed |
| quant | "real trading would do worse" | unsupported as a guarantee | reworded as a simulation limitation |

Researched answers (`surfaces/research.py`) quote MedlinePlus sentences word for word, so they cannot misstate their source. Their remaining risks are **relevance** (handled by the title/all-keyword gate) and **applicability**, which the answer labels.

## Round 2 (2026-10-06): conversation depth

### What is actually implemented

| Layer | Implemented? | Detail |
|---|---|---|
| Deterministic facts | yes | Health, property and Quant numbers come only from `core/*_facts.py`. |
| Live source retrieval | yes | Health questions query NIH MedlinePlus (consumer pages) and PubMed (systematic reviews and meta-analyses in humans since 2010, main keyword in the title). Property follow-ups query NYC Open Data ACRIS: parties (636b-3b5g), legals (8h5j-fqxa), master (bnx9-e6tj). |
| Synthesis | extractive only | Candidate sentences are scored by fixed rules: keyword overlap, the aspect asked about, findings over aims, Results/Conclusions sections. They are de-duplicated and quoted with [n]. The rules also detect uncertainty statements, aspect gaps ("no source addresses timing") and study populations. |
| Broader web search | **no** | No search API is configured. |
| Model reasoning | **no** | No model in the answer path; see the decisions below. |
| Follow-ups | yes | Short messages continue the last card for 30 minutes (context stored in assist.db). Each card type has its own follow-up words. Generic phrasing with a new subject starts a new question; a message about another domain switches domain. After a batch of cards, the bot picks the card that fits the question or asks which one. |

What leaves the box: up to 3 topic keywords per health lookup, and document/lot numbers for property lookups. Nothing goes to Hermes or Gemini. NCBI requests are paced at 0.4 s apart (its limit without an API key is 3/s), with one retry on 429.

### Known limits
* Choosing keywords is rule-based. When nothing matches, the search broadens by dropping generic words, then at most one specific word. It can drop the word that mattered ("train boxing sick" → "boxing"), but the answer says it broadened. A model would choose better.
* Quotes are verbatim, but deciding which ones answer the question is heuristic. Population tags come from titles and "in/among …" phrases only.
* The NIH Office of Dietary Supplements API is behind a Cloudflare challenge, so it isn't used.

### Fixed in this round
* Workout durations are computed (reps × 4 s, rests, side switches, 30 s transitions, warm-up). The old "15-minute" plan was about 21 minutes; it is now about 16 and labelled with its computed total.
* `/profile`: items from `var/health_profile.md` start as *unconfirmed*. Workouts use confirmed items only, cues match the plan, and re-seeding never brings back an item you removed.
* Sources buttons link to the exact pages; research answers get one button per cited source.
* Every card and the brief show the same "🕒 … as of" times in ET. Quant UTC labels were removed.
* `core/health.py`: unsummarized sleep-stage segments crossing midnight were split into two nights. They are now clustered into one session. **This takes effect when spine-health is restarted (pending approval).**
* ACRIS document names and party roles come from the official 126-code table (`core/acris_codes.json`). The old hard-coded guess "MCON = consolidation" was wrong: it is a memorandum of contract.
* Health Auto Export: the receiver was contract-tested against the app's documented sleep (summarized and stage) and v2 workout JSON. The Sync help text now uses the app's own option names.

### Resources
The service RSS is about 14–25 MB, under its 128 MB cap. An uncached research answer takes 0.6–2 s and makes 2–8 public HTTP requests; cached answers are local. No model RAM is used and there is no cost.

## Round 3 (2026-10-06 evening): verification and budgets

Branch `feat/assistant-round3` from `62f7821`. **Not merged.** `spine-assist` runs from the main checkout, so this takes effect only after a merge and a `spine-assist` restart, which both need Nick's approval.

### Verified state (23:00 UTC)
* **Typed "Explain this" reply: it has not arrived.** The last stored update is 17:34 UTC. `spine-assist` was restarted at 18:14 UTC when 62f7821 was deployed. Its log has no errors, and it holds an open long-poll connection to Telegram. Message bodies are not stored, so the reply's latency cannot be measured. Latency measured from telemetry for the earlier session: text answers took 0.48–0.65 s, the `p_explain` button took 5.5–7.0 s (live ACRIS lookups), and other buttons took 1.0–1.4 s.
* **`spine-health` was restarted at 23:01:59 UTC** (approved). Before the restart: the last sync was at 22:41, there were no open :8123 connections and no import process was running. After the restart it listens on loopback and the Tailscale address and returns 401 without the token. The cross-midnight sleep fix is now live. New regression test: `tests/test_health.py::test_stage_segments_crossing_midnight_are_one_night`. It passes on the fix and fails on the pre-fix `core/health.py`.
* The scheduled brief is still off (no `brief` enable preference is set). The Hermes-delivered `brief` cron (09:40 UTC) is unchanged.

### Workout time budgets (enforced)
Before this round, the computed totals overran their labels: strength15 took 16:00, mobility10 about 15:00 and walkprep8 10:42. `surfaces/workouts.fit()` now trims a plan until its computed total, including the warm-up, rests, side switches and transitions, is within the limit. The limit is the plan's nominal minutes, or the minutes the user asks for ("15-minute workout", "I only have 8 minutes", `/workout 20 min`). Trimming happens in this order: drop sets (last move first, never below 1), shorten the warm-up to 60 s, then drop trailing moves, which are named in the plan. Plans are never padded. Results: strength15 takes 14:39, mobility10 8:42 and walkprep8 7:44. The "Shorter version" option honours the same limit. Tests: `TestWorkouts` in `tests/test_conversation.py`. The full suite has 391 tests and all pass.

### Local model for source-grounded answers: assessment, no benchmark run
* The only model is qwen3.5:4b Q4_K_M (3.4 GB on disk, about 3.6 GB resident) on the shared `personal-os-ollama` instance (Codex's unit, one model loaded at a time). That unit is currently stopped. Its API answers, but nothing is loaded.
* RAM: 7.8 GB total, 3.7 GB available at 19:00 ET, and 586 MB of swap already in use. The gate is 3500 + 1024 MB headroom. Brief logs show 5.1–6.9 GB available at 05:40 ET on 4 of the last 6 days, and 1.0 and 2.4 GB on the other two.
* Latency, from real runs: about 500 tokens in and about 75 out took 33–36 s, including load. A grounded answer needs roughly 1.5–3k tokens of quoted sources, and on this CPU it is estimated at 1–2 minutes. This is an estimate, not a measurement.
* Concurrency: 1. CLAUDE.md forbids concurrent model loops and daytime heavy jobs.
* **Conclusion:** on-demand daytime answers are not practical: a 3.6 GB model would exceed the ~2 GB daytime budget and would be too slow for chat. An overnight batch that answers saved research questions (01:00–06:00, gated by the existing RAM check) is practical about 2 nights in 3. Answer quality is **unmeasured**. The benchmark was not run: RAM was below the gate and it was outside the night window. Next step if approved: a one-off night benchmark (about 5 saved questions, one at a time, with timing and a faithfulness check against the quoted sources) before any scheduled job is added.

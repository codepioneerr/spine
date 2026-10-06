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

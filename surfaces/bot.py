"""
surfaces.bot — the inbound Telegram service (long polling).

    python3 -m surfaces.bot            # run (systemd unit: spine-assist.service)
    python3 -m surfaces.bot --once     # process pending updates once, then exit
    python3 -m surfaces.bot --brief-now  # send today's brief now (still deduped)

## Identity and ownership

Polls SPINE_ASSIST_BOT_TOKEN. Intended to be @dellquanttrade_bot's token:
on Oct 6 2026 nothing polled that bot (darkweb-jobs only sends with it), so
long polling here creates no getUpdates conflict, and eventbot's outbound
reports keep working unchanged. Hermes (@hermnick_bot) is not touched and
keeps its own token, gateway and polling.

## Authorization

Only SPINE_ASSIST_ALLOWED_ID (Nick's numeric user id) in a PRIVATE chat
whose id equals that user id is answered. Everything else is dropped
silently and counted in telemetry. Callback tokens are opaque random
strings resolved server-side and checked against the card's chat; a token
is not authorization by itself.

## Reliability

update_ids are recorded before handling (duplicate deliveries do nothing);
side-effecting callbacks are single-use; every handler runs under a
try/except that answers "Something went wrong" rather than crashing the
loop. The scheduler tick runs between polls: brief at the configured local
time, once per local day (brief_log), skipped during pause.
"""

from __future__ import annotations

import argparse
import re
import os
import sys
import time
import traceback
from datetime import datetime, timezone

from core import health_facts as hf, property_facts as pf
from surfaces import assistant as A, daily, profile as P, research as R, router
from surfaces.assistant import Reply
from surfaces.store import Store
from surfaces.tg import Bot, TgError, esc, kb

COMMANDS = [("brief", "Today's short overview"), ("health", "Your health data and trends"),
            ("workout", "Quiet dorm-mat routine"), ("mobility", "Ankles, knees, hips"),
            ("property", "Recent property records"), ("quant", "Simulation summary"),
            ("focus", "What goes in your brief"), ("settings", "Brief time, quiet hours"),
            ("glossary", "Plain-English terms"), ("sources", "Where answers come from"),
            ("status", "Operator view"), ("privacy", "What is stored and where"),
            ("profile", "Your confirmed profile"), ("help", "What this bot does"), ("cancel", "Cancel a pending choice")]


def log(event, **kw):
    """stdout -> journal. Never message text or health values."""
    bits = " ".join(f"{k}={v}" for k, v in kw.items())
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {event} {bits}", flush=True)


class Assistant:
    def __init__(self, bot: Bot, store: Store, allowed_id: int, now=None):
        self.bot, self.store, self.allowed = bot, store, int(allowed_id)
        self.research_opener = None          # tests inject a fake fetcher
        self._now = now

    def now(self):
        return self._now() if self._now else datetime.now(timezone.utc)

    # ── sending ────────────────────────────────────────────────────
    def emit(self, chat_id, r: Reply, reply_to=None, silent=False):
        card_id = None
        rows = []
        if r.actions or r.ref:
            card_id = self.store.new_card(chat_id, r.domain, r.ref, r.snapshot)
            btns = [(label, self.store.token(card_id, act, arg)) for label, act, arg in r.actions]
            rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        if r.links:
            links = [(label[:40], url) for label, url in r.links[:6]]
            rows += [links[i:i + 2] for i in range(0, len(links), 2)]
        markup = kb(rows) if rows else None
        if r.photo:
            msg = self.bot.photo(chat_id, r.photo, r.text, markup, silent=silent)
        else:
            msg = self.bot.send(chat_id, r.text, markup, reply_to=reply_to, silent=silent)
        if card_id and msg:
            self.store.attach_msg(card_id, msg["message_id"])
            if r.domain not in ("brief", "status", "general", "focus"):
                self.store.set_pref("context", {"card": card_id, "ts": time.time()})
        return msg

    CONTEXT_TTL_S = 30 * 60

    def context_card(self, text=""):
        """The card a short follow-up refers to. After a batch (three property
        cards at once) pick the one the question fits; if none fits, None."""
        c = self.store.pref("context")
        if not c or time.time() - c.get("ts", 0) > self.CONTEXT_TTL_S:
            return None
        if c.get("batch"):
            cards = [self.store.card(cid) for cid in c["batch"]]
            cards = [x for x in cards if x]
            t = text.lower()
            want = ("MTGE",) if re.search(r"borrow|lend|loan|mortgage|bank", t) else \
                ("DEED", "DEEDO") if re.search(r"sold|sale|buyer|seller|bought|grant", t) else None
            if want:
                fit = [x for x in cards if (x["snapshot"] or {}).get("doc_type") in want]
                if len(fit) == 1:
                    return fit[0]
            return {"ambiguous": cards}
        return self.store.card(c["card"])

    def confirmed_profile(self):
        return P.confirmed(self.store.conn)

    # ── authorization ──────────────────────────────────────────────
    def authorized(self, user, chat) -> bool:
        return (bool(user) and bool(chat) and chat.get("type") == "private"
                and int(user.get("id", 0)) == self.allowed and int(chat.get("id", 0)) == self.allowed)

    # ── update entry point ─────────────────────────────────────────
    def handle(self, upd: dict):
        uid = upd.get("update_id")
        if uid is None or self.store.seen_update(uid):
            self.store.tel("dup_update")
            return
        t0 = time.time()
        if "callback_query" in upd:
            cq = upd["callback_query"]
            msg = cq.get("message") or {}
            if not self.authorized(cq.get("from"), msg.get("chat")):
                self.store.tel("unauthorized", ok=False, detail="callback")
                return
            kind = "callback"
            try:
                self.on_callback(cq)
            except Exception as exc:
                self._fail(msg["chat"]["id"], exc)
        elif "message" in upd:
            m = upd["message"]
            if not self.authorized(m.get("from"), m.get("chat")):
                self.store.tel("unauthorized", ok=False, detail="message")
                return
            kind = "message"
            try:
                self.on_message(m)
            except Exception as exc:
                self._fail(m["chat"]["id"], exc)
        else:
            return
        self.store.tel(kind, ms=int((time.time() - t0) * 1000))

    def _fail(self, chat_id, exc):
        log("handler_error", err=type(exc).__name__)
        traceback.print_exc(limit=3)
        self.store.tel("error", ok=False, detail=type(exc).__name__)
        try:
            self.bot.send(chat_id, "Something went wrong answering that. It's logged; try "
                                   "again or use /help.")
        except TgError:
            pass

    # ── messages ───────────────────────────────────────────────────
    def on_message(self, m):
        chat = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        if not text:
            self.bot.send(chat, "I can only read text messages for now.")
            return
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        if reply_to and not text.startswith("/"):
            card = self.store.card_for_msg(chat, reply_to)
            if card:
                return self.on_card_followup(chat, card, text, m["message_id"])
            self.bot.send(chat, "I don't have a record of that message (it may be older than 30 "
                                "days or from another bot). Ask about it directly or use /help.")
            return
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            cmd = cmd.split("@")[0].lower()
            return self.on_command(chat, cmd, arg.strip())
        return self.on_text(chat, text)

    def on_command(self, chat, cmd, arg):
        s = self.store
        if cmd in ("start", "help"):
            return self.emit(chat, A.help_reply())
        if cmd == "brief":
            return self.emit(chat, daily.build(s, self.now()))
        if cmd == "health":
            if arg:
                return self.on_text(chat, "health " + arg)
            return self.emit(chat, A.health_summary(now=self.now()))
        if cmd == "workout":
            plan = "mobility10" if "mob" in arg else "walkprep8" if "walk" in arg else "strength15"
            return self.emit(chat, A.workout(plan, self.confirmed_profile()))
        if cmd == "mobility":
            return self.emit(chat, A.mobility(self.confirmed_profile()))
        if cmd == "property":
            return self.send_property(chat)
        if cmd == "quant":
            return self.emit(chat, A.quant_summary(now=self.now()))
        if cmd == "glossary":
            return self.emit(chat, A.glossary_reply(arg))
        if cmd == "focus":
            return self.show_focus(chat, arg)
        if cmd == "settings":
            return self.settings(chat, arg)
        if cmd == "sources":
            return self.emit(chat, Reply("<b>Where answers come from</b>\n"
                                         + A.sources_text(list(A.evidence.SOURCES))))
        if cmd == "status":
            return self.emit(chat, self.status())
        if cmd == "privacy":
            return self.emit(chat, A.privacy_reply())
        if cmd == "forget":
            return self.emit(chat, Reply("Delete this assistant's local data?",
                                         actions=[("Forget everything", "forget", "all"),
                                                  ("Only history", "forget", "history"),
                                                  ("Cancel", "noop", "")]))
        if cmd == "profile":
            if arg.lower().startswith("add "):
                P.add_note(s.conn, arg[4:])
                return self.bot.send(chat, "Added to your profile as a confirmed note. Notes are shown "
                                           "back to you; only the listed items change workouts.")
            return self.show_profile(chat)
        if cmd == "cancel":
            s.set_pref("pending", None)
            return self.bot.send(chat, "Cancelled. Nothing is pending.")
        return self.bot.send(chat, "Unknown command. /help lists what I can do.")

    def on_text(self, chat, text):
        card = self.context_card(text)
        if card and "ambiguous" in card and any(router.card_hook(text, x) for x in card["ambiguous"]):
            return self.bot.send(chat, "Which record do you mean? Reply to that card with your "
                                       "question (swipe left on it in Telegram).")
        if card and "ambiguous" not in card and not router.switches_domain(text, card["domain"]) \
                and router.should_follow(text, card):
            self.store.tel("followup", detail=card["domain"])
            return self.on_card_followup(chat, card, text, None)
        return self._route(chat, text)

    def _route(self, chat, text):
        item = A.find_property(text)
        if item:
            return self.emit(chat, A.property_card(item, now=self.now()))
        m = re.search(r"\bwhat(?:'s| is) an? ([a-z&' ]{3,40}?)\??$", text.lower())
        if m:
            code = A.acris_code_reply(m.group(1).strip())
            if code:
                return self.emit(chat, code)
        intent = router.route(text)
        if router.novel(text, intent):
            intent = "unknown"
        self.store.tel("intent", detail=intent)
        if intent == "sexual":
            return self.emit(chat, A.sexual_health())
        if intent == "mobility":
            return self.emit(chat, A.mobility(self.confirmed_profile()))
        if intent == "workout":
            return self.emit(chat, A.workout("strength15", self.confirmed_profile()))
        if intent == "sleep":
            return self.emit(chat, A.sleep_answer(now=self.now()))
        if intent == "improve":
            return self.emit(chat, A.improve(now=self.now()))
        if intent == "activity":
            return self.emit(chat, A.activity_vs_guidance(now=self.now()))
        if intent == "trend":
            m, d = router.trend_metric(text)
            self.emit(chat, A.trend(m, d, now=self.now()))
            return
        if intent == "health":
            return self.emit(chat, A.health_summary(now=self.now()))
        if intent == "glossary":
            return self.emit(chat, A.glossary_reply(router.glossary_term(text) or ""))
        if intent == "property":
            t = router.glossary_term(text)
            if t and len(text) < 40:
                return self.emit(chat, A.glossary_reply(t))
            return self.send_property(chat)
        if intent == "quant" or re.search(r"\b(bot|simulation|quant)\b.*\b(buy|bought|own|hold)", text.lower()):
            pos = A.find_position(text)
            if pos:
                return self.emit(chat, A.quant_why(int(pos["id"])))
            return self.emit(chat, A.quant_summary(now=self.now()))
        if intent == "focus":
            topic = router.focus_topic(text)
            if topic:
                return self.propose_focus(chat, topic)
            return self.show_focus(chat, "")
        if intent == "brief":
            return self.emit(chat, daily.build(self.store, self.now()))
        if intent == "privacy":
            return self.emit(chat, A.privacy_reply())
        r = A.research_answer(text, self.store.conn, online=self.store.pref("research_online"),
                              now=self.now(), opener=self.research_opener)
        if (r.ref or {}).get("status") is not None or not r.links:
            self.store.add_research(text)
        self.store.tel("research", ok=bool(r.links))
        return self.emit(chat, r)

    # ── property / focus / settings ────────────────────────────────
    def send_property(self, chat):
        conn = A.spine_conn()
        items = A.property_items(conn, 3)
        if not items:
            return self.bot.send(chat, "No recent large recorded documents in the store.")
        self.bot.send(chat, "<b>Recorded property documents</b> (largest first). Reply “Explain "
                            "this” to any card. These are filings, not listings or advice.")
        ids = []
        for it in items:
            self.emit(chat, A.property_card(it, conn, now=self.now()), silent=True)
            ids.append(self.store.pref("context")["card"])
        self.store.set_pref("context", {"batch": ids, "ts": time.time()})

    def propose_focus(self, chat, topic):
        label, sensitive = daily.FOCUS_TOPICS.get(topic, (topic, False))
        r = Reply(f"Add <b>{esc(label)}</b> to your daily brief? One short line a day, at your "
                  f"brief time ({esc(self.store.pref('brief_time'))} New York). Pause or cancel "
                  "anytime with /focus.", domain="focus", ref={"kind": "focus_proposal", "topic": topic},
                  actions=[("Yes, add it", "focus_add", topic), ("No", "noop", "")])
        return self.emit(chat, r)

    def show_focus(self, chat, arg):
        fs = self.store.focuses()
        if arg:
            topic = router.focus_topic(arg) or arg.lower()
            if topic in daily.FOCUS_TOPICS:
                return self.propose_focus(chat, topic)
        lines = ["<b>Your focus list</b>"]
        acts = []
        for f in fs:
            label, sens = daily.FOCUS_TOPICS.get(f["topic"], (f["topic"], False))
            lines.append(f"• {esc(label)} — {f['status']}, {f['cadence']}")
            if f["status"] == "active":
                acts.append((f"Pause {label}"[:30], "focus_pause", str(f["id"])))
            else:
                acts.append((f"Resume {label}"[:30], "focus_resume", str(f["id"])))
            acts.append((f"Stop {label}"[:30], "focus_cancel", str(f["id"])))
        if not fs:
            lines.append("Nothing yet. Options: walking, mobility, sleep consistency, dorm "
                         "strength, real-estate basics. Try “/focus mobility”.")
        brief_on = self.store.pref("brief_enabled")
        lines.append(f"\nDaily brief: {'on' if brief_on else 'off'} at "
                     f"{esc(self.store.pref('brief_time'))} New York. /settings to change.")
        return self.emit(chat, Reply("\n".join(lines), domain="focus", ref={"kind": "focus_list"},
                                     actions=acts[:9]))

    def settings(self, chat, arg):
        s = self.store
        a = arg.lower().split()
        msg = None
        if a[:1] == ["brief"] and len(a) >= 2:
            if a[1] in ("on", "off"):
                s.set_pref("brief_enabled", a[1] == "on")
                msg = f"Daily brief {a[1]}."
            elif _hhmm(a[1]):
                s.set_pref("brief_time", _hhmm(a[1]))
                msg = f"Brief time set to {_hhmm(a[1])} New York."
        elif a[:1] == ["weekly"] and len(a) >= 2 and a[1] in ("on", "off"):
            s.set_pref("weekly_enabled", a[1] == "on")
            msg = f"Weekly review {a[1]} (Sundays)."
        elif a[:1] == ["quiet"] and len(a) >= 2 and "-" in a[1]:
            q0, q1 = (_hhmm(x) for x in a[1].split("-", 1))
            if q0 and q1:
                s.set_pref("quiet_start", q0)
                s.set_pref("quiet_end", q1)
                msg = f"Quiet hours {q0}–{q1}: nothing scheduled is sent then."
        elif a[:1] == ["mode"] and len(a) >= 2 and a[1] in ("compact", "detailed"):
            s.set_pref("mode", a[1])
            msg = f"Brief mode: {a[1]}."
        elif a[:1] == ["research"] and len(a) >= 2 and a[1] in ("on", "off"):
            s.set_pref("research_online", a[1] == "on")
            msg = f"Online research (MedlinePlus keyword lookup) {a[1]}."
        elif a[:1] == ["pause"]:
            s.set_pref("paused", True)
            msg = "All scheduled messages paused. /settings resume to restart."
        elif a[:1] == ["resume"]:
            s.set_pref("paused", False)
            msg = "Scheduled messages resumed."
        p = s.prefs()
        text = ((msg + "\n\n") if msg else "") + (
            f"<b>Settings</b>\nDaily brief: {'on' if p['brief_enabled'] else 'off'} at {p['brief_time']} "
            f"New York\nWeekly review: {'on' if p['weekly_enabled'] else 'off'}\nQuiet hours: "
            f"{p['quiet_start']}–{p['quiet_end']}\nOnline research: {'on' if p['research_online'] else 'off'}\nMode: {p['mode']}\nPaused: {'yes' if p['paused'] else 'no'}\n\n"
            "Change with e.g. <code>/settings brief 07:30</code>, <code>/settings brief on</code>, "
            "<code>/settings weekly on</code>, <code>/settings quiet 22:30-07:00</code>, "
            "<code>/settings pause</code>")
        return self.bot.send(chat, text)

    def status(self) -> Reply:
        lines = ["<b>Operator view</b>"]
        try:
            cov = hf.coverage(A.health_conn(), self.now())
            lines.append(f"Health: last import {A._et(cov['last_import'])}; {cov['days']} day(s).")
        except Exception as exc:
            lines.append(f"Health db: {type(exc).__name__}")
        try:
            newest = pf.feed_newest(A.spine_conn())
            lines.append(f"ACRIS: newest recording {str(newest)[:10]} (city dataset paused; ingestion current).")
        except Exception as exc:
            lines.append(f"spine.db: {type(exc).__name__}")
        try:
            from core import governor
            lines.append(f"RAM available: {governor.available_mb()} MB (local model needs ~4.5 GB "
                         "incl. headroom; answers here don't use it).")
        except Exception:
            pass
        try:
            hc = A.health_conn()
            n_sleep = hc.execute("SELECT COUNT(*) FROM sleep_sessions").fetchone()[0]
            n_work = hc.execute("SELECT COUNT(*) FROM workouts").fetchone()[0]
            lines.append(f"Health records: {n_sleep} sleep sessions, {n_work} workouts.")
        except Exception:
            pass
        lines.append("Answers: deterministic + live public-source retrieval; no model in the answer path "
                     f"(online research {'on' if self.store.pref('research_online') else 'off'}).")
        r = self.store.conn.execute(
            "SELECT event, COUNT(*), SUM(1-ok), CAST(AVG(ms) AS INT) FROM telemetry WHERE ts > ? "
            "GROUP BY event", (time.time() - 86400,)).fetchall()
        if r:
            lines.append("Last 24 h: " + ", ".join(f"{e} {n}" + (f" ({f} failed)" if f else "")
                                                   + (f" ~{ms} ms" if ms else "")
                                                   for e, n, f, ms in r))
        try:
            for line in open(os.path.join(A.paths.root(), "var", "log", "health_brief.log"),
                             encoding="utf-8").readlines()[-200:]:
                if "plan skipped" in line:
                    last = line
            lines.append("Last health-brief model note: " + esc(last.strip()[:160]))
        except Exception:
            pass
        return Reply("\n".join(lines), domain="status")

    # ── replies to a card, or short follow-ups to the last card ───
    def on_card_followup(self, chat, card, text, msg_id):
        ref, snap = card["ref"], card["snapshot"] or {}
        kind, dom = ref.get("kind"), card["domain"]
        t = text.lower()

        def has(pat):
            return re.search(pat, t) is not None

        if kind == "research" or dom == "research":
            old = ref.get("keywords") or []
            new = [k for k in R.keywords(text, limit=4) if k not in old and k not in router.COVERED]
            kws = (old[:2] + new[:1]) if new else old
            asp = R.aspect(text)
            if not new and (asp is None or asp == ref.get("aspect")):
                return self.bot.send(chat, "The sources I retrieved have nothing more on that angle "
                                           "than the answer above. Try a different angle (how much, "
                                           "timing, safety), or a clinician for a personal answer.")
            return self.emit(chat, A.research_answer(
                text, self.store.conn, online=self.store.pref("research_online"), now=self.now(),
                opener=self.research_opener, kws=kws, asp=asp, context=" ".join(old)),
                reply_to=msg_id)
        if kind == "acris_item":
            for pat, act in ((r"lender|borrow|bank|buyer|seller|who|owner|part(y|ies)|grantor|grantee", "p_parties"),
                             (r"history|before|else|previous|past|other (record|document)|earlier", "p_history"),
                             (r"sale|loan|sold|refinanc", "p_saleloan"),
                             (r"score|ranking|why .*(send|sent|flag|select|pick)", "p_score"),
                             (r"building|units|built|zoning|how big|floors", "p_building"),
                             (r"worth|value|price|valuation", "p_value")):
                if has(pat):
                    return self.card_action(chat, card, act, None, reply_to=msg_id)
            if has(r"what should i do|next step|what now"):
                return self.emit(chat, Reply(
                    "Next steps for learning (not investing advice): open the official ACRIS "
                    "record to see the pages; tap “Lot history” to see what else was filed; "
                    "tap Save to keep it on your watchlist.", domain="property"), reply_to=msg_id)
            return self.card_action(chat, card, "p_explain", None, reply_to=msg_id)
        if kind == "position":
            if has(r"rule|track|perform|win|record|how (has|did|does) (it|this)"):
                return self.emit(chat, A.quant_rule(snap.get("rule") or ""), reply_to=msg_id)
            return self.emit(chat, A.quant_why(int(ref["position_id"])), reply_to=msg_id)
        if dom == "quant":
            pos = A.find_position(text)
            if pos:
                if has(r"rule|track|perform|win|record"):
                    return self.emit(chat, A.quant_rule(pos["rule"]), reply_to=msg_id)
                return self.emit(chat, A.quant_why(int(pos["id"])), reply_to=msg_id)
            if has(r"change|today|24|new|opened|closed"):
                return self.emit(chat, A.quant_changes(now=self.now()), reply_to=msg_id)
            if has(r"risk|assum|real money|fees|slippage"):
                return self.emit(chat, Reply(A.QUANT_RISKS, domain="quant"), reply_to=msg_id)
            if has(r"position|holding|what (do|does) it (own|hold)"):
                return self.card_action(chat, card, "q_positions", None)
            return self.emit(chat, A.quant_calc(snap), reply_to=msg_id)
        if kind == "workout":
            plan = ref.get("plan", "strength15")
            if has(r"hurt|pain|sore|injur|swell|numb|tingl"):
                return self.emit(chat, Reply(
                    "If a move causes sharp pain, swelling, numbness or tingling, stop that move. "
                    "Pain that lingers into the next day, or any swelling, is worth a campus-health or "
                    "physio check before continuing. Mild muscle effort and soreness a day later are "
                    "normal. For today, the mobility plan is a gentler option: /mobility",
                    domain="health"), reply_to=msg_id)
            if has(r"short|quick|less time|no time|busy"):
                return self.emit(chat, Reply(A.workouts.shorter(plan), domain="health"), reply_to=msg_id)
            if has(r"easier|too hard|hard|tough|beginner"):
                return self.emit(chat, Reply(
                    "Easier: use each move's “Easier” option and do 1 set instead of 2. Keep the "
                    "slow tempo; that matters more than the number of reps.", domain="health"),
                    reply_to=msg_id)
            if has(r"harder|too easy|easy|progress|more"):
                return self.emit(chat, Reply(
                    "Progress one thing at a time, only after a session felt easy: add 2 reps to each "
                    "set, or a third set, or slow the lowering to 3 s. Not all at once.",
                    domain="health"), reply_to=msg_id)
        if dom == "health":
            if has(r"trend|chart|last week|this week|30|month|over time"):
                m, d = router.trend_metric(text)
                return self.emit(chat, A.trend(m, d, now=self.now()))
            if has(r"what should i do|next|improve"):
                return self.emit(chat, A.improve(now=self.now()), reply_to=msg_id)
            if router.novel(text, "health"):
                return self.emit(chat, A.research_answer(
                    text, self.store.conn, online=self.store.pref("research_online"), now=self.now(),
                    opener=self.research_opener), reply_to=msg_id)
            return self.emit(chat, A.explain_health(now=self.now()), reply_to=msg_id)
        if dom == "brief":
            return self.emit(chat, Reply("This brief lists at most three things from your data. "
                                         "Tap a domain button to see the details and the dates "
                                         "behind each line."), reply_to=msg_id)
        return self._route(chat, text)

    # ── profile ────────────────────────────────────────────────────
    def show_profile(self, chat):
        conn = self.store.conn
        P.seed_from_file(conn, A.load_profile())
        items = P.items(conn)
        lines = ["<b>Your profile</b> (self-reported; used only after you confirm)"]
        acts = []
        for it in items:
            mark = {"confirmed": "✅", "unconfirmed": "❔"}.get(it["status"], "")
            lines.append(f"{mark} {esc(it['label'])} — {it['status']}, from {esc(it['origin'])}")
            if not it["key"].startswith("note:"):
                if it["status"] != "confirmed":
                    acts.append((f"Confirm: {it['label']}"[:32], "pf_confirm", it["key"]))
                acts.append((f"Remove: {it['label']}"[:32], "pf_remove", it["key"]))
        if not items:
            lines.append("Nothing saved.")
        lines.append("\n❔ items are suggestions read from your old profile file; workouts ignore them "
                     "until you confirm. Add your own: <code>/profile add sprained left ankle in 2025</code>")
        return self.emit(chat, Reply("\n".join(lines), domain="profile", ref={"kind": "profile"},
                                     actions=acts[:10]))

    # ── callbacks ──────────────────────────────────────────────────
    def on_callback(self, cq):
        chat = cq["message"]["chat"]["id"]
        status, cb, card = self.store.resolve(cq.get("data") or "", chat)
        if status in ("unknown", "foreign"):
            self.store.tel("cb_rejected", ok=False, detail=status)
            return self.bot.answer(cq["id"], "This button isn't valid anymore.")
        if status == "expired":
            self.bot.answer(cq["id"], "This button expired. Ask again for a fresh card.", alert=True)
            return
        action, arg = cb["action"], cb["arg"]
        side_effect = action in ("fb", "focus_add", "focus_pause", "focus_resume", "focus_cancel",
                                 "forget", "p_save", "p_dismiss", "pf_confirm", "pf_remove")
        if side_effect:
            if status == "used" or not self.store.mark_used(cb["token"]):
                return self.bot.answer(cq["id"], "Already recorded.")
        self.bot.answer(cq["id"])
        self.store.tel("cb", detail=action)
        return self.card_action(chat, card, action, arg, cq_msg=cq["message"]["message_id"])

    def card_action(self, chat, card, action, arg, reply_to=None, cq_msg=None):
        snap = card["snapshot"] or {}
        s = self.store
        if action == "noop":
            return self.bot.send(chat, "OK.")
        if action == "trend":
            m, d = arg.split(":")
            return self.emit(chat, A.trend(m, int(d), now=self.now()))
        if action == "improve":
            return self.emit(chat, A.improve(now=self.now()))
        if action == "explain_health":
            return self.emit(chat, A.explain_health(now=self.now()), reply_to=reply_to)
        if action == "sync_help":
            return self.emit(chat, A.sync_help())
        if action == "sources":
            ids = A.evidence.TOPICS.get(arg, [])
            return self.emit(chat, Reply("<b>Sources for that answer</b>\n" + A.sources_text(ids),
                                         links=A.source_links(ids)))
        if action == "workout":
            return self.emit(chat, A.workout(arg or "strength15", self.confirmed_profile()))
        if action == "fb":
            s.feedback(card["id"], arg)
            replies = {"done": "Logged as done (your report, not sensor data). Nice.",
                       "easy": "Logged. Next time: add one set to each move, or slow the lowering to 3 s.",
                       "hard": "Logged. Next time: use the easier option for each move and do 2 sets.",
                       "skip": "Logged as skipped. No reminder will follow.",
                       "later": "OK. I'll include it in tomorrow's brief rather than nag today."}
            if arg in ("done", "skip") and cq_msg:
                self.bot.clear_buttons(chat, cq_msg)
            return self.bot.send(chat, replies.get(arg, "Logged."))
        if action == "focus_propose":
            return self.propose_focus(chat, arg)
        if action == "focus_add":
            label, sensitive = daily.FOCUS_TOPICS.get(arg, (arg, False))
            s.add_focus(arg, sensitive=sensitive)
            on = s.pref("brief_enabled")
            return self.bot.send(chat, f"Added <b>{esc(label)}</b>." + (
                "" if on else " Your daily brief is off; turn it on with <code>/settings brief on</code>."))
        if action in ("focus_pause", "focus_resume", "focus_cancel"):
            st = {"focus_pause": "paused", "focus_resume": "active", "focus_cancel": "cancelled"}[action]
            s.set_focus_status(int(arg), st)
            return self.bot.send(chat, f"Focus {st}.")
        if action == "forget_ask":
            return self.on_command(chat, "forget", "")
        if action == "forget":
            gone = s.forget(arg)
            return self.bot.send(chat, "Deleted local assistant data: " + ", ".join(gone) +
                                 ". Your phone's Health data, the imported health.db, and Telegram "
                                 "history are untouched.")
        if action == "open":
            return {"health": lambda: self.emit(chat, A.health_summary(now=self.now())),
                    "property": lambda: self.send_property(chat),
                    "quant": lambda: self.emit(chat, A.quant_summary(now=self.now())),
                    "focus": lambda: self.show_focus(chat, "")}.get(arg, lambda: None)()
        if action == "p_explain":
            return self.emit(chat, A.property_explain(snap), reply_to=reply_to)
        if action == "p_saleloan":
            return self.emit(chat, A.property_saleloan(snap), reply_to=reply_to)
        if action == "p_building":
            return self.emit(chat, A.property_building(snap), reply_to=reply_to)
        if action == "p_score":
            return self.emit(chat, Reply(esc(pf.SCORE_EXPLAIN), domain="property"), reply_to=reply_to)
        if action in ("p_save", "p_dismiss"):
            key = "watchlist" if action == "p_save" else "dismissed"
            lst = s.pref(key) or []
            if snap.get("doc_id") not in lst:
                lst.append(snap.get("doc_id"))
                s.set_pref(key, lst)
            return self.bot.send(chat, "Saved to your local watchlist." if action == "p_save"
                                 else "Dismissed locally. The source record is unchanged.")
        if action == "p_parties":
            return self.emit(chat, A.property_parties(snap), reply_to=reply_to)
        if action == "p_history":
            return self.emit(chat, A.property_history(snap), reply_to=reply_to)
        if action == "p_value":
            return self.emit(chat, Reply(
                "Value: a mortgage amount is a loan, and a deed amount is a past price. Assessed value "
                "on the Building facts card is a tax figure, usually far below market value. Our "
                "valuation model isn't shown yet: its backtested error is still above the 25% bar, so "
                "I won't give you a number.", domain="property"), reply_to=reply_to)
        if action == "r_aspect":
            old = (card["ref"] or {}).get("keywords") or []
            return self.emit(chat, A.research_answer(
                " ".join(old), s.conn, online=s.pref("research_online"), now=self.now(),
                opener=self.research_opener, kws=old, asp=arg, context=" ".join(old)))
        if action == "w_short":
            return self.emit(chat, Reply(A.workouts.shorter(arg or "strength15"), domain="health"))
        if action == "profile":
            return self.show_profile(chat)
        if action in ("pf_confirm", "pf_remove"):
            st = "confirmed" if action == "pf_confirm" else "removed"
            P.set_status(s.conn, arg, st)
            return self.bot.send(chat, f"Profile item {st}. /profile to review.")
        if action == "q_changes":
            return self.emit(chat, A.quant_changes(now=self.now()))
        if action == "q_rule":
            return self.emit(chat, A.quant_rule(arg))
        if action == "q_positions":
            for r in A.quant_positions():
                self.emit(chat, r, silent=True)
            return None
        if action == "q_calc":
            return self.emit(chat, A.quant_calc(snap))
        if action == "q_risks":
            return self.emit(chat, Reply(A.QUANT_RISKS, domain="quant"))
        if action == "q_why":
            return self.emit(chat, A.quant_why(int(card["ref"]["position_id"])), reply_to=reply_to)
        return self.bot.send(chat, "That button isn't supported anymore.")

    # ── scheduler ──────────────────────────────────────────────────
    def tick(self):
        s = self.store
        p = s.prefs()
        if p["paused"]:
            return None
        local = self.now().astimezone(hf.TZ)
        hhmm = local.strftime("%H:%M")
        if _in_quiet(hhmm, p["quiet_start"], p["quiet_end"]) and hhmm < p["brief_time"]:
            return None
        day = local.date().isoformat()
        sent = None
        if p["brief_enabled"] and hhmm >= p["brief_time"] and not s.brief_sent(day, "daily"):
            if s.mark_brief(day, "daily"):
                self.emit(self.allowed, daily.build(s, self.now()))
                sent = "daily"
        if (p["weekly_enabled"] and local.strftime("%a").lower() == p["weekly_day"]
                and hhmm >= p["brief_time"] and not s.brief_sent(day, "weekly")):
            if s.mark_brief(day, "weekly"):
                self.emit(self.allowed, daily.weekly(s, self.now()), silent=True)
                sent = "weekly"
        return sent


def _hhmm(s):
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except (ValueError, AttributeError):
        pass
    return None


def _in_quiet(t, start, end):
    return (start <= t or t < end) if start > end else (start <= t < end)


def load_env():
    """Read SPINE_ASSIST_* from the repo .env (mode 600 enforced) if not set."""
    path = os.path.join(A.paths.root(), ".env")
    if os.path.exists(path):
        if oct(os.stat(path).st_mode & 0o777) != "0o600":
            raise SystemExit(".env must be mode 600")
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k.startswith("SPINE_ASSIST_") and k not in os.environ:
                os.environ[k] = v.strip().strip('"\'')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--brief-now", action="store_true")
    ap.add_argument("--set-commands", action="store_true")
    a = ap.parse_args(argv)
    load_env()
    token = os.environ.get("SPINE_ASSIST_BOT_TOKEN")
    allowed = os.environ.get("SPINE_ASSIST_ALLOWED_ID")
    if not token or not allowed:
        print("SPINE_ASSIST_BOT_TOKEN and SPINE_ASSIST_ALLOWED_ID must be set", file=sys.stderr)
        return 78
    bot, store = Bot(token), Store()
    app = Assistant(bot, store, int(allowed))
    if a.set_commands:
        bot.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        log("commands_set", n=len(COMMANDS))
    if a.brief_now:
        app.emit(app.allowed, daily.build(store))
        return 0
    offset = (store.max_update() or 0) + 1 or None
    log("start", once=a.once)
    while True:
        try:
            ups = bot.updates(offset, timeout=0 if a.once else 25)
        except TgError as exc:
            log("poll_error", err=str(exc)[:120])
            if a.once:
                return 1
            time.sleep(10)
            continue
        for u in ups:
            offset = u["update_id"] + 1
            app.handle(u)
        try:
            app.tick()
        except Exception as exc:
            log("tick_error", err=type(exc).__name__)
        store.prune()
        if a.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())

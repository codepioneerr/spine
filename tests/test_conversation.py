"""
Conversation round 2: research synthesis, follow-ups, profile confirmation,
computed workout durations, exact sources, consistent snapshot times, and
the Health Auto Export contract. Synthetic fixtures; network is faked.

    python3 -m unittest tests.test_conversation
"""
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_assistant import Base, U, _iso  # noqa: E402
from surfaces import research as R, workouts as W, profile as P  # noqa: E402
from core import health, property_live as L  # noqa: E402

MEDLINE_XML = """<?xml version="1.0"?><nlmSearchResult><list>
<document url="https://medlineplus.gov/caffeine.html"><content name="title">&lt;span class="qt0"&gt;Caffeine&lt;/span&gt;</content>
<content name="FullSummary">&lt;p&gt;Caffeine is a stimulant.&lt;/p&gt;&lt;p&gt;For most people, it is not harmful to consume up to 400mg of caffeine a day.&lt;/p&gt;&lt;p&gt;You should check with your provider whether to limit or avoid caffeine if you:&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Have sleep disorders, including insomnia&lt;/li&gt;&lt;/ul&gt;</content></document>
<document url="https://medlineplus.gov/posture.html"><content name="title">Good Posture</content>
<content name="FullSummary">&lt;p&gt;Caffeine and sleep and posture and caffeine and sleep.&lt;/p&gt;</content></document>
</list></nlmSearchResult>"""

ESEARCH = json.dumps({"esearchresult": {"idlist": ["1", "2", "3"]}})
EFETCH = """<?xml version="1.0"?><PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID>1</PMID><Article><Journal><Title>Sleep Med Rev</Title></Journal>
<ArticleTitle>The effect of caffeine on subsequent sleep: a meta-analysis</ArticleTitle>
<Abstract><AbstractText>This systematic review investigated the effect of caffeine on sleep. We searched five databases. Caffeine consumption reduced total sleep time by 45 min and sleep efficiency by 7%. The evidence was of low certainty and further research is needed on caffeine and sleep.</AbstractText></Abstract></Article>
<ArticleDate><Year>2023</Year></ArticleDate></MedlineCitation><PubmedData/>
<PublicationTypeList><PublicationType>Meta-Analysis</PublicationType></PublicationTypeList></PubmedArticle>
<PubmedArticle><MedlineCitation><PMID>2</PMID><Article><Journal><Title>Water Res</Title></Journal>
<ArticleTitle>Removal of pharmaceuticals in wastewater plants</ArticleTitle>
<Abstract><AbstractText>Caffeine and sleep aids were removed by caffeine treatment plants.</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle>
<PubmedArticle><MedlineCitation><PMID>3</PMID><Article><Journal><Title>J Ortho</Title></Journal>
<ArticleTitle>Caffeine and sleep after total knee arthroplasty in older adults</ArticleTitle>
<Abstract><AbstractText>Caffeine intake was associated with worse sleep in patients after surgery.</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle>
</PubmedArticleSet>"""


def fake_net(seen, offline=False):
    class Resp:
        def __init__(s, b): s.b = b
        def read(s): return s.b
        def __enter__(s): return s
        def __exit__(s, *a): return False

    def opener(req, timeout=None):
        url = req.full_url
        seen.append(url)
        if offline:
            raise OSError("offline")
        if "wsearch.nlm.nih.gov" in url:
            return Resp(MEDLINE_XML.encode())
        if "esearch.fcgi" in url:
            return Resp(ESEARCH.encode())
        if "efetch.fcgi" in url:
            return Resp(EFETCH.encode())
        if "636b-3b5g" in url:
            return Resp(json.dumps([
                {"document_id": "M1", "party_type": "1", "name": "WILLOUGHBY BORROWER LP", "city": "NEW YORK", "state": "NY"},
                {"document_id": "M1", "party_type": "2", "name": "BIG LENDER LLC", "city": "PHILA", "state": "PA"}]).encode())
        if "8h5j-fqxa" in url:
            return Resp(json.dumps([{"document_id": "M1"}, {"document_id": "S1"}, {"document_id": "D0"}]).encode())
        if "bnx9-e6tj" in url:
            return Resp(json.dumps([
                {"document_id": "M1", "doc_type": "MTGE", "recorded_datetime": "2026-08-31T00:00:00.000", "document_amt": "42648000"},
                {"document_id": "S1", "doc_type": "SAT", "recorded_datetime": "2026-08-31T00:00:00.000", "document_amt": "0"},
                {"document_id": "D0", "doc_type": "DEED", "recorded_datetime": "1998-03-01T00:00:00.000", "document_amt": "0"}]).encode())
        raise OSError("unexpected url " + url)
    return opener


class TestResearch(unittest.TestCase):
    def test_keywords_aspect_and_stopwords(self):
        self.assertEqual(R.keywords("how much caffeine before bed is too much"), ["caffeine"])
        self.assertEqual(R.aspect("how much caffeine before bed is too much"), "dose")
        self.assertEqual(R.aspect("is creatine safe?"), "safety")

    def test_synthesis_relevance_population_uncertainty(self):
        seen = []
        r = R.lookup("does caffeine affect my sleep", opener=fake_net(seen))
        self.assertEqual(r["status"], "ok")
        titles = [s["title"] for s in r["sources"]]
        self.assertNotIn("Good Posture", titles)                       # no title hit
        self.assertFalse(any("wastewater" in t for t in titles))        # research title gate
        texts = " ".join(p["text"] for p in r["points"])
        self.assertIn("reduced total sleep time by 45 min", texts)      # finding, not the aim
        self.assertNotIn("investigated", texts)
        self.assertNotIn("databases", texts)                            # methods excluded
        self.assertNotIn("span", texts + " ".join(titles))              # highlight markup stripped
        self.assertTrue(any("low certainty" in u["text"] for u in r["uncertainty"]))
        pops = [s["population"] for s in r["sources"] if s.get("population")]
        self.assertIn("people after joint replacement", pops)
        for p in r["points"]:
            self.assertTrue(1 <= p["n"] <= len(r["sources"]))
        terms = [u.split("term=")[1].split("&")[0] for u in seen if "term=" in u]
        self.assertTrue(all("my" not in t.split("+") for t in terms))

    def test_gap_reported_not_filled(self):
        r = R.lookup("is caffeine safe", opener=fake_net([]))
        self.assertEqual(r["aspect"], "safety")
        self.assertTrue(r["gap"] in (None, "safety"))
        r = R.lookup("what is the timing for caffeine", opener=fake_net([]), asp="timing")
        self.assertEqual(r["gap"], "timing")

    def test_insufficient_sleep_is_not_uncertainty(self):
        self.assertIsNone(R.UNCERTAIN.search("caffeine in response to insufficient sleep"))
        self.assertIsNotNone(R.UNCERTAIN.search("there is insufficient evidence"))


class TestRoundThree(unittest.TestCase):
    """Cases found by unscripted questions on real data (2026-10-06)."""

    def test_everyday_words_become_indexed_terms(self):
        self.assertEqual(R.keywords("I had two coffees around 4pm, is that going to wreck my sleep tonight?"),
                         ["caffeine", "sleep"])
        self.assertEqual(R.aspect("I had two coffees around 4pm"), "timing")
        self.assertEqual(R.keywords("does protein timing matter for building muscle?"), ["protein", "muscle"])
        self.assertEqual(R.keywords("is it bad that I sit in lectures all day?"), ["sedentary"])
        self.assertEqual(R.keywords("should I stretch before or after boxing practice?"), ["stretching", "boxing"])

    def test_objectives_are_not_findings_and_needs_are_uncertainty(self):
        self.assertIsNotNone(R.AIMS.search("To investigate the effectiveness of workplace interventions"))
        self.assertIsNotNone(R.UNCERTAIN.search("larger trials are needed to determine"))

    def test_research_card_follow_rules(self):
        from surfaces import router
        card = {"domain": "research", "ref": {"kind": "research", "keywords": ["protein", "muscle"]}}
        self.assertTrue(router.should_follow("how much is too much?", card))
        self.assertTrue(router.should_follow("what about standing desks", card))       # explicit extension
        self.assertFalse(router.should_follow("is it bad that I sit in lectures all day?", card))
        self.assertFalse(router.should_follow("does caffeine timing matter for sleep?", card))

    def test_broadening_is_reported(self):
        seen = []
        r = R.lookup("caffeine boxing", opener=fake_net(seen), kws=["caffeine", "boxing"])
        self.assertEqual(r["keywords"], ["caffeine"])
        self.assertEqual(r["broadened_from"], ["caffeine", "boxing"])


class TestWorkouts(unittest.TestCase):
    def test_durations_include_rests_and_transitions(self):
        for pid, (name, warm, _, moves) in W.PLANS.items():
            total = W.plan_seconds(pid)
            parts = warm + sum(W.MOVES[m].seconds() for m in moves) + W.TRANSITION_S * (len(moves) - 1)
            self.assertEqual(total, parts)
            self.assertIn(f"about {round(total / 60)} min total", W.render(pid))
        bridge = W.MOVES["glute_bridge"]                     # 2 x 12 reps @4 s, 45 s rest
        self.assertEqual(bridge.seconds(), 2 * 12 * 4 + 45)
        side = W.MOVES["side_plank"]                         # 2 x 20 s per side, 10 s switch, 30 s rest
        self.assertEqual(side.seconds(), 2 * (40 + 10) + 30)

    def test_profile_cues_need_confirmation_and_match_plan(self):
        self.assertEqual(W.profile_notes({}, "strength15"), [])
        notes = " ".join(W.profile_notes({"flat_feet": True, "knee_valgus": True}, "strength15"))
        self.assertIn("2nd–3rd toe", notes)
        self.assertNotIn("Flat feet", notes)                 # no balance/calf move in this plan


class TestConversation(Base):
    def setUp(self):
        super().setUp()
        self.seen = []
        self.app.research_opener = fake_net(self.seen)
        L._orig_get = L._get
        L._get = lambda ds, params, opener=None: L._orig_get(ds, params, fake_net(self.seen))

    def tearDown(self):
        L._get = L._orig_get
        super().tearDown()

    def test_research_followup_keeps_topic_and_changes_aspect(self):
        self.msg("Does caffeine affect my sleep?")
        first = self.last()
        self.assertIn("Researched answer: caffeine + sleep", first)
        self.assertIn("no AI model", first)
        self.msg("how much is too much?")
        t = self.last()
        self.assertIn("caffeine", t)
        self.assertIn("focusing on how much", t)
        self.assertIn("follow-up on", t)
        self.assertIn("400mg", t)

    def test_sources_buttons_are_exact_pages(self):
        self.msg("Does caffeine affect my sleep?")
        btns = json.dumps(self.bot.sent[-1]["buttons"])
        self.assertIn("https://medlineplus.gov/caffeine.html", btns)
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/1/", btns)
        self.msg("How does my activity compare with general guidance?")
        mid = self.bot.sent[-1]["id"]
        self.press(self.tokens(mid)["sources"], mid)
        self.assertIn("https://www.cdc.gov/physical-activity-basics/guidelines/adults.html",
                      json.dumps(self.bot.sent[-1]["buttons"]))

    def test_property_followups_use_live_records_of_that_card(self):
        self.msg("/property")
        mort = [c for c in self.bot.sent if c["buttons"] and "Mortgage" in c["text"]][0]
        self.msg("who was the lender?")                      # context: last card sent
        t = self.last()
        self.assertIn("Mortgagee/lender", t)
        self.assertIn("BIG LENDER LLC", t)
        self.assertTrue(any("document_id=M1" in u for u in self.seen))
        self.msg("what else happened at this building?", reply=mort["id"])
        t = self.last()
        self.assertIn("refinancing", t)
        self.assertIn("Interpretation (not proof)", t)
        self.assertIn("satisfaction of mortgage", t)

    def test_quant_followups(self):
        self.msg("/quant")
        self.msg("what changed today?")
        self.assertIn("What changed in the simulation", self.last())
        mid = self.bot.sent[-2]["id"]
        self.press(self.tokens(mid)["q_positions"], mid)
        pos = [s for s in self.bot.sent if "BTC-USD" in s["text"] and s["buttons"]][0]
        self.msg("how has this rule done?", reply=pos["id"])
        self.assertIn("crypto_mention", self.last())
        self.assertIn("Closed trades: 1", self.last())

    def test_domain_switch_is_not_a_followup(self):
        self.msg("/property")
        self.msg("what about my sleep?")
        self.assertNotIn("Parties", self.last())
        self.assertNotIn("lot", self.last().lower()[:40])

    def test_workout_followups(self):
        self.msg("/workout")
        self.msg("my knee hurts during it")
        self.assertIn("stop that move", self.last())
        self.msg("/workout")
        self.msg("is there a shorter version?")
        self.assertIn("short version", self.last())

    def test_profile_confirm_and_remove(self):
        with open(os.environ["SPINE_HEALTH_PROFILE"], "w") as fh:
            fh.write("- Structure: knock-knees (valgus), flat feet.\n- Trains boxing.\n- Wants sexual health tips")
        self.msg("/profile")
        t = self.last()
        self.assertIn("unconfirmed", t)
        self.assertNotIn("sexual", t.lower())                 # goals are not profile facts
        self.msg("/workout")
        self.assertNotIn("you confirmed", self.last())       # unconfirmed: not used
        self.msg("/profile")
        mid = self.bot.sent[-1]["id"]
        toks = {a + ":" + (arg or ""): t_ for t_, a, arg in self.store.conn.execute(
            "SELECT token, action, arg FROM callbacks WHERE card_id=(SELECT id FROM cards WHERE msg_id=?)",
            (mid,))}
        self.press(toks["pf_confirm:knee_valgus"], mid)
        self.press(toks["pf_remove:flat_feet"], mid)
        self.msg("/workout")
        self.assertIn("you confirmed", self.last())
        self.msg("/profile")
        self.assertNotIn("Flat feet", self.last())
        P.seed_from_file(self.store.conn, "flat feet")       # re-seeding never resurrects a removal
        self.assertNotIn("flat_feet", [i["key"] for i in P.items(self.store.conn)])

    def test_address_and_code_questions(self):
        self.msg("tell me about 2 Loan Ave")
        self.assertIn("Mortgage", self.last())
        self.msg("what's a sundry agreement?")
        self.assertIn("ACRIS code SAGE", self.last())
        self.assertNotIn("party 1 is the party 1", self.last().lower())

    def test_snapshot_times_consistent_card_and_brief(self):
        self.env.sample("step_count", "2026-10-05", 343)
        self.env.imported(_iso(2026, 10, 6, 8, 7))
        self.msg("/health")
        card = self.last()
        self.msg("/brief")
        brief = self.last()
        stamp = re.search(r"Health data as of the ([^<;]+?) export", card).group(1)
        self.assertIn(stamp, brief)
        self.assertIn("4:07 AM ET", stamp)                    # 08:07 UTC = 04:07 EDT


class TestHealthAutoExportContract(unittest.TestCase):
    """Payloads copied from the app's documentation (help.healthyapps.dev,
    updated 2026-08-23): summarized sleep, unsummarized stages, v2 workout."""

    def test_documented_formats_ingest(self):
        import tempfile
        path = tempfile.mktemp()
        c = health.connect(path)
        payload = {"data": {
            "metrics": [{"name": "sleep_analysis", "units": "hr", "data": [
                {"date": "2024-02-06", "totalSleep": 7.5, "asleep": 7.0, "core": 3.5, "deep": 1.5,
                 "rem": 2.0, "sleepStart": "2024-02-05 23:00:00 -0800",
                 "sleepEnd": "2024-02-06 06:30:00 -0800", "inBed": 8.0,
                 "inBedStart": "2024-02-05 22:45:00 -0800", "inBedEnd": "2024-02-06 06:45:00 -0800"}]},
                {"name": "sleep_analysis", "units": "hr", "data": [
                    {"startDate": "2024-02-07 23:00:00 -0800", "endDate": "2024-02-07 23:30:00 -0800",
                     "qty": 0.5, "value": "Core"},
                    {"startDate": "2024-02-07 23:30:00 -0800", "endDate": "2024-02-08 00:30:00 -0800",
                     "qty": 1.0, "value": "Deep"}]}],
            "workouts": [{"id": "W1", "name": "Kickboxing", "start": "2024-02-06 18:00:00 -0800",
                          "end": "2024-02-06 18:45:00 -0800", "duration": 2700,
                          "activeEnergyBurned": {"qty": 410, "units": "kcal"},
                          "heartRate": {"min": {"qty": 120, "units": "bpm"},
                                        "avg": {"qty": 150, "units": "bpm"},
                                        "max": {"qty": 175, "units": "bpm"}}}]}}
        res = health.ingest(payload, c)
        self.assertEqual(res["errors"], 0)
        rows = c.execute("SELECT total_sleep_minutes, deep_sleep_minutes FROM sleep_sessions "
                         "ORDER BY start_time").fetchall()
        self.assertEqual(rows[0], (450.0, 90.0))
        self.assertEqual(rows[1][1], 60.0)
        w = c.execute("SELECT workout_type, duration_minutes, active_energy_kcal, avg_heart_rate "
                      "FROM workouts").fetchone()
        self.assertEqual(w, ("Kickboxing", 45.0, 410.0, 150.0))
        os.remove(path)


class TestAcrisCodes(unittest.TestCase):
    def test_roles_and_names_from_official_table(self):
        self.assertEqual(L.roles("MTGE"), ("mortgagor/borrower", "mortgagee/lender"))
        self.assertEqual(L.type_name("MCON"), "memorandum of contract")   # not "consolidation"
        self.assertGreater(len(L.CODES), 100)


if __name__ == "__main__":
    unittest.main()

"""
surfaces.evidence — vetted sources the assistant may cite.

Every entry was opened and read on RETRIEVED; `claim` is what the page
actually supports, `limits` what it does not. The assistant cites only from
here. A question this registry does not cover gets an honest "not yet
researched" plus a saved research request, never an invented citation.

Refresh policy: re-check each entry when older than REFRESH_DAYS
(bin/assist --check-sources lists the stale ones).
"""

RETRIEVED = "2026-10-06"
REFRESH_DAYS = 180

SOURCES = {
    "cdc_activity": {
        "title": "CDC — Adult Activity: An Overview",
        "url": "https://www.cdc.gov/physical-activity-basics/guidelines/adults.html",
        "updated": "2023-12-20",
        "claim": "Adults need 150 min/week of moderate-intensity activity (or 75 min vigorous) "
                 "and 2 days/week of muscle-strengthening activity working all major muscle groups.",
        "limits": "Population guidance; says nothing about an individual's ideal dose.",
    },
    "cdc_sleep": {
        "title": "CDC — About Sleep",
        "url": "https://www.cdc.gov/sleep/about/index.html",
        "updated": "2024-05-15",
        "claim": "Adults 18-60 should get 7 or more hours of sleep per night.",
        "limits": "Population guidance. Wearable sleep estimates are not polysomnography.",
    },
    "niddk_ed_causes": {
        "title": "NIDDK — Symptoms & Causes of Erectile Dysfunction",
        "url": "https://www.niddk.nih.gov/health-information/urologic-diseases/erectile-dysfunction/symptoms-causes",
        "updated": "2024-10",
        "claim": "Physical inactivity, smoking, excess alcohol, drug use, and heart/blood-vessel "
                 "disease (atherosclerosis, high blood pressure) are linked with erectile problems; "
                 "ED can be a sign of another health problem.",
        "limits": "Describes risk factors for a condition; does not mean you have it, and wearable "
                  "data cannot measure sexual function or hormones.",
    },
    "niddk_ed_diet": {
        "title": "NIDDK — Eating, Diet & Nutrition for Erectile Dysfunction",
        "url": "https://www.niddk.nih.gov/health-information/urologic-diseases/erectile-dysfunction/eating-diet-nutrition",
        "updated": "",
        "claim": "Heart-healthy eating and a healthy weight support blood-vessel health relevant "
                 "to erectile function.",
        "limits": "General lifestyle guidance; not a supplement recommendation.",
    },
    "niddk_kegel": {
        "title": "NIDDK — Kegel Exercises",
        "url": "https://www.niddk.nih.gov/health-information/urologic-diseases/kegel-exercises",
        "updated": "2021-11",
        "claim": "Pelvic floor training can help men and women with weakened pelvic floor "
                 "muscles, but is not right for everyone: check with a clinician first; don't do "
                 "them while urinating; overdoing them can cause straining.",
        "limits": "Not a blanket routine for people without symptoms.",
    },
    "cdc_sti_testing": {
        "title": "CDC — Getting Tested for STIs",
        "url": "https://www.cdc.gov/sti/testing/index.html",
        "updated": "2026-03-17",
        "claim": "CDC gives testing frequencies by group (e.g. yearly chlamydia/gonorrhea for "
                 "sexually active women under 25; at least yearly syphilis/chlamydia/gonorrhea/HIV "
                 "for sexually active gay and bisexual men, more often with multiple partners).",
        "limits": "Which schedule applies depends on personal context only you or a clinician know.",
    },
    "acris": {
        "title": "NYC Department of Finance — ACRIS",
        "url": "https://www.nyc.gov/site/finance/property/acris.page",
        "updated": "",
        "claim": "ACRIS provides recorded property documents (deeds etc.) for Manhattan, Queens, "
                 "the Bronx and Brooklyn from 1966 to the present.",
        "limits": "Recording a document is not proof of clean title or a current listing.",
    },
    "acris_codes": {
        "title": "NYC Open Data — ACRIS Document Control Codes",
        "url": "https://data.cityofnewyork.us/d/7isb-wh4c",
        "updated": "",
        "claim": "DEED = deed (grantor/seller -> grantee/buyer); MTGE = mortgage "
                 "(mortgagor/borrower, mortgagee/lender); SAT = satisfaction of mortgage.",
        "limits": "Codes define the document type, not the economic substance of a deal.",
    },
    "pluto": {
        "title": "NYC Open Data — Primary Land Use Tax Lot Output (PLUTO)",
        "url": "https://data.cityofnewyork.us/d/64uk-42ks",
        "updated": "2026-08-24 (dataset rows updated)",
        "claim": "Lot-level land use and building facts for NYC tax lots.",
        "limits": "The data dictionary PDF version was not machine-verified against this release.",
    },
    "goata_site": {
        "title": "GOATA Movement (official site)",
        "url": "https://www.goatamovement.com/",
        "updated": "",
        "claim": "GOATA is a commercial movement/gait coaching system founded by Gary Scheffler, "
                 "built on video analysis of movement patterns.",
        "limits": "Practitioner material. A search on 2026-10-06 found no peer-reviewed controlled "
                  "trials of GOATA itself; treat specific alignment claims as unproven.",
    },
}

# Topic -> source ids, used to attach links to answers.
TOPICS = {
    "activity": ["cdc_activity"],
    "sleep": ["cdc_sleep"],
    "sexual": ["niddk_ed_causes", "niddk_ed_diet", "cdc_sti_testing"],
    "pelvic": ["niddk_kegel"],
    "mobility": ["cdc_activity", "goata_site"],
    "property": ["acris", "acris_codes", "pluto"],
}


def cite(ids) -> list[dict]:
    return [dict(SOURCES[i], id=i) for i in ids if i in SOURCES]


def stale(today: str) -> list[str]:
    from datetime import date
    age = (date.fromisoformat(today) - date.fromisoformat(RETRIEVED)).days
    return list(SOURCES) if age > REFRESH_DAYS else []

"""Audit freshly imported Queue rows before they are dialed. Read-only: writes nothing.

    python scripts/audit_import.py --last 71          # the last 71 rows of the Queue
    python scripts/audit_import.py --after "Pintail Restoration"   # everything after that company's row

Checks: untouched, valid +1 numbers (real area codes), duplicates (phone / website / same name+city) against
the rest of the Queue and inside the block, timezone valid and consistent with the state, blanks, toll-free,
odd characters, franchise-looking names, and -- the one that matters for outreach -- whether a new company
shares a website with a company you already emailed (email_track).
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from app import google_sheets as gs  # noqa: E402
from app.config import CFG  # noqa: E402

CANON = re.compile(r"\+1[2-9]\d\d[2-9]\d{6}")
E, C, M, P = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"
STATE_TZ = {"Alabama": {C}, "Arizona": {"America/Phoenix", M}, "Arkansas": {C}, "California": {P}, "Colorado": {M}, "Connecticut": {E},
            "Delaware": {E}, "Florida": {E, C}, "Georgia": {E}, "Idaho": {M, "America/Boise", P}, "Illinois": {C},
            "Indiana": {E, C, "America/Indianapolis", "America/Detroit"}, "Iowa": {C}, "Kansas": {C, M}, "Kentucky": {E, C},
            "Louisiana": {C}, "Maine": {E}, "Maryland": {E}, "Massachusetts": {E}, "Michigan": {E, C, "America/Detroit"},
            "Minnesota": {C}, "Mississippi": {C}, "Missouri": {C}, "Montana": {M}, "Nebraska": {C, M}, "Nevada": {P},
            "New Hampshire": {E}, "New Jersey": {E}, "New Mexico": {M}, "New York": {E}, "North Carolina": {E}, "North Dakota": {C, M},
            "Ohio": {E}, "Oklahoma": {C}, "Oregon": {P, "America/Boise"}, "Pennsylvania": {E}, "Rhode Island": {E},
            "South Carolina": {E}, "South Dakota": {C, M}, "Tennessee": {E, C}, "Texas": {C, M}, "Utah": {M}, "Vermont": {E},
            "Virginia": {E}, "Washington": {P}, "West Virginia": {E}, "Wisconsin": {C}, "Wyoming": {M}, "District of Columbia": {E}}
FRANCHISE = re.compile(r"servpro|servicemaster|puroclean|belfor|911 rest|paul davis|steamatic|rainbow|restoration 1|first onsite|"
                       r"munters|dryfast|blackmon|rapid response", re.I)
INDUSTRY = re.compile(r"restor|water|flood|fire|mold|damage|dry|clean|disaster|remed|rescue|mitigat|storm|smoke|environment|construct|"
                      r"services|recover|renew|dki|pro|leak|roof|remodel|build|contract", re.I)
SHARED_HOSTS = {"facebook.com", "instagram.com", "linktr.ee", "yelp.com", "google.com", "business.site", "wixsite.com", "godaddysites.com"}


def dom(v) -> str:
    d = re.sub(r"^https?://(www\.)?|[/?#].*$", "", str(v).strip().lower())
    return d


def norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(llc|inc|co|company|restoration|services|and)\b", "", str(s).lower()))


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--last", type=int, help="audit the last N Queue rows")
    g.add_argument("--after", help="audit every row after the (last) row whose company name contains this text")
    a = ap.parse_args()
    for _ in range(6):
        try:
            q = gs._service().spreadsheets().values().get(spreadsheetId=CFG.sheet_id, range=f"{CFG.sched_queue_tab}!A1:AZ100000",
                                                           valueRenderOption="UNFORMATTED_VALUE").execute()["values"]
            break
        except Exception:  # noqa: BLE001
            time.sleep(4)
    h = q[0]
    rows = [(i, dict(zip(h, x + [""] * (len(h) - len(x))))) for i, x in enumerate(q[1:], start=2)]
    if a.last:
        new = rows[-a.last:]
    else:
        hit = [i for i, r in rows if a.after.lower() in str(r["company_name"]).lower()]
        if not hit:
            sys.exit(f"no row matches {a.after!r}")
        new = [(i, r) for i, r in rows if i > hit[-1]]
    start = new[0][0]
    old = [(i, r) for i, r in rows if i < start]
    print(f"Queue rows: {len(rows)} | new block: sheet rows {new[0][0]}..{new[-1][0]} = {len(new)} rows")
    print("untouched (no attempts/history/last call):", all(not r["call_sid_history"] and not r["current_quarter_attempts"] and not r["last_call_date"] for i, r in new),
          "| sources:", dict(collections.Counter(str(r["source"]) or "(blank)" for i, r in new)))
    oldp = collections.Counter(str(r["phone_e164"]) for i, r in old)
    newp = collections.Counter(str(r["phone_e164"]) for i, r in new)
    oldd = collections.defaultdict(list)
    for i, r in old:
        if r["domain"] and dom(r["domain"]) not in SHARED_HOSTS:
            oldd[dom(r["domain"])].append((i, r))
    newd = collections.Counter(dom(r["domain"]) for i, r in new if r["domain"] and dom(r["domain"]) not in SHARED_HOSTS)
    oldn = collections.defaultdict(list)
    for i, r in old:
        oldn[(norm(r["company_name"]), str(r["city"]).lower())].append(i)

    def tzbad(r):
        try:
            ZoneInfo(r["timezone"])
            return False
        except Exception:  # noqa: BLE001
            return True

    checks = [
        ("blank company", lambda r: not str(r["company_name"]).strip()),
        ("phone not +1XXXXXXXXXX / bad area code", lambda r: not CANON.fullmatch(str(r["phone_e164"]))),
        ("phone cell not text", lambda r: not isinstance(r["phone_e164"], str)),
        ("phone already on an older row", lambda r: oldp[str(r["phone_e164"])] > 0),
        ("phone repeated inside the new rows", lambda r: newp[str(r["phone_e164"])] > 1),
        ("blank/invalid timezone", tzbad),
        ("state/timezone mismatch", lambda r: str(r["state"]) in STATE_TZ and r["timezone"] not in STATE_TZ[str(r["state"])]),
        ("unrecognised state", lambda r: bool(str(r["state"]).strip()) and str(r["state"]) not in STATE_TZ),
        ("blank state/city", lambda r: not str(r["state"]).strip() or not str(r["city"]).strip()),
        ("blank website", lambda r: not str(r["domain"]).strip()),
        ("website is a social/shared page (not the company's own)", lambda r: dom(r["domain"]) in SHARED_HOSTS),
        ("same website repeated in new rows", lambda r: bool(r["domain"]) and newd[dom(r["domain"])] > 1),
        ("website already on an older row", lambda r: bool(r["domain"]) and bool(oldd.get(dom(r["domain"])))),
        ("same name+city as an older row", lambda r: bool(oldn.get((norm(r["company_name"]), str(r["city"]).lower())))),
        ("toll-free number", lambda r: str(r["phone_e164"])[2:5] in ("800", "833", "844", "855", "866", "877", "888")),
        ("odd characters / whitespace in name", lambda r: str(r["company_name"]) != str(r["company_name"]).strip() or "  " in str(r["company_name"]) or not str(r["company_name"]).isascii()),
    ]
    for name, f in checks:
        bad = [(i, r) for i, r in new if f(r)]
        print(f"{name}: {len(bad)}")
        for i, r in bad[:8]:
            print("      ", i, str(r["company_name"])[:34], r["phone_e164"], r["city"], r["state"], str(r["domain"])[:38])
    emailed = [(i, r, o) for i, r in new if r["domain"] and dom(r["domain"]) not in SHARED_HOSTS
               for (_oi, o) in oldd.get(dom(r["domain"]), []) if str(o["email_track"]).strip()]
    print(f"ALREADY EMAILED (website matches a company with a send date): {len(emailed)}")
    for i, r, o in emailed[:10]:
        print("      ", i, r["company_name"][:30], "| same site as", o["company_name"][:30], "email_track", o["email_track"])
    print("timezones:", dict(collections.Counter(str(r["timezone"]).split("/")[-1] for i, r in new)))
    pre = collections.Counter(str(r["phone_e164"])[2:9] for i, r in new)
    print("repeated 7-digit prefixes:", [(p, n) for p, n in pre.items() if n > 1])
    print("franchise-looking:", [(i, r["company_name"]) for i, r in new if FRANCHISE.search(str(r["company_name"]))])
    print("names with no industry word:", [(i, r["company_name"]) for i, r in new if not INDUSTRY.search(str(r["company_name"]))])
    allp = collections.Counter(str(r["phone_e164"]) for i, r in rows)
    print("whole Queue: duplicate numbers:", sum(1 for n in allp.values() if n > 1), "| invalid numbers:", sum(1 for i, r in rows if not CANON.fullmatch(str(r["phone_e164"]))))


if __name__ == "__main__":
    main()

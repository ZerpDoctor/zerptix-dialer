"""One-off backfill for the quarter-turn minimum gap (owner rule 2026-10-06).

The quarter reset used to clear last_call_date, so a company called two weeks before the turn became due again two
weeks later. compute_quarter_reset now sets next_eligible_date = last call + SCHED_QUARTER_TURN_MIN_DAYS (42); this
script does the same for rows that were ALREADY reset: Queue rows with 0 attempts this quarter, no last_call_date and a
prospect call in the Calls sheet less than that many days ago.

    python scripts/backfill_quarter_turn_gap.py            # dry run: counts + a sample
    python scripts/backfill_quarter_turn_gap.py --apply    # writes next_eligible_date (one batchUpdate), saves an undo file

Only ever LATER-s next_eligible_date; touches nothing else.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

sys.path.insert(0, ".")
from app import google_sheets as gs  # noqa: E402
from app.config import CFG  # noqa: E402

UNDO = "tests/golden/quarter_turn_gap_undo.json"


def col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def read(svc, rng):
    for _ in range(6):
        try:
            return svc.get(spreadsheetId=CFG.sheet_id, range=rng, valueRenderOption="UNFORMATTED_VALUE").execute()["values"]
        except Exception:  # noqa: BLE001
            time.sleep(4)
    raise SystemExit("could not read the Sheet")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today, UTC)")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(dt.timezone.utc).date()
    gap = CFG.sched_quarter_turn_min_days
    svc = gs._service().spreadsheets().values()
    calls = read(svc, "Calls!A1:BZ100000")
    ch = calls[0]
    ip, it, ic = ch.index("phone_e164"), ch.index("logged_at_iso"), ch.index("company_name")
    last: dict[str, dt.date] = {}
    for r in calls[1:]:
        if len(r) > max(ip, it, ic) and r[ic] and r[ip]:           # prospect calls only (test calls have no company)
            try:
                when = dt.datetime.fromisoformat(str(r[it]))
            except ValueError:
                continue
            d = (when - dt.timedelta(hours=6)).date()               # evening calls are logged after midnight UTC
            if d > last.get(r[ip], dt.date.min):
                last[r[ip]] = d
    q = read(svc, f"{CFG.sched_queue_tab}!A1:AZ100000")
    qh = q[0]
    c_ne = qh.index("next_eligible_date")
    updates, undo, sample = [], [], []
    for n, r in enumerate(q[1:], start=2):
        d = dict(zip(qh, r + [""] * (len(qh) - len(r))))
        lc = last.get(d["phone_e164"])
        att = d["current_quarter_attempts"]
        att = int(att) if str(att).strip() else 0
        if lc is None or att != 0 or str(d["last_call_date"]).strip():
            continue
        earliest = lc + dt.timedelta(days=gap)
        if earliest <= today:
            continue
        cur = str(d["next_eligible_date"]).strip()
        if cur and cur >= earliest.isoformat():
            continue
        updates.append({"range": f"{CFG.sched_queue_tab}!{col(c_ne)}{n}", "values": [[earliest.isoformat()]]})
        undo.append({"row": n, "phone": d["phone_e164"], "old": cur})
        if len(sample) < 6:
            sample.append((n, d["company_name"][:28], lc.isoformat(), earliest.isoformat()))
    print(f"gap {gap}d, today {today}: {len(updates)} Queue rows would be held")
    for s in sample:
        print("   ", s)
    if a.apply and updates:
        json.dump({"column": col(c_ne), "rows": undo}, open(UNDO, "w"), indent=1)
        svc.batchUpdate(spreadsheetId=CFG.sheet_id, body={"valueInputOption": "RAW", "data": updates}).execute()
        print("applied; undo file:", UNDO)


if __name__ == "__main__":
    main()

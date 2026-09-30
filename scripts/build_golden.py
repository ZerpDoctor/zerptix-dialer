"""Build the golden replay fixtures from the Calls sheet.

    python scripts/build_golden.py            # writes tests/golden/cases.json

A "golden case" is a real call whose correct outcome a person has already
checked: the two 2026-09-29/30 batches (read row by row and corrected) and the
manual franchise / own-cell test calls of 2026-09-30. The expected outcome is
the Sheet's CURRENT outcome for that call (i.e. after the corrections).

Each case carries what _compute_outcome needs to be replayed offline: the final
transcript, AMD verdict, digits pressed, where the post-press audio starts (from
the Call_Checkpoint tab, only when it agrees with the Sheet), the timer flags and
the call duration. Calls that pressed a digit but have no trustworthy press
position are kept for documentation but marked replayable=false.

Re-run this to add newly verified nights; never hand-edit cases.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from app import google_sheets as gs  # noqa: E402
from app.config import CFG  # noqa: E402

OUT = Path("tests/golden/cases.json")
# UTC window: 2026-09-29 evening batch .. end of 2026-09-30 (the second batch and all manual tests)
START, END = "2026-09-29T15:00", "2026-10-01T00:00"
# Franchise numbers dialed manually on 2026-09-30 (fresh lookups, never Queue rows)
FRANCHISE = {
    "+18328565900": "PuroClean of Central Southwest Houston", "+14042334644": "911 Restoration of Northwest Atlanta",
    "+16143240026": "ServiceMaster Restoration by Neverman", "+13039931313": "PuroClean of Central Denver",
    "+16158685324": "SERVPRO of East Nashville", "+17045969700": "SERVPRO of North Central Mecklenburg",
    "+16157314222": "SERVPRO of Southeast Nashville", "+12818867755": "Paul Davis Greater Houston",
    "+14692896376": "Paul Davis North Dallas", "+15123874023": "(wrong number: hotel reservation line)",
    "+15125620411": "Restoration 1 of Austin", "+14699653700": "Rainbow Restoration Far North Dallas",
    "+17709390128": "BELFOR Atlanta", "+17702469943": "Steamatic of Atlanta", "+19722178245": "911 Restoration of Dallas",
    "+16159322400": "PuroClean Nashville", "+16157548536": "ServiceMaster Restore by David",
    "+17043248528": "ServiceMaster DSI Charlotte",
}


def main() -> None:
    svc = gs._service()
    calls = svc.spreadsheets().values().get(
        spreadsheetId=CFG.sheet_id, range=f"{CFG.sheet_tab}!A1:Z100000").execute()["values"]
    h = calls[0]
    cps = svc.spreadsheets().values().get(
        spreadsheetId=CFG.sheet_id, range=f"{CFG.call_checkpoint_tab}!A1:Z100000").execute()["values"]
    ch = cps[0]
    last_cp = {}
    for x in cps[1:]:
        d = dict(zip(ch, x + [""] * (len(ch) - len(x))))
        last_cp[d["call_sid"]] = d

    cases, skipped = [], 0
    for x in calls[1:]:
        r = dict(zip(h, x + [""] * (len(h) - len(x))))
        if not (START <= r["logged_at_iso"] < END):
            continue
        outcome = r["outcome"]
        if outcome in ("busy", "no_answer", "disconnected") and not r["ivr_transcript"].strip():
            skipped += 1                  # telephony-status outcomes: nothing to replay
            continue
        digits = [d for d in r["digits_sent"].split(",") if d]
        cp = last_cp.get(r["call_sid"])
        press_idx, replayable = None, True
        flags = {"hit_time_cap": False, "ivr_detected": bool(digits), "gatekeeping_detected": False,
                 "alt_contact_detected": False}
        if cp:
            flags = {k: str(cp.get(k, "")).lower() == "true" for k in flags}
            if ",".join(digits) == cp.get("digits_sent", ""):
                press_idx = int(cp.get("transcript_at_last_digit") or 0) if digits else 0
        if digits and press_idx is None:
            replayable = False            # pressed a digit, no trustworthy position of the press
        company = r["company_name"] or FRANCHISE.get(r["phone_e164"], "")
        cases.append({
            "id": r["call_sid"][:8], "tier": "reviewed-batch" if r["company_name"] else "franchise-test",
            "company": company, "phone": r["phone_e164"], "answered_by": r["answered_by"],
            "transcript": r["ivr_transcript"], "digits_sent": digits, "press_idx": press_idx,
            "ivr_detected": flags["ivr_detected"] or bool(digits), "hit_time_cap": flags["hit_time_cap"],
            "gatekeeping_detected": flags["gatekeeping_detected"], "alt_contact_detected": flags["alt_contact_detected"],
            "duration_sec": int(r["duration_sec"] or 0), "expected": outcome, "replayable": replayable,
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, indent=1), encoding="utf-8")
    rep = [c for c in cases if c["replayable"]]
    print(f"wrote {OUT}: {len(cases)} cases ({len(rep)} replayable, {len(cases) - len(rep)} kept for documentation), "
          f"{skipped} telephony-only rows skipped")
    by = {}
    for c in rep:
        by[c["expected"]] = by.get(c["expected"], 0) + 1
    print("replayable by expected outcome:", dict(sorted(by.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()

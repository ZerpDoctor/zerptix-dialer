"""Fill duration_sec / twilio_call_status for Calls rows the status callback missed.

    python scripts/reconcile_durations.py [--days 3] [--apply]

For a call WE hang up, the carrier's "completed" callback can land before the row is
written; the backfill then found no row and gave up (fixed in server.webhook_status,
but rows already written stay blank). This asks SignalWire for each such call and fills
just those two cells. Telephony-only outcomes (busy / no_answer / disconnected / failed)
legitimately have no duration and are skipped. Dry run unless --apply.
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
from app import google_sheets as gs  # noqa: E402
from app.config import CFG  # noqa: E402
from app.queue_model import col_letter  # noqa: E402

SKIP_OUTCOMES = {"busy", "no_answer", "disconnected"}


def fetch(sid: str) -> dict:
    url = (f"https://{CFG.signalwire_space_url}/api/laml/2010-04-01/Accounts/"
           f"{CFG.signalwire_project_id}/Calls/{sid}.json")
    auth = base64.b64encode(f"{CFG.signalwire_project_id}:{CFG.signalwire_api_token}".encode()).decode()
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"}), timeout=20) as r:
        return json.loads(r.read().decode())


def main(days: int, apply: bool) -> None:
    svc = gs._service()
    v = svc.spreadsheets().values().get(spreadsheetId=CFG.sheet_id, range=f"{CFG.sheet_tab}!A1:Z100000").execute()["values"]
    h = v[0]
    ix = {n: h.index(n) for n in ("call_sid", "outcome", "duration_sec", "twilio_call_status", "logged_at_iso", "company_name")}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    todo = []
    for n, x in enumerate(v[1:], start=2):
        x = x + [""] * (len(h) - len(x))
        if x[ix["logged_at_iso"]] < cutoff or x[ix["outcome"]] in SKIP_OUTCOMES:
            continue
        if not x[ix["duration_sec"]] or x[ix["twilio_call_status"]].lower() not in ("completed",):
            todo.append((n, x))
    print(f"{len(todo)} rows in the last {days} days with a blank duration or a non-final status")
    data = []
    for n, x in todo:
        sid = x[ix["call_sid"]]
        try:
            c = fetch(sid)
        except Exception as e:  # noqa: BLE001
            print(f"   row {n} {x[ix['company_name']][:28]:28} FETCH FAILED: {e!r}"[:150])
            continue
        dur, st = str(c.get("duration") or ""), str(c.get("status") or "")
        print(f"   row {n} {x[ix['company_name']][:28]:28} {x[ix['twilio_call_status']] or '-':>11} -> {st:10} duration {x[ix['duration_sec']] or '-':>3} -> {dur}")
        if dur and st:
            data.append({"range": f"{CFG.sheet_tab}!{col_letter(ix['duration_sec'])}{n}", "values": [[dur]]})
            data.append({"range": f"{CFG.sheet_tab}!{col_letter(ix['twilio_call_status'])}{n}", "values": [[st]]})
    if apply and data:
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=CFG.sheet_id, body={"valueInputOption": "RAW", "data": data}).execute()
        print(f"wrote {len(data) // 2} rows")
    elif not apply:
        print("(dry run -- pass --apply)")


if __name__ == "__main__":
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 3
    main(days, "--apply" in sys.argv)

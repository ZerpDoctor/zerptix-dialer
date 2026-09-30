"""One-off backfill: fill the email_safe / email_safe_reason columns for every
existing Calls row (new rows get them from server._build_row).

    python scripts/backfill_email_safe.py            # dry run: prints the distribution, writes nothing
    python scripts/backfill_email_safe.py --apply    # writes the two header cells + the two data columns

Only the two new columns are written (one header update + one data update); no
other cell in the sheet is touched. Safe to re-run -- it recomputes from the row.
"""
from __future__ import annotations

import collections
import sys

sys.path.insert(0, ".")
from app import google_sheets as gs  # noqa: E402
from app.config import CFG  # noqa: E402
from app.email_safety import assess  # noqa: E402
from app.queue_model import col_letter  # noqa: E402


def main(apply: bool) -> None:
    svc = gs._service()
    values = svc.spreadsheets().values().get(
        spreadsheetId=CFG.sheet_id, range=f"{CFG.sheet_tab}!A1:Z100000").execute().get("values", [])
    header, rows = values[0], values[1:]
    idx = {name: header.index(name) for name in ("outcome", "digits_sent", "ivr_transcript", "notes")}
    out, dist = [], collections.Counter()
    for r in rows:
        r = r + [""] * (len(header) - len(r))
        verdict, why = assess({k: r[i] for k, i in idx.items()})
        out.append([verdict, why])
        dist[verdict] += 1
    print(f"{len(rows)} rows -> {dict(dist)}")
    by_outcome = collections.defaultdict(collections.Counter)
    for r, (verdict, _w) in zip(rows, out):
        by_outcome[(r + [""] * 3)[idx["outcome"]]][verdict] += 1
    for oc, c in sorted(by_outcome.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"   {oc or '(blank)':18} {dict(c)}")
    if not apply:
        print("\n(dry run -- nothing written; pass --apply)")
        return
    c1, c2 = col_letter(len(gs.HEADER) - 2), col_letter(len(gs.HEADER) - 1)
    svc.spreadsheets().values().update(
        spreadsheetId=CFG.sheet_id, range=f"{CFG.sheet_tab}!{c1}1:{c2}1", valueInputOption="RAW",
        body={"values": [["email_safe", "email_safe_reason"]]}).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=CFG.sheet_id, range=f"{CFG.sheet_tab}!{c1}2:{c2}{len(rows) + 1}", valueInputOption="RAW",
        body={"values": out}).execute()
    print(f"\nwrote {c1}1:{c2}{len(rows) + 1}")


if __name__ == "__main__":
    main("--apply" in sys.argv)

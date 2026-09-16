"""Propose `timezone` values for Queue rows that are missing one.

    python -m app.fill_timezones --dry-run        # print proposals, change nothing
    python -m app.fill_timezones                  # write proposals for blank rows
    python -m app.fill_timezones --tab Queue_SIM

The scheduler NEVER trusts a proposal automatically -- a row is dialed only once
a human has confirmed a valid IANA name in the column. Split-timezone states are
marked "VERIFY". Rows with an unknown state are left blank for you to fill.
"""
from __future__ import annotations

import argparse

from .config import CFG
from .queue_backend import SheetQueue
from .timezones import parse_tz, propose


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tab", default=CFG.sched_queue_tab)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="also re-propose rows that already have a (maybe wrong) timezone")
    args = ap.parse_args()

    q = SheetQueue(args.tab)
    rows = q.read_rows()
    changed = 0
    print(f"{args.tab}: {len(rows)} rows\n")

    for r in rows:
        has_valid = parse_tz(r.timezone) is not None
        if has_valid and not args.overwrite:
            continue
        proposed, note = propose(r.state, r.phone_e164)
        tag = "keep" if not proposed else ("DRY" if args.dry_run else "write")
        print(f"  {r.company_name or '?':28} state={r.state or '--':4} "
              f"current={r.timezone or '(blank)':22} -> {proposed or '(leave blank)':22} [{tag}] {note}")
        if proposed:
            changed += 1
            if not args.dry_run:
                q.update_fields(r.row_number, {"timezone": proposed})

    print(f"\n{'would write' if args.dry_run else 'wrote'} {changed} proposal(s). "
          f"Review every VERIFY row against the company's city before running the scheduler.")


if __name__ == "__main__":
    main()

"""Create / populate the Queue tab.

    python -m app.seed_queue                 # just ensure the tab + header exist
    python -m app.seed_queue --sample        # + append a few TEST_ALLOWLIST companies
    python -m app.seed_queue --tab Queue_SIM --sample --wipe

Real company data you paste into the tab yourself (columns match spec section 2).
"""
from __future__ import annotations

import argparse

from .config import CFG
from .queue_backend import SheetQueue, ensure_queue_tab

# company, phone, timezone, state. The first row uses your TEST_ALLOWLIST
# number so a real `scheduler tick` can actually dial it once Twilio is live;
# the rest use distinct placeholder numbers (each company must have its own).
def _sample_rows() -> list[dict]:
    allow = CFG.test_allowlist[0] if CFG.test_allowlist else "+15555550123"
    spec = [
        ("Eastern Sample Co",  allow,          "America/New_York",    "NY"),
        ("Central Sample Co",  "+15550200002", "America/Chicago",     "IL"),
        ("Mountain Sample Co", "+15550200003", "America/Denver",      "CO"),
        ("Pacific Sample Co",  "+15550200004", "America/Los_Angeles", "CA"),
        ("Arizona Sample Co",  "+15550200005", "America/Phoenix",     "AZ"),
        ("No-Timezone Sample Co", "+15550200006", "",                 "TX"),  # skipped + flagged
    ]
    return [
        {
            "company_name": name, "phone_e164": phone, "state": st, "timezone": tz,
            "source": "sample", "current_quarter_attempts": "0",
            "this_quarter_status": "in_progress",
        }
        for name, phone, tz, st in spec
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tab", default=CFG.sched_queue_tab)
    ap.add_argument("--sample", action="store_true", help="append sample companies")
    ap.add_argument("--wipe", action="store_true", help="clear existing data rows first")
    args = ap.parse_args()

    ensure_queue_tab(args.tab)
    q = SheetQueue(args.tab)
    print(f"Queue tab {args.tab!r} ready ({len(q.read_rows())} data rows).")

    if args.wipe:
        q.clear_data_rows()
        print("  cleared existing data rows")

    if args.sample:
        rows = _sample_rows()
        q.append_rows(rows)
        print(f"  appended {len(rows)} sample companies (phone = {rows[0]['phone_e164']})")


if __name__ == "__main__":
    main()

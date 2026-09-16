"""Accelerated full-cycle simulation of the scheduler (spec section 7).

Runs a virtual clock ~130 days across a calendar-quarter boundary against an
in-memory Queue, with scripted per-company outcomes and NO Twilio/Anthropic.
Prints a per-company timeline, checks the cadence invariants, and (optionally)
flushes the final Queue state to the Queue_SIM tab so you can eyeball it.

    python -m app.sim_quarter                 # run + print + assert
    python -m app.sim_quarter --flush         # also write final state to Queue_SIM
    python -m app.sim_quarter --days 200 --step 15
"""
from __future__ import annotations

import argparse
import itertools
from datetime import datetime, timedelta, timezone

from .config import CFG
from .queue_backend import MemoryQueue
from .queue_model import ANSWERS_TO_COVER, HEADER, MISS_OUTCOMES
from . import queue_writer, scheduler
from .timezones import parse_tz

# company_name, phone, timezone, state, scripted outcomes (then "unknown"), flags
COMPANIES = [
    ("Eastern Co",  "+15550100001", "America/New_York",    "NY", ["unknown", "unknown", "voicemail"], {}),
    # A single "answered" is no longer decisive -- needs 4 consecutive to cover.
    ("Central Co",  "+15550100002", "America/Chicago",     "IL", ["answered", "answered", "answered", "answered"], {}),
    ("Mountain Co", "+15550100003", "America/Denver",      "CO", ["unknown", "unknown", "unknown", "unknown"], {}),
    ("Pacific Co",  "+15550100004", "America/Los_Angeles", "CA", ["no_answer"], {}),
    ("Arizona Co",  "+15550100005", "America/Phoenix",     "AZ", ["busy"], {}),
    ("DNC Co",      "+15550100006", "America/New_York",    "NY", [], {"do_not_call": "true"}),
    ("Closed Co",   "+15550100007", "America/Chicago",     "IL", [], {"replied_or_closed": "true"}),
    ("NoTZ Co",     "+15550100008", "",                    "TX", ["voicemail"], {}),
    # --- consecutive-answered-streak fix regression coverage ---
    ("Four Answers Co", "+15550100009", "America/New_York", "NY", ["answered", "answered", "answered", "answered"], {}),
    ("Answer Then Miss Co", "+15550100010", "America/New_York", "NY", ["answered", "voicemail"], {}),
    # The scripted 4th "answered" is never actually reached: confirmed_miss on
    # attempt 3 stops further dialing, same as any other miss. That's the point.
    ("Answer Answer Miss Co", "+15550100011", "America/New_York", "NY", ["answered", "answered", "no_answer", "answered"], {}),
]


def _seed_rows() -> list[dict]:
    rows = []
    for name, phone, tz, st, _outcomes, flags in COMPANIES:
        d = {h: "" for h in HEADER}
        d.update({
            "company_name": name, "phone_e164": phone, "state": st, "timezone": tz,
            "source": "sim", "current_quarter_attempts": "0",
            "this_quarter_status": "in_progress",
        })
        d.update(flags)
        rows.append(d)
    return rows


def run(days: int, step_minutes: int, flush: bool) -> int:
    mem = MemoryQueue(_seed_rows())
    scripts = {name: iter(outcomes) for name, _p, _tz, _st, outcomes, _f in COMPANIES}
    sid_counter = itertools.count(1)

    def sim_dialer(phone: str, *, window: str, local_date_iso: str, from_number: str | None = None) -> str:
        return "CAsim" + format(next(sid_counter), "028d")

    timeline: list[dict] = []

    start = datetime(2026, 1, 4, 0, 0, tzinfo=timezone.utc)  # a Sunday 00:00 UTC
    steps = int(days * 24 * 60 / step_minutes)
    for i in range(steps):
        now = start + timedelta(minutes=i * step_minutes)
        # record_attempts=True: no server in the sim, so the tick records the
        # attempt itself via queue_writer (single-threaded).
        decisions = scheduler.tick(now, queue=mem, dialer=sim_dialer, record_attempts=True)
        for d in decisions:
            if d.action != "dialed":
                continue
            name = d.row.company_name
            outcome = next(scripts[name], "unknown")
            queue_writer.apply_outcome(mem, d.row.phone_e164, outcome,
                                       call_sid=f"CAsimO{i}", when_utc=now)
            fresh = mem.find_by_phone(d.row.phone_e164)
            timeline.append({
                "company": name,
                "utc": now.isoformat(),
                "local": d.local_now.strftime("%Y-%m-%d %a %H:%M %Z"),
                "window": d.window,
                "attempt": fresh.attempts,
                "quarter": f"{d.local_now.year}Q{(d.local_now.month - 1)//3 + 1}",
                "outcome": outcome,
            })

    _report(mem, timeline)
    failures = _check_invariants(mem, timeline, step_minutes)

    if flush:
        from .queue_backend import SheetQueue, ensure_queue_tab
        ensure_queue_tab(CFG.sched_sim_queue_tab)
        sq = SheetQueue(CFG.sched_sim_queue_tab)
        sq.clear_data_rows()
        sq.append_rows(mem.as_dicts())
        print(f"\nflushed final state to tab {CFG.sched_sim_queue_tab!r}")

    return 1 if failures else 0


def _report(mem: MemoryQueue, timeline: list[dict]) -> None:
    print("\n================ TIMELINE ================")
    for name, *_ in COMPANIES:
        evs = [t for t in timeline if t["company"] == name]
        row = next(r for r in mem.read_rows() if r.company_name == name)
        print(f"\n{name}  tz={row.timezone or '(none)'}  "
              f"status={row.this_quarter_status}  attempts={row.attempts}  "
              f"last_outcome={row.last_outcome or '-'}  dnc={row.is_dnc} closed={row.is_closed}")
        if not evs:
            print("   (never dialed)")
        for e in evs:
            print(f"   {e['local']:24} {e['window']:11} attempt#{e['attempt']} "
                  f"{e['quarter']}  -> {e['outcome']}")


def _check_invariants(mem: MemoryQueue, timeline: list[dict], step_minutes: int) -> list[str]:
    fails: list[str] = []
    from datetime import date
    from .scheduler import active_window

    by_company: dict[str, list[dict]] = {}
    for t in timeline:
        by_company.setdefault(t["company"], []).append(t)

    for name, _phone, tz, st, _o, flags in COMPANIES:
        evs = by_company.get(name, [])

        if flags.get("do_not_call") or flags.get("replied_or_closed"):
            if evs:
                fails.append(f"{name}: dialed {len(evs)}x despite dnc/closed flag")
            continue
        if not tz:
            if evs:
                fails.append(f"{name}: dialed despite missing timezone")
            continue

        # a callable company must actually have been dialed
        if not evs:
            fails.append(f"{name}: valid tz, no skip flags, but never dialed")
            continue

        # group by quarter
        by_q: dict[str, list[dict]] = {}
        for e in evs:
            by_q.setdefault(e["quarter"], []).append(e)

        for q, qevs in by_q.items():
            if len(qevs) > CFG.sched_max_attempts_per_quarter:
                fails.append(f"{name} {q}: {len(qevs)} attempts > cap {CFG.sched_max_attempts_per_quarter}")

            # spacing + alternation
            prev = None
            expected_win = CFG.sched_first_window
            for e in qevs:
                d = date.fromisoformat(e["local"][:10])
                if prev is not None:
                    gap = (d - prev).days
                    if gap < CFG.sched_min_days_between_attempts:
                        fails.append(f"{name} {q}: attempts {gap}d apart (< {CFG.sched_min_days_between_attempts})")
                prev = d
                if e["window"] != expected_win:
                    fails.append(f"{name} {q}: window {e['window']} != expected {expected_win}")
                expected_win = "deep_night" if expected_win == "evening" else "evening"

            # stop-on-miss (always decisive) / stop-on-Nth-consecutive-answered
            # (a single answer is NOT decisive -- only ANSWERS_TO_COVER in a row,
            # with no miss in between, terminates the quarter as covered).
            consecutive_answers = 0
            for idx, e in enumerate(qevs):
                if e["outcome"] in MISS_OUTCOMES:
                    if idx != len(qevs) - 1:
                        fails.append(f"{name} {q}: {len(qevs) - 1 - idx} attempt(s) after a terminal '{e['outcome']}'")
                    break
                if e["outcome"] == "answered":
                    consecutive_answers += 1
                    if consecutive_answers >= ANSWERS_TO_COVER:
                        if idx != len(qevs) - 1:
                            fails.append(f"{name} {q}: {len(qevs) - 1 - idx} attempt(s) after "
                                        f"{ANSWERS_TO_COVER}th consecutive answered")
                        break
                # ivr_unresolved / unknown -> inconclusive, doesn't reset or
                # advance the streak, and isn't terminal either.

        # local-time window / calling-day check
        z = parse_tz(tz)
        for e in evs:
            local_dt = datetime.fromisoformat(e["utc"]).astimezone(z)
            if active_window(local_dt) is None:
                fails.append(f"{name}: dialed at {e['local']} which is outside any calling window/day")

    print("\n================ INVARIANTS ================")
    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
    else:
        print("  all invariants hold: <=4/quarter, >=%dd apart, windows alternate, "
              "stop-on-miss, Sun-Thu local, quarter reset" % CFG.sched_min_days_between_attempts)
    return fails


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=130)
    ap.add_argument("--step", type=int, default=30, help="virtual-clock step in minutes")
    ap.add_argument("--flush", action="store_true", help="write final state to Queue_SIM tab")
    args = ap.parse_args()
    raise SystemExit(run(args.days, args.step, args.flush))


if __name__ == "__main__":
    main()

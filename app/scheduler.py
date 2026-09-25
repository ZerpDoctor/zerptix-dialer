"""Scheduler: read the Queue tab, decide who is due right now, place the call.

Fires at two windows per company-local day -- evening (default 21:30-22:30) and
deep-night (default 02:00-04:00) -- Sun-Thu only, where a "calling day" owns its
evening plus the 2-4am that follows it (so deep-night clock times are Mon-Fri).

Cadence: <=4 attempts / calendar quarter, >=10 days apart, alternating
evening/deep-night, stop the moment a usable miss outcome is logged.

`tick()` is a pure function of (now, queue backend, dialer). Production uses
SheetQueue + an HTTP POST to /calls; the simulation injects MemoryQueue + a
scripted dialer (app/sim_quarter.py).

CLI:
    python -m app.scheduler tick [--now 2026-09-07T22:00:00-04:00] [--dry-run] [--tab NAME]
    python -m app.scheduler run  [--tab NAME]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time as _time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .config import CFG
from .queue_backend import SheetQueue
from .queue_model import (
    COVERED_OUTCOMES,
    HEADER,
    MISS_OUTCOMES,
    STATUS_CONFIRMED_COVERED,
    STATUS_CONFIRMED_MISS,
    STATUS_IN_PROGRESS,
    QueueRow,
    append_csv,
    next_window_for,
)
from .timezones import parse_tz

log = logging.getLogger("dialer.scheduler")

_WD = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# --------------------------------------------------------------------------- #
# nightly volume cap / number-pool distribution
# --------------------------------------------------------------------------- #
# Stopgap for launch, NOT the full number-pool/ramp system (spec section 10) --
# no answer-rate-based ramping, ordering, or health checks, just an even
# round-robin spread with an independent daily budget per number.
#
# Process-lifetime, per-number dial counts, resetting once per UTC calendar
# day. A pool number without an explicit override in CFG.signalwire_nightly_caps
# falls back to SCHED_NIGHTLY_CAP as its default -- so a single-number setup
# (CFG.signalwire_from_numbers has exactly one entry) behaves identically to
# the original single-number cap: one budget, same env var. This generalizes
# that mechanism rather than keeping two parallel cap implementations.
_pool_dial_counts: dict[str, int] = {}
_pool_count_date: date | None = None
_pool_rr_index = 0


def _pool_baseline(today_utc: date) -> dict[str, int]:
    """Working copy of today's per-number counts, resetting the module-level
    dict (and its date) if the day has rolled over."""
    global _pool_dial_counts, _pool_count_date
    if _pool_count_date != today_utc:
        _pool_dial_counts = {}
        _pool_count_date = today_utc
    return dict(_pool_dial_counts)


def _cap_for(number: str) -> int:
    """0 (or unset) means unlimited for that number."""
    return CFG.signalwire_nightly_caps.get(number, CFG.sched_nightly_cap)


def pick_pool_number(pool_counts: dict[str, int], rr_index: int) -> tuple[str | None, int]:
    """Round-robin across CFG.signalwire_from_numbers, skipping any number
    already at its own nightly cap. Returns (chosen_number, advanced_rr_index);
    chosen_number is None if the pool is empty or every number is at cap."""
    pool = CFG.signalwire_from_numbers
    if not pool:
        return None, rr_index
    for _ in range(len(pool)):
        number = pool[rr_index % len(pool)]
        rr_index += 1
        cap = _cap_for(number)
        if not cap or pool_counts.get(number, 0) < cap:
            return number, rr_index
    return None, rr_index


# --------------------------------------------------------------------------- #
# windows / weekdays
# --------------------------------------------------------------------------- #

def _calling_weekdays() -> set[int]:
    return {_WD[d.strip().lower()[:3]] for d in CFG.sched_calling_days.split(",") if d.strip()}


def _parse_window(s: str) -> tuple[time, time]:
    a, b = s.split("-")
    return (
        time(*(int(x) for x in a.split(":"))),
        time(*(int(x) for x in b.split(":"))),
    )


@dataclass
class Window:
    name: str
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # wraps midnight


def _evening() -> Window:
    s, e = _parse_window(CFG.sched_evening_window)
    return Window("evening", s, e)


def _deep_night() -> Window:
    s, e = _parse_window(CFG.sched_deep_night_window)
    return Window("deep_night", s, e)


def active_window(local_dt: datetime) -> Window | None:
    """Which window is active at this company-local datetime, or None."""
    days = _calling_weekdays()
    t = local_dt.time()
    wd = local_dt.weekday()  # Mon=0 .. Sun=6

    ev = _evening()
    if ev.contains(t) and wd in days:
        return ev

    dn = _deep_night()
    if dn.contains(t) and ((wd - 1) % 7) in days:
        # deep-night belongs to the preceding evening's calling day
        return dn
    return None


# --------------------------------------------------------------------------- #
# quarters
# --------------------------------------------------------------------------- #

def _quarter(d: date) -> tuple[int, int]:
    return (d.year, (d.month - 1) // 3 + 1)


def compute_quarter_reset(row: QueueRow, local_now: datetime) -> dict | None:
    """If the row's last attempt was in an earlier calendar quarter, the fields
    that must be reset for the new quarter (else None)."""
    if not row.last_call_date:
        return None
    try:
        last = date.fromisoformat(row.last_call_date)
    except ValueError:
        return None
    if _quarter(last) == _quarter(local_now.date()):
        return None
    return {
        "current_quarter_attempts": 0,
        "last_call_window": "",
        "this_quarter_status": STATUS_IN_PROGRESS,
        "next_eligible_date": "",
        "miss_timestamp": "",
        "consecutive_answered_count": 0,
        "last_updated_at": local_now.astimezone(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# eligibility
# --------------------------------------------------------------------------- #

@dataclass
class Decision:
    row: QueueRow
    action: str  # dial | would-dial | dialed | skip | error | reset
    reason: str
    local_now: datetime | None = None
    window: str | None = None


def evaluate(row: QueueRow, now_utc: datetime) -> Decision:
    tz = parse_tz(row.timezone)
    if tz is None:
        return Decision(row, "skip", f"FLAG: timezone missing/invalid ({row.timezone!r})")

    local = now_utc.astimezone(tz)
    today = local.date().isoformat()

    if row.is_dnc:
        return Decision(row, "skip", "do_not_call", local)
    if row.is_closed:
        return Decision(row, "skip", "replied_or_closed", local)
    if row.this_quarter_status == STATUS_CONFIRMED_MISS:
        return Decision(row, "skip", "confirmed_miss this quarter", local)
    if row.this_quarter_status == STATUS_CONFIRMED_COVERED:
        return Decision(row, "skip", "confirmed_covered this quarter", local)
    if row.attempts >= CFG.sched_max_attempts_per_quarter:
        return Decision(row, "skip",
                        f"{row.attempts}/{CFG.sched_max_attempts_per_quarter} attempts this quarter", local)

    win = active_window(local)
    if win is None:
        return Decision(row, "skip", "outside calling windows / not a calling day", local)
    nxt = next_window_for(row)
    if win.name != nxt:
        return Decision(row, "skip", f"in {win.name} window, next attempt is {nxt}", local, win.name)
    if row.last_call_date == today:
        return Decision(row, "skip", "already attempted today", local, win.name)
    if row.next_eligible_date and today < row.next_eligible_date:
        return Decision(row, "skip", f"not eligible until {row.next_eligible_date}", local, win.name)

    return Decision(row, "dial", f"eligible ({win.name})", local, win.name)


# --------------------------------------------------------------------------- #
# dialer
# --------------------------------------------------------------------------- #

class DialError(RuntimeError):
    pass


class Skipped(RuntimeError):
    """The server declined to dial (in-flight, or already attempted today)."""


def http_dialer(phone: str, *, window: str, local_date_iso: str, from_number: str | None = None) -> str:
    """Place a call via the running server's /calls endpoint, telling it to
    record the attempt on the company's Queue row under its write lock.
    Returns the CallSid, or raises Skipped / DialError."""
    # NOT localhost: on Railway, worker and dialer-web are separate containers,
    # so 127.0.0.1 inside worker refers to itself, not dialer-web. PUBLIC_BASE_URL
    # is already required/set on both services (it's how webhooks get built too),
    # so route through it instead of assuming same-machine deployment.
    url = CFG.callback_url("calls")
    payload = {
        "to_number": phone,
        "record_attempt": True,
        "window": window,
        "local_date": local_date_iso,
    }
    if from_number:
        payload["from_number"] = from_number
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["call_sid"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            parsed = json.loads(body)
            msg = parsed.get("error") or parsed.get("skipped") or body
        except Exception:  # noqa: BLE001
            msg = body
        if e.code == 409:
            raise Skipped(msg) from e
        raise DialError(f"HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise DialError(f"cannot reach dialer server at {url}: {e}") from e


# --------------------------------------------------------------------------- #
# bounded-concurrency dial pool
# --------------------------------------------------------------------------- #

_POOL_POLL_SECONDS = 3
_POOL_MAX_WAIT_SECONDS = 150  # backstop only; every real call already
# resolves within ivr_master_timeout_seconds (~60s) + a little webhook
# latency, so this should essentially never actually bind in practice.


def _inflight_count() -> int:
    """Global in-flight count from dialer-web's own CallStore -- the real
    source of truth, not the Sheet (already hit its read-quota limit more
    than once tonight). Any failure (network hiccup, dialer-web mid-
    restart) is treated as 0 rather than blocking the pool forever on
    something we can't currently observe -- worst case a slot opens a
    little early, not a stuck pool."""
    url = CFG.callback_url("calls/inflight_count")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return int(json.loads(r.read()).get("count", 0))
    except Exception as e:  # noqa: BLE001
        log.warning("inflight-count check failed (%s); treating as 0", e)
        return 0


def pooled_http_dialer(phone: str, *, window: str, local_date_iso: str, from_number: str | None = None) -> str:
    """Bounded-concurrency wrapper around http_dialer (2026-09-21), used as
    the real dialer for both CLI commands below -- waits for a free slot
    (fewer than CFG.sched_max_concurrent_calls calls still in-flight,
    globally) before placing the next real call, instead of a blind fixed
    delay. Calibrated live against real franchise numbers: N=3/5/8
    concurrent all resolved with zero 'unknown' outcomes; an unpaced
    21-at-once burst produced 81% unknown (blank AMD, zero transcript) --
    confirming a real capacity ceiling somewhere between 8 and 21
    concurrent calls, not a gradual slope.

    Queries dialer-web's global in-flight count directly (via
    _inflight_count) rather than tracking a local list of this process's
    own call_sids -- a real incident the same night showed 10 calls truly
    concurrent despite the cap, traced to exactly that: a worker process
    restart reset a local list to empty, so the new process assumed 0 in
    flight and dialed 5 more while the previous process's 5 were still
    resolving. Asking dialer-web for the real current count instead is
    immune to that.

    Deliberately NOT used by sim_quarter.py's injected dialer, which stays
    instant."""
    deadline = _time.time() + _POOL_MAX_WAIT_SECONDS
    while _time.time() < deadline:
        if _inflight_count() < CFG.sched_max_concurrent_calls:
            break
        _time.sleep(_POOL_POLL_SECONDS)
    else:
        log.warning("pooled dialer: no free slot after %ds, dialing anyway", _POOL_MAX_WAIT_SECONDS)

    return http_dialer(phone, window=window, local_date_iso=local_date_iso, from_number=from_number)


# --------------------------------------------------------------------------- #
# tick
# --------------------------------------------------------------------------- #

def tick(now_utc: datetime | None = None, *, queue=None, dialer=None,
         dry_run: bool = False, record_attempts: bool = False) -> list[Decision]:
    """One scheduling pass.

    Production: `dialer` is http_dialer, which POSTs /calls and the SERVER
    records the attempt on the Queue row under its write lock -- so pass
    record_attempts=False (the default) and the scheduler itself never writes
    attempt fields.

    Simulation: there is no server, so pass record_attempts=True and the tick
    records the attempt directly via queue_writer (single-threaded, lock is free).

    The only Queue write the scheduler ever does is the calendar-quarter reset,
    and only for a row whose last attempt was in a prior quarter -- such a row
    cannot have a call in flight, so it cannot race the server.
    """
    from . import queue_writer

    global _pool_dial_counts, _pool_rr_index

    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    queue = queue if queue is not None else SheetQueue(CFG.sched_queue_tab)
    dialer = dialer or http_dialer
    out: list[Decision] = []

    # Local working copies: seeded from the persisted pool state, but only
    # written back at the end if this isn't a dry run -- so previewing never
    # spends the real budget, while still showing an accurate "who's next" cutoff.
    pool_counts = _pool_baseline(now_utc.date())
    rr_index = _pool_rr_index

    for row in queue.read_rows():
        tz = parse_tz(row.timezone)
        if tz is not None:
            reset = compute_quarter_reset(row, now_utc.astimezone(tz))
            if reset:
                if not dry_run:
                    queue.update_fields(row.row_number, reset)
                for k, v in reset.items():
                    row.set(k, v)
                out.append(Decision(row, "reset", "new calendar quarter"))

        d = evaluate(row, now_utc)
        if d.action != "dial":
            out.append(d)
            continue

        chosen_number, rr_index = pick_pool_number(pool_counts, rr_index)
        if chosen_number is None:
            reason = ("no SIGNALWIRE_FROM_NUMBERS configured" if not CFG.signalwire_from_numbers
                      else "all pool numbers at nightly cap")
            out.append(Decision(row, "skip", reason, d.local_now, d.window))
            continue

        if dry_run:
            out.append(Decision(row, "would-dial", f"{d.reason}, from={chosen_number}",
                                d.local_now, d.window))
            pool_counts[chosen_number] = pool_counts.get(chosen_number, 0) + 1
            continue

        local_date = d.local_now.date()
        try:
            call_sid = dialer(row.phone_e164, window=d.window, local_date_iso=local_date.isoformat(),
                              from_number=chosen_number)
        except Skipped as e:
            out.append(Decision(row, "skip", f"server skipped ({e})", d.local_now, d.window))
            continue
        except DialError as e:
            log.error("DIAL ERROR for %s (%s) from %s: %s -- not counted as an attempt",
                      row.company_name, row.phone_e164, chosen_number, e)
            out.append(Decision(row, "error", str(e), d.local_now, d.window))
            continue

        note = f"{d.window} sid={call_sid} from={chosen_number}"
        if record_attempts:
            fields = queue_writer.record_attempt(
                queue, row.phone_e164, call_sid=call_sid, window=d.window,
                local_date=local_date, now_utc=now_utc,
            )
            if fields is None:
                out.append(Decision(row, "skip", "already attempted today (race)", d.local_now, d.window))
                continue
            note = f"{d.window} attempt #{fields['current_quarter_attempts']} sid={call_sid} from={chosen_number}"

        pool_counts[chosen_number] = pool_counts.get(chosen_number, 0) + 1
        out.append(Decision(row, "dialed", note, d.local_now, d.window))

    if not dry_run:
        _pool_dial_counts = pool_counts
        _pool_rr_index = rr_index

    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _print(decisions: list[Decision]) -> None:
    for d in decisions:
        if d.action in ("dialed", "would-dial", "error", "reset"):
            log.info("  [%s] %s (%s) -- %s", d.action, d.row.company_name or "?",
                     d.row.phone_e164, d.reason)
    cap_skips = [d for d in decisions if d.action == "skip" and d.reason == "all pool numbers at nightly cap"]
    if cap_skips:
        names = ", ".join(d.row.company_name or d.row.phone_e164 for d in cap_skips)
        log.info("  [cap] all pool numbers at nightly cap -- %d eligible compan(y/ies) skipped this tick: %s",
                 len(cap_skips), names)
    if CFG.signalwire_from_numbers:
        usage = ", ".join(
            f"{n}={_pool_dial_counts.get(n, 0)}/{_cap_for(n) or '∞'}"
            for n in CFG.signalwire_from_numbers
        )
        log.info("  pool usage today: %s", usage)
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.action] = counts.get(d.action, 0) + 1
    log.info("tick summary: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing")


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Zerptix dialer scheduler")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick", help="evaluate once and dial who is due")
    t.add_argument("--now", help="ISO 8601 instant WITH offset, e.g. 2026-09-07T22:00:00-04:00")
    t.add_argument("--dry-run", action="store_true", help="print who would be dialed, don't dial")
    t.add_argument("--tab", default=None)
    r = sub.add_parser("run", help="loop: tick every SCHED_TICK_SECONDS")
    r.add_argument("--tab", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "tick":
        now = None
        if args.now:
            now = datetime.fromisoformat(args.now)
            if now.tzinfo is None:
                print("--now needs a UTC offset (e.g. ...-04:00 or ...+00:00)")
                sys.exit(2)
        q = SheetQueue(args.tab or CFG.sched_queue_tab)
        _print(tick(now, queue=q, dialer=pooled_http_dialer, dry_run=args.dry_run))
    elif args.cmd == "run":
        q = SheetQueue(args.tab or CFG.sched_queue_tab)
        log.info("scheduler loop: tick every %ds against tab %r, max %d concurrent calls",
                  CFG.sched_tick_seconds, q.tab, CFG.sched_max_concurrent_calls)
        while True:
            try:
                _print(tick(queue=q, dialer=pooled_http_dialer))
            except Exception as e:  # noqa: BLE001
                log.exception("tick failed: %s", e)
            _time.sleep(CFG.sched_tick_seconds)


if __name__ == "__main__":
    _cli()

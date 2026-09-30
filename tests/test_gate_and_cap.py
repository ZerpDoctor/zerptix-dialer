"""Tests for the restart-proof nightly cap, the pre-dial gate, the error
circuit breaker and timezone fairness. Everything is injected: an in-memory
queue, a fake dialer, a fake Calls-sheet reader, a fake clock. No network.
"""
from __future__ import annotations

import itertools
import sys
import types
import unittest
from datetime import datetime, timezone

try:  # pragma: no cover
    import flask_sock  # noqa: F401
except Exception:  # pragma: no cover
    _m = types.ModuleType("flask_sock")

    class _S:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k): return lambda f: f
    _m.Sock = _S
    sys.modules["flask_sock"] = _m

from app import health, scheduler  # noqa: E402
from app.config import CFG  # noqa: E402
from app.queue_backend import MemoryQueue  # noqa: E402
from app.queue_model import HEADER  # noqa: E402

NUMBERS = ["+15550000001", "+15550000002", "+15550000003", "+15550000004"]
CAP_EACH = 25
# 9:35pm Tuesday Sep 29 in the Eastern / Central / Mountain / Pacific windows
ET, CT, MT, PT = (datetime(2026, 9, 30, h, 35, tzinfo=timezone.utc) for h in (1, 2, 3, 4))
TODAY = "2026-09-30"


def make_rows(tz: str, n: int, prefix: str) -> list[dict]:
    rows = []
    for i in range(n):
        d = {h: "" for h in HEADER}
        d.update({"company_name": f"{prefix} {i}", "phone_e164": f"+1555{abs(hash((prefix, i))) % 10**7:07d}",
                  "timezone": tz, "state": "XX", "current_quarter_attempts": "0",
                  "this_quarter_status": "in_progress"})
        rows.append(d)
    return rows


class FakeDialer:
    def __init__(self):
        self.calls = []
        self._n = itertools.count(1)

    def __call__(self, phone, *, window, local_date_iso, from_number=None):
        sid = f"CAfake{next(self._n):05d}"
        self.calls.append((phone, from_number, sid))
        return sid


def sheet_rows(per_number: dict[str, int], sid_prefix="CAold") -> list[dict]:
    out, i = [], 0
    for number, n in per_number.items():
        for _ in range(n):
            i += 1
            out.append({"row": i + 1, "call_sid": f"{sid_prefix}{i:05d}", "from_number": number,
                        "logged_at_iso": f"{TODAY}T01:{i % 60:02d}:00+00:00"})
    return out


def healthy_gate(**kw):
    return health.DialGate(checks_fn=lambda: [health.Check("all", True)], **kw)


class CapTestCase(unittest.TestCase):
    def setUp(self):
        self._cfg = (CFG.signalwire_from_numbers, CFG.signalwire_nightly_caps, CFG.sched_tz_fairness, CFG.gate_enabled)
        CFG.signalwire_from_numbers = list(NUMBERS)
        CFG.signalwire_nightly_caps = {n: CAP_EACH for n in NUMBERS}
        CFG.sched_tz_fairness = False
        CFG.gate_enabled = True
        scheduler._dialed_sids.clear()
        scheduler._pool_dial_counts = {}
        scheduler._pool_count_date = None
        scheduler._pool_rr_index = 0
        self.dialer = FakeDialer()

    def tearDown(self):
        (CFG.signalwire_from_numbers, CFG.signalwire_nightly_caps, CFG.sched_tz_fairness, CFG.gate_enabled) = self._cfg
        scheduler._dialed_sids.clear()

    def tick(self, queue, now, activity=None, gate=None):
        act = activity if callable(activity) else (lambda day: list(activity or []))
        return scheduler.tick(now, queue=queue, dialer=self.dialer, activity_fn=act,
                              gate=gate if gate is not None else healthy_gate())


class RestartProofCap(CapTestCase):
    def test_a_fresh_process_still_respects_calls_already_in_the_sheet(self):
        """2026-09-29: a redeploy reset the in-memory count and a second full
        cap was dialed. With 100 calls already in the Calls sheet a brand-new
        process must dial nothing."""
        q = MemoryQueue(make_rows("America/New_York", 60, "NY"))
        decisions = self.tick(q, ET, sheet_rows({n: CAP_EACH for n in NUMBERS}))
        self.assertEqual(self.dialer.calls, [])
        self.assertTrue(any(d.reason == "all pool numbers at nightly cap" for d in decisions))

    def test_only_the_remaining_headroom_is_dialed(self):
        q = MemoryQueue(make_rows("America/New_York", 60, "NY"))
        self.tick(q, ET, sheet_rows({NUMBERS[0]: 25, NUMBERS[1]: 25, NUMBERS[2]: 20, NUMBERS[3]: 20}))
        self.assertEqual(len(self.dialer.calls), 10)    # 5 + 5 left on numbers 3 and 4
        self.assertTrue(all(c[1] in NUMBERS[2:] for c in self.dialer.calls))

    def test_dials_not_yet_in_the_sheet_still_count_and_are_never_double_counted(self):
        q = MemoryQueue(make_rows("America/New_York", 150, "NY"))
        self.tick(q, ET, [])                             # dials the full 100
        self.assertEqual(len(self.dialer.calls), 100)
        self.dialer.calls.clear()
        self.tick(q, ET, [])                             # none logged yet -> still capped
        self.assertEqual(self.dialer.calls, [])
        # 60 of those calls have now been logged; the other 40 are still pending
        first_sids = list(scheduler._dialed_sids)[:60]
        logged = [{"row": i + 2, "call_sid": sid, "from_number": scheduler._dialed_sids[sid][0],
                   "logged_at_iso": f"{TODAY}T01:40:{i % 60:02d}+00:00"} for i, sid in enumerate(first_sids)]
        self.tick(q, ET, logged)
        self.assertEqual(self.dialer.calls, [], "60 logged + 40 pending must still equal the cap of 100, not 60")

    def test_an_unreadable_sheet_means_no_dialing(self):
        q = MemoryQueue(make_rows("America/New_York", 10, "NY"))

        def broken(day):
            raise RuntimeError("Sheets API unavailable")
        with self.assertRaises(RuntimeError):
            self.tick(q, ET, broken)
        self.assertEqual(self.dialer.calls, [], "an unverifiable count is not a zero")

    def test_yesterdays_pending_dials_do_not_count_today(self):
        scheduler._dialed_sids["CAyesterday"] = (NUMBERS[0], datetime(2026, 9, 29).date())
        q = MemoryQueue(make_rows("America/New_York", 200, "NY"))
        self.tick(q, ET, [])
        self.assertEqual(len(self.dialer.calls), 100)


class Gate(CapTestCase):
    def test_a_failed_health_check_blocks_dialing_and_alerts_once(self):
        sent = []
        orig = health.alerts.send
        health.alerts.send = lambda title, detail="", **k: sent.append(title)
        try:
            now = [1000.0]
            failing = [True]
            gate = health.DialGate(
                checks_fn=lambda: [health.Check("anthropic", not failing[0], "credit balance is too low")],
                clock=lambda: now[0])
            q = MemoryQueue(make_rows("America/New_York", 20, "NY"))
            d = self.tick(q, ET, [], gate)
            self.assertEqual(self.dialer.calls, [])
            self.assertTrue(any(x.reason.startswith("GATE:") and "anthropic" in x.reason for x in d))
            self.tick(q, ET, [], gate)                    # still failing: no second alert
            self.assertEqual(sent.count("Dialing STOPPED: health check failed"), 1)
            failing[0] = False
            now[0] += CFG.gate_recheck_fail_seconds + 1   # next re-check finds it healthy
            self.tick(q, ET, [], gate)
            self.assertEqual(len(self.dialer.calls), 20)
            self.assertIn("Dialing RESUMED: health checks pass again", sent)
        finally:
            health.alerts.send = orig

    def test_a_healthy_gate_is_not_rechecked_every_tick(self):
        n = [0]
        now = [1000.0]

        def checks():
            n[0] += 1
            return [health.Check("all", True)]
        gate = health.DialGate(checks_fn=checks, clock=lambda: now[0])
        for _ in range(5):
            self.assertTrue(gate.allow([])[0])
        self.assertEqual(n[0], 1)
        now[0] += CFG.gate_recheck_ok_seconds + 1
        gate.allow([])
        self.assertEqual(n[0], 2)

    def test_dry_run_is_never_gated(self):
        gate = health.DialGate(checks_fn=lambda: [health.Check("x", False, "down")])
        q = MemoryQueue(make_rows("America/New_York", 3, "NY"))
        d = scheduler.tick(ET, queue=q, dialer=self.dialer, dry_run=True, activity_fn=lambda day: [], gate=gate)
        self.assertEqual(sum(1 for x in d if x.action == "would-dial"), 3)


class CircuitBreaker(CapTestCase):
    ERR = ("tail: anthropic unavailable (BadRequestError (HTTP 400): Your credit balance is too low); "
           "no keyword match, trusting AMD")

    def rows_and_notes(self, n_err, n_ok, iso_prefix):
        rows, notes = [], {}
        for i in range(n_err + n_ok):
            rows.append({"row": i + 2, "call_sid": f"CAb{i}", "from_number": NUMBERS[0],
                         "logged_at_iso": f"{iso_prefix}:{i:02d}+00:00"})
            notes[i + 2] = self.ERR if i < n_err else "tail: clear voicemail greeting"
        return rows, notes

    def test_repeated_dependency_errors_stop_dialing_even_though_the_live_checks_pass(self):
        sent = []
        orig = health.alerts.send
        health.alerts.send = lambda title, detail="", **k: sent.append(title)
        try:
            rows, notes = self.rows_and_notes(4, 2, "2026-09-30T01:40")
            t0 = datetime(2026, 9, 30, 1, 45, tzinfo=timezone.utc).timestamp()
            now = [t0]
            gate = health.DialGate(checks_fn=lambda: [health.Check("all", True)],
                                   notes_fn=lambda r: {x: notes[x] for x in r}, clock=lambda: now[0])
            self.assertFalse(gate.allow(rows)[0])
            self.assertIn("Dialing STOPPED: error circuit breaker tripped", sent)
            now[0] += 60
            self.assertFalse(gate.allow(rows)[0], "must stay open during the cooldown")
            now[0] += health.BREAKER_COOLDOWN_SECONDS
            self.assertTrue(gate.allow(rows)[0], "after the cooldown and passing live checks it resumes; "
                                                 "the old errors must not re-trip it")
        finally:
            health.alerts.send = orig

    def test_healthy_traffic_does_not_trip_it(self):
        rows, notes = self.rows_and_notes(1, 7, "2026-09-30T01:40")
        now = datetime(2026, 9, 30, 1, 45, tzinfo=timezone.utc).timestamp()
        gate = health.DialGate(checks_fn=lambda: [health.Check("all", True)],
                               notes_fn=lambda r: {x: notes[x] for x in r}, clock=lambda: now)
        self.assertTrue(gate.allow(rows)[0])


class TimezoneFairness(CapTestCase):
    def setUp(self):
        super().setUp()
        self.rows = (make_rows("America/New_York", 150, "NY") + make_rows("America/Chicago", 150, "CT")
                     + make_rows("America/Denver", 150, "MT") + make_rows("America/Los_Angeles", 150, "PT"))

    def per_zone(self):
        zone = {r["phone_e164"]: r["timezone"].split("/")[-1] for r in self.rows}
        out = {}
        for phone, _n, _s in self.dialer.calls:
            out[zone[phone]] = out.get(zone[phone], 0) + 1
        return out

    def run_night(self):
        q = MemoryQueue([dict(r) for r in self.rows])
        logged: list[dict] = []
        for now in (ET, CT, MT, PT):
            self.tick(q, now, lambda day: list(logged))
            # every dial so far has resolved and been logged
            logged[:] = [{"row": i + 2, "call_sid": sid, "from_number": num, "logged_at_iso": f"{TODAY}T01:00:00+00:00"}
                         for i, (_p, num, sid) in enumerate(self.dialer.calls)]
        return self.per_zone()

    def test_without_fairness_the_first_window_uses_the_whole_cap(self):
        self.assertEqual(self.run_night(), {"New_York": 100})

    def test_with_fairness_every_timezone_gets_a_share(self):
        CFG.sched_tz_fairness = True
        self.assertEqual(self.run_night(), {"New_York": 25, "Chicago": 25, "Denver": 25, "Los_Angeles": 25})

    def test_with_fairness_an_earlier_zones_unused_share_carries_forward(self):
        CFG.sched_tz_fairness = True
        self.rows = (make_rows("America/New_York", 5, "NY") + make_rows("America/Chicago", 150, "CT")
                     + make_rows("America/Denver", 150, "MT") + make_rows("America/Los_Angeles", 150, "PT"))
        got = self.run_night()
        self.assertEqual(got["New_York"], 5)
        self.assertEqual(sum(got.values()), 100, "the 20 unused Eastern slots must go to later zones")

    def test_a_handful_of_minor_zone_rows_do_not_claim_a_share(self):
        """The real Queue has ~14 Alaska/Hawaii rows; as buckets they would each
        reserve a sixth of the cap and never use it."""
        CFG.sched_tz_fairness = True
        self.rows = (make_rows("America/New_York", 150, "NY") + make_rows("America/Chicago", 150, "CT")
                     + make_rows("America/Denver", 150, "MT") + make_rows("America/Los_Angeles", 150, "PT")
                     + make_rows("Pacific/Honolulu", 6, "HI") + make_rows("America/Anchorage", 8, "AK"))
        got = self.run_night()
        self.assertEqual(got.get("New_York"), 25, "Eastern's share is a quarter, not a sixth")


if __name__ == "__main__":
    unittest.main()

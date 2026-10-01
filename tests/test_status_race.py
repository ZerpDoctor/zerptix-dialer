"""The terminal status callback can land BEFORE the Calls row exists (we hang up, the
carrier's "completed" arrives within a second, the row takes 1-3s to write). The
backfill then found nothing and gave up, leaving duration blank and the status stuck
at "answered" (Houzpital, Freedom Services, Icon Property Rescue, National Fire &
Water on 2026-10-01; Restoration Logistics and Henderson earlier)."""
from __future__ import annotations

import sys
import types
import unittest

try:  # pragma: no cover
    import flask_sock  # noqa: F401
except Exception:  # pragma: no cover
    _m = types.ModuleType("flask_sock")

    class _S:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k): return lambda f: f
    _m.Sock = _S
    sys.modules["flask_sock"] = _m

from app import google_sheets, server  # noqa: E402


class StatusRace(unittest.TestCase):
    def setUp(self):
        self._orig = (server._verify, google_sheets.backfill_call_status, server.time.sleep)
        server._verify = lambda req: True
        server.time.sleep = lambda s: None
        self.client = server.app.test_client()

    def tearDown(self):
        server._verify, google_sheets.backfill_call_status, server.time.sleep = self._orig

    def test_the_backfill_retries_until_the_row_exists(self):
        sid = "CArace00001"
        server.STORE.register(sid, "+15555550123")
        server.STORE.mark_logged(sid)                     # _resolve() has claimed the call; the row is not written yet
        calls = []

        def fake(call_sid, status, duration):
            calls.append((call_sid, status, duration))
            return len(calls) >= 3                        # the row appears on the 3rd attempt
        google_sheets.backfill_call_status = fake
        self.client.post("/webhooks/status", data={"CallSid": sid, "CallStatus": "completed", "CallDuration": "25"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1], (sid, "completed", "25"))

    def test_a_status_that_arrived_before_the_row_is_built_is_used(self):
        sid = "CArace00002"
        server.STORE.register(sid, "+15555550124", company_name="Race Test Co")
        server.STORE.update(sid, call_status="completed", call_duration="31")
        rec = server.STORE.snapshot(sid)
        with server.app.test_request_context("/x", method="POST", data={"To": "+15555550124"}):
            row = server._build_row(rec, "answered", "")
        self.assertEqual(row["duration_sec"], "31")
        self.assertEqual(row["twilio_call_status"], "completed")


if __name__ == "__main__":
    unittest.main()

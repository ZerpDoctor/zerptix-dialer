"""A number nothing can answer is never dialed or counted. 122 clay_import_2026 Queue rows carry
US numbers whose area code starts with 0/1 (+11790442279, +10999999999); 64 were logged as misses."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app import scheduler
from app.queue_model import HEADER, QueueRow

ET_EVENING = datetime(2026, 9, 30, 1, 35, tzinfo=timezone.utc)      # 9:35pm Eastern


def row(phone: str) -> QueueRow:
    d = {h: "" for h in HEADER}
    d.update({"company_name": "Guard Co", "phone_e164": phone, "timezone": "America/New_York", "state": "VA",
              "current_quarter_attempts": "0", "this_quarter_status": "in_progress"})
    return QueueRow(2, [d[h] for h in HEADER])


class InvalidNumbers(unittest.TestCase):
    def test_impossible_area_codes_are_skipped(self):
        for p in ("+11790442279", "+10999999999", "+11000314020", "+11516903897"):
            d = scheduler.evaluate(row(p), ET_EVENING)
            self.assertEqual(d.action, "skip", p)
            self.assertIn("not a valid US number", d.reason)

    def test_a_real_number_is_not_skipped_for_that_reason(self):
        d = scheduler.evaluate(row("+12029228118"), ET_EVENING)
        self.assertNotIn("not a valid US number", d.reason)
        self.assertEqual(d.action, "dial")


if __name__ == "__main__":
    unittest.main()


class EmailHold(unittest.TestCase):
    """Owner's rule 2026-10-03: 90 days from the last email send is the next available send / call."""

    def held(self, value, today, days=90):
        from datetime import date
        return scheduler.emailed_this_quarter(value, date.fromisoformat(today), hold_days=days)

    def test_ninety_days_from_the_send(self):
        self.assertTrue(self.held("2026-09-26", "2026-10-03"))        # 7 days ago, across the quarter boundary
        self.assertTrue(self.held("2026-09-26", "2026-12-24"))        # day 89
        self.assertFalse(self.held("2026-09-26", "2026-12-25"))       # day 90: available again

    def test_timestamps_and_blanks(self):
        self.assertTrue(self.held("2026-09-30T14:00:00+00:00", "2026-10-03"))
        self.assertFalse(self.held("", "2026-10-03"))
        self.assertFalse(self.held("   ", "2026-10-03"))

    def test_a_future_date_or_free_text_still_holds(self):
        self.assertTrue(self.held("2027-01-01", "2026-10-03"))         # typo: safe direction
        self.assertTrue(self.held("sent", "2026-10-03"))

    def test_a_quarter_tag_means_that_quarter_only(self):
        self.assertFalse(self.held("2026-Q3", "2026-10-03"))
        self.assertTrue(self.held("2026-Q4", "2026-10-03"))

    def test_the_scheduler_skips_a_company_emailed_within_90_days(self):
        r = row("+12029228118")
        r.set("email_track", "2026-09-26")
        d = scheduler.evaluate(r, ET_EVENING)
        self.assertEqual(d.action, "skip")
        self.assertIn("emailed within 90 days", d.reason)
        r.set("email_track", "2026-06-01")
        self.assertEqual(scheduler.evaluate(r, ET_EVENING).action, "dial")


class QuarterResetOnce(unittest.TestCase):
    """2026-10-02..05: last_call_date stayed in last quarter, so EVERY 60s tick matched the same ~2,100 rows
    again and rewrote each one -- thousands of Sheet writes a minute vs a 60/min quota; ticks failed with 429."""

    def rows(self, n=5):
        out = []
        for i in range(n):
            d = {h: "" for h in HEADER}
            d.update({"company_name": f"Q3 Co {i}", "phone_e164": f"+1202555{1000 + i}", "timezone": "America/New_York", "state": "VA",
                      "current_quarter_attempts": "2", "this_quarter_status": "confirmed_miss", "last_call_date": "2026-09-24",
                      "last_outcome": "voicemail", "miss_timestamp": "2026-09-24T01:00:00+00:00", "next_eligible_date": "2026-10-04"})
            out.append(d)
        return out

    def test_a_reset_clears_last_call_date_so_it_cannot_match_again(self):
        from datetime import datetime
        from app.queue_model import row_from_dict
        r = row_from_dict(self.rows(1)[0], row_number=2)
        local = datetime(2026, 10, 5, 21, 35, tzinfo=timezone.utc)
        first = scheduler.compute_quarter_reset(r, local)
        self.assertEqual((first["last_call_date"], first["this_quarter_status"], first["current_quarter_attempts"]), ("", "in_progress", 0))
        for k, v in first.items():
            r.set(k, v)
        self.assertIsNone(scheduler.compute_quarter_reset(r, local))          # idempotent

    def test_a_tick_writes_all_resets_in_one_bulk_call_and_the_next_tick_writes_none(self):
        from app.queue_backend import MemoryQueue
        q = MemoryQueue(self.rows(5))
        calls = []
        orig_many, orig_one = q.update_many, q.update_fields
        q.update_many = lambda ups: (calls.append(("many", len(ups))), orig_many(ups))[1]
        q.update_fields = lambda n, f: (calls.append(("one", n)), orig_one(n, f))[1]
        now = datetime(2026, 10, 5, 1, 35, tzinfo=timezone.utc)
        out1 = scheduler.tick(now, queue=q, dialer=lambda *a, **k: "CAx", dry_run=False, activity_fn=lambda day: [], gate=_OpenGate())
        self.assertEqual([c for c in calls if c[0] == "many"], [("many", 5)])
        self.assertEqual(sum(1 for d in out1 if d.action == "reset"), 5)
        calls.clear()
        out2 = scheduler.tick(now, queue=q, dialer=lambda *a, **k: "CAx", dry_run=False, activity_fn=lambda day: [], gate=_OpenGate())
        self.assertEqual(sum(1 for d in out2 if d.action == "reset"), 0)
        self.assertEqual([c for c in calls if c[0] == "many"], [])


class QuarterTurnMinimumGap(unittest.TestCase):
    """Owner rule 2026-10-06: a company called 2 weeks before the turn must not be redialed 2 weeks later -- at least
    sched_quarter_turn_min_days (42) after the last call."""

    def row(self, last_call):
        from app.queue_model import row_from_dict
        d = {h: "" for h in HEADER}
        d.update({"company_name": "Gap Co", "phone_e164": "+12025551234", "timezone": "America/New_York", "state": "VA",
                  "current_quarter_attempts": "1", "this_quarter_status": "confirmed_miss", "last_call_date": last_call,
                  "last_outcome": "voicemail", "next_eligible_date": ""})
        return row_from_dict(d, row_number=2)

    def test_a_company_called_two_weeks_before_the_turn_waits_until_six_weeks_after_that_call(self):
        from datetime import datetime
        r = self.row("2026-09-24")
        local = datetime(2026, 10, 6, 21, 35, tzinfo=timezone.utc)
        reset = scheduler.compute_quarter_reset(r, local)
        self.assertEqual(reset["next_eligible_date"], "2026-11-05")
        for k, v in reset.items():
            r.set(k, v)
        self.assertEqual(scheduler.evaluate(r, ET_EVENING).action, "skip")           # 10-06: held
        late = datetime(2026, 11, 9, 2, 35, tzinfo=timezone.utc)                      # Sun 11-08, 9:35pm Eastern
        self.assertEqual(scheduler.evaluate(r, late).action, "dial")

    def test_an_old_call_is_not_held_at_all(self):
        from datetime import datetime
        r = self.row("2026-06-20")
        reset = scheduler.compute_quarter_reset(r, datetime(2026, 10, 6, 21, 35, tzinfo=timezone.utc))
        self.assertEqual(reset["next_eligible_date"], "")


class _OpenGate:
    def allow(self, rows):
        return True, ""


class SolicitationRefusedIsDoNotCall(unittest.TestCase):
    """Owner rule 2026-10-05: a number that says it does not accept solicitation calls is do-not-call."""

    T = "Number does not accept solicitation calls. If you're a customer,"

    def test_the_phrase_is_detected(self):
        from app import ivr
        self.assertTrue(ivr.refuses_solicitation(self.T))
        self.assertFalse(ivr.refuses_solicitation("Please leave a message after the tone."))

    def test_it_is_never_an_email_target(self):
        from app import email_safety
        safe, why = email_safety.assess({"company_name": "Restoration Xpress", "outcome": "gatekeeping_miss", "ivr_transcript": self.T})
        self.assertEqual(safe, "no")
        self.assertIn("solicitation", why)

    def test_the_queue_row_gets_do_not_call(self):
        from app import queue_writer
        from app.queue_backend import MemoryQueue
        d = {h: "" for h in HEADER}
        d.update({"company_name": "Restoration Xpress", "phone_e164": "+12025550188", "timezone": "America/New_York",
                  "current_quarter_attempts": "1", "this_quarter_status": "in_progress"})
        q = MemoryQueue([d])
        queue_writer.apply_outcome(q, "+12025550188", "gatekeeping_miss", call_sid="CAx", do_not_call=True)
        self.assertEqual(q.find_by_phone("+12025550188")._g("do_not_call"), "true")
        d2 = dict(d, phone_e164="+12025550199")
        q2 = MemoryQueue([d2])
        queue_writer.apply_outcome(q2, "+12025550199", "gatekeeping_miss", call_sid="CAy")
        self.assertNotEqual(q2.find_by_phone("+12025550199")._g("do_not_call"), "true")

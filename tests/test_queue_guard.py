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

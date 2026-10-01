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

"""email_safe: real rows whose correct treatment is known. A wrong `yes` is an
email to a real business making a claim we can't support, so every case here is
a call that was actually wrong or risky."""
from __future__ import annotations

import unittest

from app.email_safety import assess


def row(outcome, transcript="", digits="", notes=""):
    return {"outcome": outcome, "ivr_transcript": transcript, "digits_sent": digits, "notes": notes}


class EmailSafety(unittest.TestCase):
    def test_clear_voicemail_greeting_is_yes(self):
        r = row("voicemail", "Hello. You've reached Jake with Mold Removal. Leave a message, and I'll call you back.")
        self.assertEqual(assess(r)[0], "yes")

    def test_menu_wording_is_not_a_voicemail_greeting(self):
        """Greenville: pressed 9, the only 'voicemail' wording was the menu's own
        'if you would like to leave a message' -- must not be a yes."""
        r = row("voicemail", "If you have an emergency press nine now. Otherwise... If you would like to leave a message,", "9")
        self.assertEqual(assess(r)[0], "review")

    def test_two_digits_is_never_a_yes_for_a_voicemail(self):
        """Johnston pressed 0,0 (not the emergency option) and reached a voicemail."""
        self.assertEqual(assess(row("voicemail", "Please leave your name, number", "0,0"))[0], "no")

    def test_extended_hold_is_never_email_safe(self):
        """Rare Restoration: hung up at the timer, their manager answered."""
        a, why = assess(row("extended_hold", "Please hold while we connect your call to emergency one.", "1,2"))
        self.assertEqual(a, "no")
        self.assertIn("never a confirmed miss", why)

    def test_answered_unknown_unresolved_disconnected_busy_are_no(self):
        for o in ("answered", "unknown", "ivr_unresolved", "disconnected", "busy"):
            with self.subTest(o):
                self.assertEqual(assess(row(o))[0], "no")

    def test_a_voicemail_without_voicemail_wording_is_no(self):
        self.assertEqual(assess(row("voicemail", "Thank you for calling. Please hold."))[0], "no")

    def test_alt_contact_and_gatekeeping(self):
        self.assertEqual(assess(row("alt_miss", "If it's an emergency, text us."))[0], "yes")
        self.assertEqual(assess(row("alt_miss", "text us", "1"))[0], "review")
        self.assertEqual(assess(row("gatekeeping_miss", "enter your ZIP code"))[0], "yes")

    def test_no_answer_is_yes(self):
        self.assertEqual(assess(row("no_answer"))[0], "yes")

    def test_classifier_outage_downgrades_to_review(self):
        r = row("voicemail", "leave a message after the tone",
                notes="tail: anthropic unavailable (BadRequestError: credit balance is too low)")
        self.assertEqual(assess(r)[0], "review")

    def test_a_row_that_says_its_evidence_is_unreliable_is_no(self):
        r = row("voicemail", "leave a message", notes="evidence unreliable; ivr: haiku: ...")
        self.assertEqual(assess(r)[0], "no")

    def test_a_row_without_a_company_name_is_never_email_safe(self):
        """Franchise / own-cell test calls carry no company name (56 rows on 2026-09-30)."""
        r = row("voicemail", "leave a message after the tone")
        r["company_name"] = ""
        a, why = assess(r)
        self.assertEqual(a, "no")
        self.assertIn("no company name", why)
        r["company_name"] = "Real Restoration Co"
        self.assertEqual(assess(r)[0], "yes")

    def test_every_answer_has_a_reason(self):
        for o in ("voicemail", "alt_miss", "gatekeeping_miss", "no_answer", "extended_hold", "answered", ""):
            a, why = assess(row(o, "leave a message"))
            self.assertIn(a, ("yes", "review", "no"))
            self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()

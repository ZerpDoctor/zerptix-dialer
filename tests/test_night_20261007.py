"""Real rows from the 2026-10-07 UTC batch (237 calls, three numbers at 80 each), adjudicated by the owner.
Deterministic only -- every Anthropic call is forced to fail (see test_classification_rules.Base)."""
from __future__ import annotations

import dataclasses
import time
import unittest

from app import ivr, media_stream, server
from tests import test_classification_rules as tc


class Night20261007(tc.Outcomes):
    LION = "Press one for residential. Press two for commercial. Press one for residential. Press two for commercial."

    def test_lion_a_menu_that_only_splits_residential_and_commercial_is_pressed(self):
        q = ivr.quick_digit(self.LION, require_complete=True)
        self.assertEqual(q[0], "1")
        self.assertFalse(q[2])
        self.assertEqual(ivr.quick_digit("Press one for commercial. Press two for residential.")[0], "2")

    def test_a_customer_type_menu_with_any_other_kind_of_option_is_left_alone(self):
        self.assertIsNone(ivr.quick_digit("Press one for residential. Press two for commercial. Press three for billing."))
        self.assertIsNone(ivr.quick_digit("Press one for residential. Press two for an existing account."))
        self.assertIsNone(ivr.quick_digit("Press one for janitorial. Press two for commercial."))

    def test_american_property_an_existing_customer_option_is_never_the_sole_way_forward(self):
        t = "Thank you for calling American Property Restoration. If you have a question about an existing service, please dial two."
        self.assertIsNone(ivr.quick_digit(t))
        self.assertIsNone(ivr.quick_digit(t, require_complete=True))

    def test_mountain_view_please_wait_while_i_connect_you_is_a_hold_not_a_redirect(self):
        t = "Please wait while I try to connect you."
        self.assertFalse(ivr.looks_like_alt_contact(t).is_alt_contact)
        self.assertIsNotNone(ivr.hold_pending(t))
        self.assertFalse(ivr.looks_like_alt_contact("Please hold while I try to connect you.").is_alt_contact)
        # screening is still screening
        self.assertTrue(ivr.looks_like_alt_contact("State your name and Google Voice will try to connect you.").is_alt_contact)

    VIP_PRE = "Experiencing higher than normal call volume. If you would like to receive a callback, please press one now."
    VIP_POST = (" Thank you for using our callback feature. Press one if you wanna be called back on the number that you are calling "
                "from now. Press two to enter a different number. Now please press one to be contacted by the next available agent. "
                "Press two to enter a time to be contacted. Your confirmation number is eight seven two Repeating. "
                "Your confirmation number is eight seven two")

    def test_vip_a_callback_queue_after_the_press_is_a_hold_not_a_menu_playing_again(self):
        self.assertIsNotNone(ivr.queue_or_callback(self.VIP_POST))
        rec = self.rec(self.VIP_PRE + self.VIP_POST, idx=len(self.VIP_PRE), digits=["1", "1"])
        server.STORE.seconds_since_answered = lambda sid: 90.0
        out, note = server._compute_outcome(rec)
        self.assertEqual(out, "extended_hold", note)

    def test_a_queue_call_that_ended_early_is_not_a_hold(self):
        rec = self.rec(self.VIP_PRE + self.VIP_POST, idx=len(self.VIP_PRE), digits=["1"])
        server.STORE.seconds_since_answered = lambda sid: 25.0
        self.assertEqual(server._compute_outcome(rec)[0], "ivr_unresolved")

    ROCK_PRE = ("Thank you for calling Rock Environmental. For asbestos services, press one. For demolition services, press two. "
                "For employment related queries, press three. Hey there. I'm the AI receptionist at Rock How can I help you today?")
    ROCK_POST = (" I'm sorry. I didn't catch that. Could you please say that again a bit more clearly? I'm here to help. "
                 "It seems we've been disconnected. Please feel free to call back if you need further assistance. Goodbye.")

    def test_rock_an_ai_receptionist_that_spoke_before_the_last_press_is_still_an_answer(self):
        rec = self.rec(self.ROCK_PRE + self.ROCK_POST, idx=len(self.ROCK_PRE), digits=["1", "2"])
        out, note = server._compute_outcome(rec)
        self.assertEqual(out, "answered", note)

    def test_a_menu_that_merely_mentions_an_assistant_is_not_an_ai_answer(self):
        t = "Thank you for calling. Our virtual assistant can take your payment. Press one for billing. Press two for service."
        rec = self.rec(t, idx=0, digits=["2"])
        self.assertNotEqual(server._compute_outcome(rec)[0], "answered")

    def test_high_tide_the_company_named_in_full_after_a_recording_disclosure(self):
        t = ("All calls are recorded for quality and training purposes. A great day at Hytab Restoration and Cleaning. "
             "It's a great day at High Tide Restoration and Cleaning.")
        self.assertTrue(ivr.echoed_name_after_opening(t, "High Tide Restoration and Cleaning"))
        self.assertFalse(ivr.echoed_name_after_opening(
            "All calls are recorded for quality and training purposes. Thank you for calling High Tide Restoration and Cleaning.",
            "High Tide Restoration and Cleaning"))
        self.assertFalse(ivr.echoed_name_after_opening("You have reached High Tide Restoration and Cleaning.",
                                                       "High Tide Restoration and Cleaning"))

    def test_next_level_and_you_re_calling_about_is_a_question_to_the_caller(self):
        t = "Call may be monitored or recorded. Thank you for calling XO registration. And you're calling about twelve restoration."
        self.assertIsNotNone(ivr.reactive_greeting(t))
        self.assertIsNone(ivr.reactive_greeting("Thank you for calling. If you're calling about a claim, press one."))

    def test_prime_aire_dial_one_one_eight_is_extension_118_not_digit_1(self):
        t = ("Thank you for calling PrimeAir. Our office hours are Monday through Friday, nine AM to five PM. To leave a message with "
             "one of our agents, please stay on the line. To reach our emergency services, please dial one one eight.")
        q = ivr.quick_digit(t, require_complete=True)
        self.assertEqual((q[0], q[2]), ("118", True))
        # nine one one is never an extension, and a plain "press one" menu is unchanged
        self.assertNotIn("911", [d for d, _ in ivr.parse_options("If this is a medical emergency, hang up and dial nine one one. Press two for service.")])
        self.assertEqual(ivr.quick_digit("For emergency service, press one. For billing, press two.")[0], "1")

    def test_restoration_relief_a_voice_assistant_that_responds_is_an_ai_receptionist(self):
        t = "I'm using a voice assistant to convert your voice to text and respond to you. If you want to continue, please stay on the call."
        d = ivr.decide_tail(t, "machine_start", "Restoration Relief")
        self.assertEqual((d.outcome, d.classifier), ("answered", "rule"))

    AERET = [-16, -19, -77, -77, -78, -20, -16, -18, -77, -77, -77, -21, -16, -18, -84, -53, -62, -32, -29, -32, -30, -31]
    ROYAL = [-81, -7, -13, -15, -21, -22, -74, -74, -73, -30, -21, -22, -73, -73, -73, -30, -21, -22, -74, -74]
    TOTAL_CARE = [-60, -8, -8, -23, -22, -17, -14, -49, -32, -19, -25, -29, -25, -23, -26, -22, -25, -22, -27, -30]
    OPERATION = [-23, -90, -71, -64, -65, -67, -68, -69, -69, -70, -71, -72, -72, -73, -73, -73, -73, -73, -73, -73]
    HIGH_TIDE = [-19, -19, -25, -24, -28, -50, -15, -13, -17, -90, -90, -90, -16, -45, -28, -23, -30, -67, -78, -76, -46, -44, -49, -71, -75]

    def test_ringing_after_a_greeting_is_recognised_and_other_audio_is_not(self):
        self.assertTrue(media_stream.ringing_after_greeting(self.AERET))
        self.assertTrue(media_stream.ringing_after_greeting(self.ROYAL))
        for e in (self.TOTAL_CARE, self.OPERATION, self.HIGH_TIDE, []):
            self.assertFalse(media_stream.ringing_after_greeting(e), e)

    def test_aeret_a_bare_greeting_then_ringing_is_not_an_answer(self):
        buf = media_stream.get_buffer("CAgolden")
        try:
            buf.energy = list(self.AERET)
            rec = self.rec("Thank you for calling.", answered_by="human", company="AERET Restoration")
            out, note = server._compute_outcome(rec)
            self.assertEqual(out, "unknown", note)
            buf.energy = list(self.OPERATION)           # a person who said hi and waited stays answered
            rec = self.rec("Hi.", answered_by="human", company="Operation Restoration")
            self.assertNotIn("rang", server._compute_outcome(rec)[1])
        finally:
            media_stream.drop_buffer("CAgolden")


class SpeechAfterTheLastTurnIsNotLost(tc.Base):
    """Pro Services, 2026-10-06: the far end hung up as its menu began. Turns read the stream buffer every few
    seconds, so the last utterance was in the buffer but never in the transcript."""

    def setUp(self):
        super().setUp()
        self._saved = (server.hang_up, server._write_sheet_with_retry, server._update_queue_row, server.get_answered_by)
        server.get_answered_by = lambda sid: ""
        self._cfg = server.CFG
        server.CFG = dataclasses.replace(server.CFG, stream_transcription_enabled=True)
        self.rows = []
        server.hang_up = lambda sid: None
        server._write_sheet_with_retry = lambda row: self.rows.append(row)
        server._update_queue_row = lambda rec, outcome: None

    def tearDown(self):
        server.hang_up, server._write_sheet_with_retry, server._update_queue_row, server.get_answered_by = self._saved
        super().tearDown()

    def test_unread_buffer_text_is_merged_when_the_far_end_hangs_up(self):
        sid = "CAflush0001"
        server.STORE.register(sid, "+15555550199", company_name="Pro Services")
        buf = media_stream.get_buffer(sid)
        buf.connected_at = time.time()
        buf.append("Hello, and thank you for calling Pro Services.")
        server.STORE.add_turn(sid, buf.text_since_last_read())          # a turn read the first sentence
        buf.append("Please listen carefully to the following menu.")      # arrived after the last turn
        buf.flushed.set()
        with server.app.test_request_context("/x", method="POST", data={"To": "+15555550199"}):
            server._resolve(sid, hangup=False)
        self.assertIn("following menu", self.rows[0]["ivr_transcript"])
        self.assertTrue(self.rows[0]["ivr_transcript"].startswith("Hello, and thank you for calling Pro Services."))

    def test_the_flush_event_starts_unset(self):
        self.assertFalse(media_stream.StreamBuffer().flushed.is_set())


if __name__ == "__main__":
    unittest.main()

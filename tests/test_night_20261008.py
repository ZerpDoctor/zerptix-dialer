"""Real rows from the 2026-10-08 UTC batch (241 calls), adjudicated by the owner. Deterministic only."""
from __future__ import annotations

import types
import unittest

from app import ivr, media_stream, server
from app.email_safety import assess, greeting_names_other_business
from tests import test_classification_rules as tc

# transcripts captured from the 2026-10-08 UTC batch (real rows)
MGM = 'Thank you for calling. To ensure the highest level of customer care, this call may be recorded. Please hold while we connect your call. Now you have reached MGM Recovery. Sorry we missed your call. We are either in the field or on the other line assisting another customer. If you could please leave your name and number, we will give you a callback as soon as possible. If this is an emergency, please call us at eight seven seven seven nine nine nine nine eight seven.'
DELAWARE = "Thank you for calling Delaware County Restoration. Our office is currently closed. If this is an emergency, please press two to speak to an on call project manager. For a dial by name director Hi. You've reached Jim from Delaware County. Restoration. I'm unable to take your call right now, so please leave me a voice mail, and I will call you right back. If you need immediate assistance, please contact Andrea Johnson at six one zero eight zero nine nine nine six seven. Thank you."
GRAYSTONE = 'Thank you for calling Greystone Restoration. Our office hours are eight AM to five PM, Monday through Friday, If you have called during normal business hours, please leave your name, phone number, and a brief message, and we will return your call shortly. If this is an after hours call and not an emergency, please leave your name, phone number, and a brief message, and we will call you the next business day. If this is an after hours emergency, please hang up and call eight one three seven three four'
ENVRES = "You have reached Environmental Resources. We're sorry we're unable to get to the phone at this time. If you'll leave your name, number, and a brief message, we'd be happy to return your call. If you need additional assistance, you could reach seven eight one two four eight nine nine seven five. Thanks, and have a great day. At the tone, please record your message. When you have finished recording, you may hang up. Or press one for more options. We did not get your mess Thank you. Goodbye."
OLDDOM = 'Thank you for calling Old Dominion Specialty Construction. Our office hours are from eight AM to four PM Monday through Friday. Please leave your name, phone number, and a brief message, and someone will return your call as soon as possible. If you are calling for our twenty four hour emergency services, please call seven zero three five zero eight five eight three zero or five four zero eight four two six one one zero. Again, thank you for calling Old Dominion Specialty Construction.'
BLACKD = 'Thank you for calling Black Diamond Remediation located in Denver, Massachusetts. Your call is very important to us. Please listen carefully to the following options. If you know the extension of the person you were trying to reach, you may dial at any time.'
CARY = "Thanks for calling Triangle Reconstruction. We're closed right now, so I'm the assistant picking up. Tell me what's going on with your home, and I'll make sure the team gets back to you first thing. Are you still there?"
LIBERTY = "Thank you for calling Liberty Restoration If you require immediate assistance, dial zero now. If you know your party's extension, dial it at any time. Please hold while we connect your call to extension zero. At the tone, please say your name then press pound."
CNB = 'This call will be recorded for quality purposes. Hello. You are now being directly connected to a CNV team member. Thank you. This call is being recorded.'
TOTALFIRE = 'Get this valuable number today at eight hundred dot com or by texting eight hundred eight hundred nine zero three eight.'
CDR = 'Thank you for calling Commercial Disaster Recovery. Please press one. For emergency services, two for accounting, or three for general'
BLUERIVER = "For calling Blue River Environmental and Restoration. Our regular business hours are Monday through Friday from eight to five. If you know your party's extension, you can dial it at any time. To dial by name, please press one. If this matter requires immediate assistance, please Using the keys on your touch tone phone, please enter the name of the party you wish to reach last name first. Press star at any time to return to the main menu. There are too many parties matching your entry. Please enter more letters. There are too many parties matching your entry. Please enter more letters. Please stay on the line while your call is transferred to the operator."
DRYTECH = 'Thank you for calling DryTek Restoration. A twenty four seven emergency water removal service company. Please listen to the prompts carefully. Press any key to start a text conversation. For emergency water removal services and fire damage, please press star. One. For the billing depart Thank you for calling DryTek Restoration. A twenty four seven emergency water removal service company. Please listen to the prompts carefully. Press any key to start a text conversation. For emergency water removal services and fire damage, please press star. One. For the billing department and Bessie Gonzales, press star two. For estimates on mold and asbestos. Please press star three. For the reconstruction department, please press star four. If you prefer to speak with someone directly, please stay on the line. And we will answer your call as soon as possible. Thank you. Thank you for calling DryTek Restoration. A twenty four seven emergency water removal service company. Please listen to the prompts carefully. Press any key to start a text conversation. For emergency water removal services and fire damage, please press star. One. For the billing department and Bessie Gonzales, press star two. For estimates on mold and asbestos.'
ENVPROT = 'Thank you for calling Home Insights Home Inspections. Unfortunately, we are unable to answer the phone right now. May be on the other line with a customer, or it may be outside of business hours. Please leave your name and phone number and what type of inspection we can help you with and we will return your call as soon as possible. You may also send a text message to eight one three eight nine eight six five four five. Thank you, and have a great day.'
STORM = "Thank you for choosing Red Dirt Sanitation. You're reliable, service for rural communities. Our office is now closed. Please leave a message, and we will return your call the next business day. Again, thank you for choosing Red Dirt Sanitation. We look forward to serving you. Thank you for choosing Red Dirt Sanitation. You're reliable, service for rural communities. Our office is now closed. Please leave a message, and we will return your call the next business day. Again, thank you for choosing Red Dirt Sanitation. We look forward to serving you. Thank you for choosing Red Dirt Sanitation, your reliable, service for rural communities. Our office is now closed. Please leave a message, and we will return your call the next business day."


# per-second dBFS of "Mold Masters. Mold Masters." (a person repeating the greeting) -- NOT ringing
MOLD_MASTERS = [-54, -72, -29, -36, -73, -72, -25, -28, -73, -73, -73, -73]
AERET = [-16, -19, -77, -77, -78, -20, -16, -18, -77, -77, -77, -21, -16, -18, -84, -53, -62, -32, -29, -32, -30, -31]
ROYAL = [-81, -7, -13, -15, -21, -22, -74, -74, -73, -30, -21, -22, -73, -73, -73, -30, -21, -22, -74, -74]


class Night20261008(tc.Outcomes):
    def test_mold_masters_a_greeting_said_twice_is_not_ringing(self):
        self.assertFalse(media_stream.ringing_after_greeting(MOLD_MASTERS))
        self.assertTrue(media_stream.ringing_after_greeting(AERET))
        self.assertTrue(media_stream.ringing_after_greeting(ROYAL))
        buf = media_stream.get_buffer("CAgolden")
        try:
            buf.energy = list(MOLD_MASTERS)
            rec = self.rec("Mold Masters. Mold Masters.", answered_by="human", company="Mold Masters")
            out, note = server._compute_outcome(rec)
            self.assertNotIn("rang", note)
        finally:
            media_stream.drop_buffer("CAgolden")

    def test_drytech_press_star_one_is_the_two_key_sequence(self):
        q = ivr.quick_digit(DRYTECH, require_complete=True)
        self.assertEqual((q[0], q[2]), ("*1", True))
        self.assertEqual(ivr._norm_digit("star. One"), "*1")
        self.assertEqual(ivr._norm_digit("star two"), "*2")
        self.assertEqual(ivr.quick_digit("For billing, press star. For emergencies press one.")[0], "1")

    def test_blue_river_dial_by_name_is_never_the_sole_way_forward(self):
        self.assertIsNone(ivr.quick_digit("Thank you for calling Blue River. If you know your party's extension, you can dial it at any time. To dial by name, please press one."))

    def test_an_emergency_number_in_a_greeting_is_an_alt_miss_by_rule(self):
        for name, tr in (("mgm", MGM), ("delaware", DELAWARE), ("graystone (number cut off)", GRAYSTONE), ("envres", ENVRES), ("olddom", OLDDOM)):
            self.assertTrue(ivr.emergency_redirect_number(tr), name)
        rec = self.rec(MGM, answered_by="machine_start", company="MGM Recovery", ivr_detected=False)
        self.assertEqual(server._compute_outcome(rec)[0], "alt_miss")
        self.assertFalse(ivr.emergency_redirect_number("If this is a medical emergency, call nine one one. Please leave a message."))
        self.assertFalse(ivr.emergency_redirect_number("Please leave a message and we will call you back at your number. Our office is five five five one two three four."))

    def test_graystone_rang_then_a_cut_off_number_is_an_alt_miss_at_the_cap(self):
        rec = self.rec(GRAYSTONE, answered_by="human", company="Graystone Restoration", ivr_detected=False)
        rec.hit_time_cap = True
        self.assertEqual(server._compute_outcome(rec)[0], "alt_miss")

    def test_a_voicemail_control_menu_is_not_pressed(self):
        self.assertIsNone(ivr.quick_digit(ENVRES))

    def test_black_diamond_an_extension_prompt_is_not_a_redirect(self):
        d = ivr.decide_alt_contact(BLACKD)
        self.assertFalse(d.is_alt_contact)

    def test_cary_an_assistant_picking_up_is_an_ai_receptionist(self):
        d = ivr.decide_tail(CARY, "machine_start", "Cary Reconstruction")
        self.assertEqual((d.outcome, d.classifier), ("answered", "rule"))

    def test_c_and_b_a_recorded_transfer_is_a_hold_not_an_answer(self):
        self.assertIsNotNone(ivr.hold_pending(CNB))
        rec = self.rec(CNB, answered_by="machine_start", company="C&B Complete Cleaning & Construction", ivr_detected=False)
        server.STORE.seconds_since_answered = lambda sid: 23.0
        self.assertEqual(server._compute_outcome(rec)[0], "ivr_unresolved")

    def test_liberty_restoration_a_screening_prompt_is_an_alt_miss(self):
        self.assertTrue(ivr.call_screening(LIBERTY))
        rec = self.rec(LIBERTY, idx=60, digits=["0"], company="Liberty Restoration")
        server.STORE.seconds_since_answered = lambda sid: 26.0
        self.assertEqual(server._compute_outcome(rec)[0], "alt_miss")

    def test_a_lapsed_toll_free_number_playing_the_800_ad_is_disconnected(self):
        self.assertEqual(ivr.decide_tail(TOTALFIRE, "machine_start", "Total Fire and Water Restoration").outcome, "disconnected")

    def test_commercial_disaster_recovery_the_emergency_option_is_one(self):
        q = ivr.quick_digit(CDR, require_complete=True)
        self.assertEqual((q[0], q[2]), ("1", True))


class WrongBusinessGreeting(unittest.TestCase):
    def row(self, company, transcript, outcome="voicemail"):
        return {"company_name": company, "outcome": outcome, "ivr_transcript": transcript, "digits_sent": "", "notes": ""}

    def test_a_greeting_for_another_business_is_never_a_proven_miss(self):
        self.assertTrue(greeting_names_other_business("Environmental Protective Solutions", ENVPROT))
        self.assertTrue(greeting_names_other_business("Storm Damage Services", STORM))
        self.assertEqual(assess(self.row("Environmental Protective Solutions", ENVPROT, "alt_miss"))[0], "review")

    def test_the_same_business_garbled_by_speech_to_text_is_fine(self):
        self.assertFalse(greeting_names_other_business("Fowcon Restoration", "Thank you for calling Falcon Restoration. Please leave a message."))
        self.assertFalse(greeting_names_other_business("Moldgone", "Hello. You've reached Kevin with Mold Gone. Leave a message."))
        self.assertFalse(greeting_names_other_business("H2Odryout", "Thank you for calling H2O dry out Southwest Florida."))
        self.assertFalse(greeting_names_other_business("Level Creek Property Restoration", "Thank you for calling. Please leave a message after the tone."))


class CarrierFailedIsNotADeadNumber(tc.Outcomes):
    def test_failed_and_canceled_are_unknown_and_retried_not_disconnected(self):
        from app import outcomes
        for cs in ("failed", "canceled"):
            rec = self.rec("", ivr_detected=False)
            rec.call_status = cs
            out, note = server._compute_outcome(rec)
            self.assertEqual(out, "unknown", cs)
            self.assertIn("retried", note)
        self.assertEqual(outcomes.from_call_status("busy"), "busy")
        self.assertEqual(outcomes.from_call_status("no-answer"), "no_answer")
        self.assertTrue(outcomes.is_terminal_status("failed"))

    def test_only_a_carrier_announcement_is_a_confirmed_dead_number(self):
        d = ivr.decide_tail("We're sorry. The number you have dialed is not in service.", "unknown", "RESCON")
        self.assertEqual(d.outcome, "disconnected")


if __name__ == "__main__":
    unittest.main()

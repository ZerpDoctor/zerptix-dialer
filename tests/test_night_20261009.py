"""Real rows from the 2026-10-09 UTC batch (235 calls), adjudicated by the owner. Deterministic only."""
from __future__ import annotations

import unittest

from app import ivr, server
from tests import test_classification_rules as tc

# transcripts captured from the 2026-10-09 UTC batch (real rows)
DANDB = "Hi. You've reached DMV restoration services. Leave a detailed message. We'll get back D and B Restoration Services. Hello?"
PROMO = "This call may be recorded for quality assurance. We have a special promotion today for select callers. If you are over fifty, please press one now. If not, press pound. If you are over Congratulations. You've been selected for a special promotion today to receive a free medical alert device. You know, the life saving button that gets you help instantly during an emergency. These devices normally cost hundreds of dollars, So press one now to get yours absolutely free. Press one immediately to secure your free device. And enjoy guaranteed twenty four seven protection. This exclusive offer is only available during this call Press one now to claim your free device. Or stay on the line for more options. Thank you for calling. This is Jessica on a recorded line. Can you hear me okay? Great. So, with our promotion"
KC = "You've reached KC Construction Services. Our office hours are Monday through Friday. Nine AM to five PM eastern time. We're sorry we missed your call. Your business is very important to us. To leave a voice mail, please press one. If this is an emergency, please stay on the line. Your call will be directed accordingly. Thank you for choosing KC Construction Services."
ACE = 'Thank you for calling Acemote Specialist. For Javi, press one. For Nachman, press two. For Leah, press three. For Isaac, press four. Or stay on the line to speak to a representative. Thank you.'
EDD = 'Directly to voice mail. Press star. Thank you for calling. Please leave a message, and your call will be returned as soon as possible. To review the voice mail, press one. To send the voice mail now, press two. To discard and rerecord, press three. For special sending options, press four.'
MOLDDET = 'Hello. And thank you for calling the experts in mold removal. For mold removal and mold remediation, please press one. For mold testing, please press two. Thanks. Please enter your ZIP code Thanks. Please enter your ZIP code Thanks. Please enter your ZIP code Please press one to be connected. Please press one to be connected. Goodbye.'
THOMAS = 'This call may be recorded for quality assurance purposes. Thank you for calling Thomasville Restoration. Are you calling to report an emergency or a new claim? Thank you for calling Thomasville Are you calling to report a new emergency or a new claim? Due to no response in the line, this call is being ended. Thank you for calling.'
REMPROS = "To ensure the highest level of customer service, this call may be monitored and recorded. Hi. Thanks for calling Remediation Pros LLC. I'm Anna, an automated assistant, and this call is recorded. I'll take a message for the team. May I have your first and last name?"


class Night20261009(tc.Outcomes):
    def test_d_and_b_a_person_cutting_off_the_recording_is_a_pickup(self):
        self.assertIsNotNone(ivr.pickup_after_greeting(DANDB, "D And B Restoration Services"))
        d = ivr.decide_tail(DANDB, "machine_start", "D And B Restoration Services")
        self.assertEqual((d.outcome, d.classifier), ("answered", "rule"))
        # a plain voicemail that ends with the name is not a pickup
        self.assertIsNone(ivr.pickup_after_greeting("Hi. You've reached the office. Leave a message. Thank you. D and B Restoration Services.", "D And B Restoration Services"))

    def test_the_promotional_robocall_is_never_a_claim_and_never_pressed(self):
        self.assertTrue(ivr.promo_recording(PROMO))
        self.assertFalse(ivr.decide_digit(PROMO).is_menu)
        rec = self.rec(PROMO, answered_by="machine_start", company="Laser Restoration", ivr_detected=False)
        out, note = server._compute_outcome(rec)
        self.assertEqual(out, "unknown")
        self.assertIn("promotional", note)
        self.assertFalse(ivr.promo_recording("Thank you for calling. If this is an emergency press one. Special promotion on carpet cleaning this month."))

    def test_never_press_leave_a_voice_mail_or_a_persons_extension(self):
        for tr in (KC, ACE):
            d = ivr.decide_digit(tr)
            self.assertFalse(d.is_menu and d.digit is not None, tr[:40])

    def test_a_voicemail_systems_own_star_prompt_is_not_pressed(self):
        self.assertIsNone(ivr.quick_digit(EDD))
        d = ivr.decide_digit(EDD)
        self.assertFalse(d.is_menu and d.digit is not None)

    def test_a_zip_gate_after_the_press_is_a_gatekeeping_miss(self):
        rec = self.rec(MOLDDET, idx=120, digits=["1", "2"], company="Mold Detection & Remediation Specialists")
        server.STORE.seconds_since_answered = lambda sid: 80.0
        self.assertEqual(server._compute_outcome(rec)[0], "gatekeeping_miss")

    def test_a_question_to_the_caller_and_an_automated_assistant_are_answers(self):
        self.assertIsNotNone(ivr.reactive_greeting(THOMAS))
        self.assertEqual(ivr.decide_tail(THOMAS, "human", "Thomasville Restoration").outcome, "answered")
        d = ivr.decide_tail(REMPROS, "machine_start", "Remediation Pros")
        self.assertEqual((d.outcome, d.classifier), ("answered", "rule"))


class PostPressAndLabelFixes(tc.Outcomes):
    JJ = ("Thank you for calling J and J ERS for emergency services or to schedule. Please press one. "
          "To speak with someone in the office, please press two.")
    DART = ("Thank you for calling Dart Restoration. You have reached us outside of our normal business hours. If you are experiencing "
            "an emergency and need immediate assistance, please press one now to be connected with a live representative. "
            "If your call is not urgent, please leave your")
    RESTORENOW_2 = ("Thank you for calling RestoreNow. All calls are recorded for quality and training purposes. If you have an "
                    "emergency need for services, please press one to be connected to our on call project manager. All other "
                    "callers, please hold to leave us a message. Our office is currently closed. This is the general voice mailbox "
                    "for RestoreNow. Please record your detailed message, and a member of our team will return your call.")
    ONETEAM = ("Thank you for calling OneTeam Restoration. If you are currently experiencing a water or fire loss in your home or "
               "business, press one. For mold remediation, press two.")

    def test_jj_ers_the_label_before_a_bare_please_press_one_is_the_emergency_option(self):
        q = ivr.quick_digit(self.JJ)
        self.assertEqual((q[0], q[2]), ("1", True))

    def test_dart_the_rest_of_the_greeting_our_press_interrupted_is_not_a_voicemail(self):
        idx = self.DART.index("If your call is not urgent")
        self.assertEqual(ivr.post_press_tail(self.DART, idx), "")
        rec = self.rec(self.DART, idx=idx, digits=["1"], company="Dart Restoration")
        server.STORE.seconds_since_answered = lambda sid: 30.0
        self.assertNotEqual(server._compute_outcome(rec)[0], "voicemail")

    def test_restorenow_a_fresh_recording_naming_the_emergency_option_again_is_pressed_once(self):
        self.assertEqual(ivr.second_emergency_digit(self.RESTORENOW_2), "1")

    def test_a_second_press_is_never_taken_from_menu_leftovers_or_other_menus(self):
        for t in ("Press two for billing. Press three for sales.",
                  "If you would like to leave a message, press nine.",
                  "Thank you for calling RestoreNow. To help direct your call, please listen. Press one if you are a homeowner. Press two if you are a vendor.",
                  ""):
            self.assertIsNone(ivr.second_emergency_digit(t), t)

    def test_one_team_a_water_or_fire_loss_option_ends_navigation_like_an_emergency_option(self):
        q = ivr.quick_digit(self.ONETEAM)
        self.assertEqual(q[0], "1")
        clause = ivr._option_clause(self.ONETEAM.lower(), "1")[0]
        self.assertTrue(ivr._clause_damage_service(clause))


if __name__ == "__main__":
    unittest.main()

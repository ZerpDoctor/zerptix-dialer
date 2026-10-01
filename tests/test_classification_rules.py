"""Golden cases: real transcripts from 2026-09-29/30 whose correct handling is
known (each one was reviewed by a person). Deterministic rules only -- every
Anthropic call is forced to fail so nothing here depends on a model.

`python -m unittest discover tests` must stay green before every push. When a
new bad call turns up, add its transcript here FIRST, watch it fail, then fix.
"""
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

from app import ivr, server  # noqa: E402


def _unavailable(*a, **k):
    raise ivr.AnthropicUnavailable("tests force the deterministic path")


DRIFORCE = "Thank you for calling Dry Fork Property Restoration. Your call may be recorded for quality purposes."
BOSTON = "Thank you for calling Boston Harbor Water Restoration. Your call may be recorded for quality purposes."
TOBIN = "Thank you for choosing Tobin Restoration. This call may be recorded."
ARCHULETA = "Thank you for calling. Your call may be recorded for quality and training purposes. Good evening."
ON_SITE = ("This call will be recorded for quality assurance. You have reached On-site Specialty Cleaning and "
           "Restoration. If this call is about an emergency loss,")
PRO_SERVICES_TAIL = "To reach accounts payable,"
ABR = "This call may be recorded. ABR."
RESTORE_PROS = "This call may be recorded for quality purposes. Thank you for calling Restore Pros. How may I help you?"
PRECISION_VM = ("Hi, this is Maria Birch with Precision Structures. I am unable to take your phone call. "
                "If you would, please leave your name,")
PHOENIX = ("Welcome to Verizon Wireless. The number you dialed has been changed. Disconnected, or is no longer "
           "in service. If you feel you have reached this recording in error, please check the number and try again.")
TROPICAL = ("Hi. You've reached Tropical Restoration Services. We are unable to take your call at the moment. "
            "Leave a message. Someone will get back to you right away. If it's an emergency, text us. "
            "Nine five four four four five three two zero zero. Thank you.")
MITIGATION_X = ("Hi. You've reached Chris Schotts with MitigationX. I'm sorry I missed your call, but if you leave a "
                "voice mail, I can get back to you as soon as I can. If it's of an urgent nature, please call "
                "seven two zero eight four five two one six four. Thank you.")
REDEMPTION = ("Hi. I'm a call assistant recording this call for the person you're trying to reach. Please say who "
              "you are and why you're calling.")
RAPID = ("The owner of this phone number has enabled automatic spam blocking. To continue, please verify your human "
         "by pressing zero.")
EXCEPTIONAL = "Press any key to receive a text from us and start this conversation right now. Or stay on the line."
JIMMY = ("Thank you for calling. After hours. This call is being recorded. Please hold for the next available agent. "
         "Jenny Garza. Emergency water removal. Jimmy Garza emergency water removal.")
BONEDRY = ("For calling Bone Dry Services. Press one for water emergency. Press two for mitigation or demolition. "
           "Press three if you are an insurance company. Press four for rebuild and reconstruction. "
           "Press five for billing. Thank you for calling Bone Dry Services.")
LR_VOICEMAIL = ("You have reached LR Contracting. Please leave a message with your name, phone number, and reason "
                "for the call. At the tone, please record your message. To disconnect, press one. To record your "
                "message, press two.")
REGAL = ("Thank you for calling Regal Restoration. For emergency services, press one. For mitigation department, "
         "press two. For office, press five. Hi. This is Rob with Regal Restoration. Hello?")
GREENVILLE_TAIL = " when we'll be happy to help. If you would like to leave a message,"
AMERICAN = ("To American Restoration. Proudly serving Colorado since nineteen ninety six. If you are calling for "
            "emergency water or fire damage services, available twenty four hours a day, please press one. For "
            "general contracting services, including new construction, remodeling, or roofing, please press two.")
RELIABLE = ("Reliable restoration and mold free now. To schedule your complimentary mold inspection, dial one. "
            "For restoration services, dial two.")
JOHNSTON = ("Hi. Thank you for calling Johnston Restoration. For emergency service needs or twenty four hour "
            "response time, please press one. For billing, scheduling, press two. For all other needs, press zero.")
RARE = ("Thank you for choosing Rare Restoration. This call may be recorded. Press one now if you are "
        "experiencing a flood or fire for our twenty four hour emergency response line. Press two for "
        "mitigation customer service.")


class Base(unittest.TestCase):
    def setUp(self):
        self._orig = {n: getattr(ivr, n) for n in
                      ("classify_ivr_digit", "classify_call_audio", "classify_alt_contact", "classify_gatekeeping")}
        for n in self._orig:
            setattr(ivr, n, _unavailable)

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(ivr, n, f)


class AutomatedOpenersAndCutOffMenus(Base):
    def test_recorded_openers_are_not_a_person(self):
        for name, t in (("DriForce", DRIFORCE), ("Boston Harbor", BOSTON), ("Tobin", TOBIN), ("Archuleta", ARCHULETA)):
            with self.subTest(name):
                self.assertTrue(ivr.menu_start_reason(t))

    def test_cut_off_menus_are_not_an_outcome(self):
        self.assertTrue(ivr.menu_start_reason(ON_SITE))
        self.assertTrue(ivr.menu_start_reason(PRO_SERVICES_TAIL))

    def test_things_that_must_not_be_treated_as_openers(self):
        self.assertIsNone(ivr.menu_start_reason(ABR), "a bare name after a disclosure is a person (user-confirmed)")
        self.assertIsNone(ivr.menu_start_reason(RESTORE_PROS), "greeting + question is a person")
        self.assertIsNone(ivr.menu_start_reason(PRECISION_VM), "a cut-off voicemail is still a voicemail")

    def test_decide_tail_returns_unresolved_for_an_opener(self):
        d = ivr.decide_tail(DRIFORCE, "human", "DriForce Property Restoration")
        self.assertEqual((d.outcome, d.classifier), ("unknown", "menu_start"))


class DeadNumbersScreeningAndRedirects(Base):
    def test_carrier_message_is_disconnected(self):
        self.assertEqual(ivr.decide_tail(PHOENIX, "machine_start", "Phoenix Contents").outcome, "disconnected")

    def test_alt_contact_gate(self):
        for name, t in (("Tropical text us", TROPICAL), ("Mitigation X call number", MITIGATION_X),
                        ("Redemption screening", REDEMPTION)):
            with self.subTest(name):
                self.assertTrue(ivr.looks_like_alt_contact(t).is_alt_contact)

    def test_911_boilerplate_and_menu_dial_are_not_redirects(self):
        self.assertFalse(ivr.looks_like_alt_contact(
            "if this is a life threatening emergency please call nine one one now").is_alt_contact)
        self.assertFalse(ivr.looks_like_alt_contact("for the directory please dial 3").is_alt_contact)

    def test_call_screening_verify_human_prompt_is_pressed(self):
        self.assertEqual(ivr.human_verification_digit(RAPID), "0")
        self.assertIsNone(ivr.human_verification_digit(EXCEPTIONAL))
        self.assertEqual(ivr.decide_digit(RAPID).digit, "0")


class WhoIsSpeaking(Base):
    def test_person_answering_by_name_after_a_hold_announcement(self):
        d = ivr.decide_tail(JIMMY, "human", "Jimmy Garza Emergency Water Removal")
        self.assertEqual(d.outcome, "answered")

    def test_a_recorded_thank_you_after_hold_is_not_a_person(self):
        t = "Please hold while we connect your call. Thank you for calling Acme Water Restoration."
        self.assertNotEqual(ivr.decide_tail(t, "machine_start", "Acme Water Restoration").outcome, "answered")

    def test_menu_recording_after_a_press_is_not_a_person(self):
        self.assertTrue(ivr.menu_recording_only(BONEDRY))

    def test_voicemail_controls_and_a_real_person_after_a_menu_are_untouched(self):
        self.assertIsNone(ivr.menu_recording_only(LR_VOICEMAIL))
        self.assertIsNone(ivr.menu_recording_only(REGAL))

    def test_thin_evidence_tag(self):
        self.assertTrue(ivr.thin_answer_reason("Keystone restoration."))
        self.assertIsNone(ivr.thin_answer_reason("Idaho Disaster Pro. This is Tiffany."))
        self.assertIsNone(ivr.thin_answer_reason("Hello? Hello?"))


class Routing(Base):
    def test_leftover_menu_after_a_press_is_not_outcome_audio(self):
        full = "...press nine now. Otherwise, please try again during our usual opening hours. Monday through Friday, eight AM to five PM,"
        self.assertEqual(ivr.post_press_tail(full + GREENVILLE_TAIL, len(full)), "")

    def test_emergency_option_is_recognised_from_the_menu_wording(self):
        self.assertTrue(ivr.option_is_emergency(AMERICAN, "1"), "American: pressed correctly, flagged false before")
        self.assertFalse(ivr.option_is_emergency(RELIABLE, "2"), "no emergency option exists in Reliable's menu")
        self.assertTrue(ivr.option_is_emergency(JOHNSTON, "1"))
        self.assertTrue(ivr.option_is_emergency(RARE, "1"), "'emergency' sits past 60 chars into the option")
        self.assertFalse(ivr.option_is_emergency(RARE, "2"))

    def test_fallback_picks_the_emergency_digit_not_the_lowest_or_zero(self):
        self.assertEqual(ivr.choose_digit_by_priority(JOHNSTON).digit, "1", "outage fallback pressed 0 before")


class EmergencyVocabulary(Base):
    """Speech-to-text renders the word as emergency / emergencies / emergent."""

    def test_stem_variants_are_recognised(self):
        for word in ("emergency", "emergencies", "emergent"):
            with self.subTest(word):
                t = f"Please listen carefully. Press one for {word} services. Press two to speak with our office staff."
                q = ivr.quick_digit(t)
                self.assertEqual(q and q[0], "1")

    def test_a_voicemail_option_is_never_chosen_as_the_emergency_one(self):
        """R And S Restores: 'for water damage emergencies please dial 200 to dial by name press 9
        to leave a message dial zero...' -- widening the vocabulary must not send the
        fallback to the message option."""
        t = ("you've reached restores for water damage emergencies please dial 200 to dial by name "
             "press 9 to leave a message dial zero and someone will return your call")
        self.assertNotEqual(ivr.choose_digit_by_priority(t).digit, "9")
        self.assertFalse(ivr.option_is_emergency(t, "9"))

    def test_an_otherwise_clause_does_not_disqualify_the_emergency_option(self):
        """Lanier: 'after hours emergency please press 4 otherwise please leave a detailed message'."""
        t = ("Experiencing an after hours emergency, please press 4. Otherwise, please leave a detailed "
             "message with your name.")
        self.assertEqual(ivr.quick_digit(t), ("4", "the menu names 4 as its emergency option", True))

    def test_non_emergency_variants_are_not_emergencies(self):
        t = "Press one for non-emergent inquiries. Press two for billing."
        self.assertFalse(ivr.option_is_emergency(t, "1"))


LEGACY = ("Thank you for calling Legacy Restoration. For Chicago, press one. Denver, press two. Fort Myers, press three. "
          "Saint Petersburg, press four. Orlando, press five. If this is an emergency, please press six. "
          "For a dial by name directory, press nine.")
TRURENU = ("Thank you for calling TrueRenew. If you know your party's extension, please enter it now or press eight for "
           "the name directory. For sales, press one. For our administrative team, press two. For emergency services, "
           "press three. Or zero for the next available representative.")
BYLT_MENU = ("Thank you for calling Built Restoration. You have reached our after hours emergency dispatch line. "
             "Press one for emergencies, Press two to leave a voice mail, and your call will be returned during normal "
             "business hours. Monday through Friday.")
BYLT_REPLAY = ("Press one for emergencies. Press two to leave a voice mail. And your call will be returned during normal "
               "business hours. Monday through Friday between eight AM and four thirty PM. We thank you again for calling Built.")
DIVERSIFIED_TAIL = ("Thank you for calling Diversified Property Services. Please press one to leave a message or dial the "
                    "extension you are trying to reach. Thank you for calling diverse")
GATEWAY = ("Hello. You've reached Diana Robbins at Gateway Rust Please leave a message, and I will call you back promptly. "
           "If your matter is urgent, please dial John Robbins at seven three four six four five five one seven "
           "Thank you for calling.")
SUNRISE = "Thank you for calling Sunrise. This Al speaking. How can I help you? Hello? Hello? Hello? Can you hear me?"


class EmergencyAttribution(Base):
    """2026-10-01: the emergency words were handed to the PREVIOUS digit when the
    description comes before its number -- Legacy (pressed 5, not 6), TruRenu (2, not 3)."""

    def test_the_emergency_option_is_the_digit_its_own_sentence_names(self):
        self.assertEqual(ivr.quick_digit(LEGACY)[0], "6")
        self.assertEqual(ivr.quick_digit(TRURENU)[0], "3")
        self.assertEqual(ivr.quick_digit(BYLT_MENU)[0], "1", "'press two to leave a voice mail' must not hide option 1")

    def test_the_keyword_fallback_agrees(self):
        self.assertEqual(ivr.choose_digit_by_priority(LEGACY).digit, "6")
        self.assertEqual(ivr.choose_digit_by_priority(TRURENU).digit, "3")

    def test_the_emergency_flag_is_per_option(self):
        self.assertFalse(ivr.option_is_emergency(LEGACY, "5"))
        self.assertTrue(ivr.option_is_emergency(LEGACY, "6"))
        self.assertFalse(ivr.option_is_emergency(TRURENU, "2"))
        self.assertTrue(ivr.option_is_emergency(TRURENU, "3"))

    def test_unpunctuated_text_is_left_to_the_model(self):
        self.assertIsNone(ivr.quick_digit("for water damage emergencies please dial 200 press 9 to leave a message dial zero"))


class NewShapes(Base):
    def test_a_name_between_dial_and_the_number_is_still_a_redirect(self):
        self.assertTrue(ivr.looks_like_alt_contact(GATEWAY).is_alt_contact)

    def test_a_greeting_then_repeated_reactions_is_a_person_or_agent(self):
        self.assertEqual(ivr.decide_tail(SUNRISE, "human", "Sunrise Water Damage").outcome, "answered")
        self.assertIsNone(ivr.reactive_greeting("Hi, how can I help you? Please leave a message at the tone. Hello?"))

    def test_a_menu_playing_again_after_a_press_is_not_a_voicemail_or_a_person(self):
        self.assertTrue(ivr.menu_replayed_after_press(BYLT_REPLAY))
        self.assertTrue(ivr.menu_replayed_after_press(DIVERSIFIED_TAIL))

    def test_a_person_or_a_real_voicemail_after_a_press_is_not_a_replay(self):
        self.assertIsNone(ivr.menu_replayed_after_press("Press two for scheduling. Thank you for calling Acme. This is Anna. How can I help you?"))
        self.assertIsNone(ivr.menu_replayed_after_press("Please leave a message at the tone. Press one for more options."))
        self.assertIsNone(ivr.menu_replayed_after_press("You have reached Water Pro. To leave a message press one. Please leave your name and number."))

    def test_never_end_a_call_on_a_sentence_still_arriving(self):
        self.assertFalse(ivr.ends_cleanly("Thank you for calling diverse"))
        self.assertTrue(ivr.ends_cleanly("Thank you for calling Diversified. How can I help you?"))


class AudioEnergy(unittest.TestCase):
    def test_the_meter_tells_silence_from_sound(self):
        import random
        from app import media_stream
        random.seed(3)
        b = media_stream.StreamBuffer()
        b.add_audio(bytes([0xFF]) * 8000)
        b.add_audio(bytes(random.choice([0x10, 0x90, 0x20, 0xA0]) for _ in range(8000)))
        self.assertLess(b.energy[0], -60)
        self.assertGreater(b.energy[1], -30)


class Outcomes(Base):
    """server._compute_outcome on a reconstructed call record."""

    def rec(self, transcript, *, idx=0, digits=(), ivr_detected=None, answered_by="machine_start", company="Test Co"):
        return types.SimpleNamespace(
            call_sid="CAgolden", call_status="", answered_by=answered_by, company_name=company,
            gatekeeping_detected=False, alt_contact_detected=False, hit_time_cap=False,
            ivr_detected=bool(digits) if ivr_detected is None else ivr_detected,
            digits_sent=list(digits), transcript_accum=transcript, transcript_at_last_digit=idx)

    def setUp(self):
        super().setUp()
        self._sa = server.STORE.seconds_since_answered

    def tearDown(self):
        server.STORE.seconds_since_answered = self._sa
        super().tearDown()

    def test_menu_recording_after_the_press_is_unresolved_not_answered(self):
        pre = "For calling Bone Dry Services. Press one for water emergency."
        rec = self.rec(pre + " Press two for mitigation or demolition. Press five for billing. Thank you for calling Bone Dry Services.",
                       idx=len(pre), digits=["1"])
        out, _ = server._compute_outcome(rec)
        self.assertEqual(out, "ivr_unresolved")

    def test_silence_after_a_press_and_a_call_that_ended_early_is_not_a_hold(self):
        t = "Please press one to be connected."
        rec = self.rec(t, idx=len(t), digits=["1"])
        server.STORE.seconds_since_answered = lambda sid: 19.0
        self.assertEqual(server._compute_outcome(rec)[0], "ivr_unresolved")

    def test_silence_after_a_press_for_the_whole_budget_is_a_hold(self):
        t = "Please press one to be connected."
        rec = self.rec(t, idx=len(t), digits=["1"])
        server.STORE.seconds_since_answered = lambda sid: 66.0
        self.assertEqual(server._compute_outcome(rec)[0], "extended_hold")

    def test_opener_only_call_is_unresolved(self):
        self.assertEqual(server._compute_outcome(self.rec(TOBIN, answered_by="machine_start"))[0], "ivr_unresolved")

    def test_menu_looping_after_the_press_is_unresolved_not_voicemail(self):
        """Bylt: pressed 1, the menu replayed three times."""
        pre = "Press one for emergencies. Press two to leave a voice mail."
        rec = self.rec(pre + " " + BYLT_REPLAY + " " + BYLT_REPLAY, idx=len(pre), digits=["1"])
        self.assertEqual(server._compute_outcome(rec)[0], "ivr_unresolved")

    def test_a_hold_announcement_on_a_short_call_is_not_a_confirmed_hold(self):
        """Houzpital: hold language, call over after 30s."""
        t = "For emergency services, please stay on the line while we connect you. Please hold while we connect you."
        server.STORE.seconds_since_answered = lambda sid: 30.0
        self.assertEqual(server._compute_outcome(self.rec(t))[0], "ivr_unresolved")
        server.STORE.seconds_since_answered = lambda sid: 66.0
        self.assertEqual(server._compute_outcome(self.rec(t))[0], "extended_hold")

    def test_disconnected_number(self):
        self.assertEqual(server._compute_outcome(self.rec(PHOENIX))[0], "disconnected")


if __name__ == "__main__":
    unittest.main()


class AiReceptionistIsAnswered(unittest.TestCase):
    """Owner decision 2026-10-01: an AI receptionist picking up is an answer, not gatekeeping."""

    def test_lightning_of_sarasota(self):
        t = ("Hi. Thanks for calling Lightning Restoration, specializing in water and mold damage, "
             "This is the AI after hours receptionist. I'm going to ask you a few questions. "
             "But first, please state your full name and spell your last name for me.")
        self.assertFalse(ivr.looks_like_gatekeeping(t).is_gatekeeping)

    def test_a_plain_name_prompt_is_still_gatekeeping(self):
        t = "Please state your full name and spell your last name, and the reason for your call."
        self.assertTrue(ivr.looks_like_gatekeeping(t).is_gatekeeping)


class PressAnyKey(unittest.TestCase):
    """Reactic Restoration 2026-10-01: "press any key to be connected" x3, nothing pressed, logged voicemail."""
    T = ("Thank you for calling Reactic Restoration. Please press any key to be connected with one of our team "
         "members. Thank you for calling Reactic Restoration. Please press any key to be connected with one of our team members.")

    def test_it_is_a_menu_and_presses_one(self):
        self.assertTrue(ivr.looks_like_menu(self.T).is_menu)
        d = ivr.decide_digit(self.T)
        self.assertEqual((d.is_menu, d.digit), (True, "1"))

    def test_a_voicemail_box_is_not(self):
        self.assertIsNone(ivr.any_key_digit("Please leave a message at the tone. When you have finished recording, press any key to be connected to the operator."))
        self.assertIsNone(ivr.any_key_digit("Press any key to continue."))


class RingingIsNotAFinishedCall(unittest.TestCase):
    """2026-10-01: Longview / Doan / Beacon -- AMD "human", ~800 frames, no text, levels that
    pulse like ringback; we hung up at 18s. Levels below are the real traces from the notes."""
    LONGVIEW = [-78, -79, -25, -22, -25, -78, -78, -78, -25, -22, -25, -81, -90, -78, -24, -22, -26]
    DOAN = [-18, -12, -13, -78, -78, -78, -18, -12, -13, -78, -78, -78, -18, -12, -13, -78]
    BEACON = [-19, -12, -13, -78, -78, -78, -19, -12, -13, -78, -78, -78, -19, -12, -13]

    def test_the_three_real_traces_are_ringing(self):
        from app import media_stream
        for t in (self.LONGVIEW, self.DOAN, self.BEACON):
            self.assertTrue(media_stream.ringback_pattern(t), t)

    def test_other_audio_is_not(self):
        from app import media_stream
        f = media_stream.ringback_pattern
        self.assertFalse(f([]))
        self.assertFalse(f([-90] * 20))                                   # dead silence
        self.assertFalse(f([-25] * 20))                                   # a tone / music with no gaps
        self.assertFalse(f([-30, -35, -28, -40, -33, -31, -38, -29]))     # speech-like, no silent gaps
        self.assertFalse(f([-20, -20, -90, -90, -90, -20, -90, -20, -20, -20]))   # irregular cadence
        self.assertFalse(f([-20, -90, -90, -90]))                         # one burst only

    def test_still_ringing_at_the_cap_is_unknown_not_a_miss(self):
        rec = types.SimpleNamespace(
            call_sid="CAring", call_status="", answered_by="human", company_name="Test Co",
            gatekeeping_detected=False, alt_contact_detected=False, hit_time_cap=True, ivr_detected=False,
            digits_sent=[], transcript_accum="", transcript_at_last_digit=0, ring_seen=True)
        self.assertEqual(server._compute_outcome(rec)[0], "unknown")
        rec.ring_seen = False
        self.assertEqual(server._compute_outcome(rec)[0], "extended_hold")


class RingWaitWiring(unittest.TestCase):
    """_conclude_not_menu on an empty AMD-human call whose audio pulses like ringback."""

    def run_case(self, enabled):
        from app import media_stream
        from app.config import CFG
        sid = f"CAring{int(enabled)}"
        server.STORE.register(sid, "+15555550199", company_name="Ring Co")
        server.STORE.update(sid, answered_by="human")
        media_stream.get_buffer(sid).energy[:] = RingingIsNotAFinishedCall.DOAN
        resolved, old = [], (server._resolve, CFG.ring_wait_enabled, CFG.stream_transcription_enabled)
        server._resolve = lambda s: resolved.append(s)
        object.__setattr__(CFG, "ring_wait_enabled", enabled)
        object.__setattr__(CFG, "stream_transcription_enabled", True)
        try:
            with server.app.test_request_context("/x", method="POST"):
                resp = server._conclude_not_menu(sid, server.STORE.snapshot(sid))
                body = resp.get_data(as_text=True)
        finally:
            server._resolve = old[0]
            object.__setattr__(CFG, "ring_wait_enabled", old[1])
            object.__setattr__(CFG, "stream_transcription_enabled", old[2])
        return resolved, body, server.STORE.snapshot(sid)

    def test_shadow_mode_still_hangs_up_but_says_it_would_have_waited(self):
        resolved, body, rec = self.run_case(False)
        self.assertEqual(len(resolved), 1)
        self.assertIn("Hangup", body)
        self.assertTrue(rec.ring_seen)
        self.assertIn("shadow: would have kept waiting", rec.press_diag)

    def test_enabled_keeps_waiting_while_the_budget_allows(self):
        resolved, body, rec = self.run_case(True)
        self.assertEqual(resolved, [])
        self.assertNotIn("Hangup", body)
        self.assertIn("-- waiting", rec.press_diag)

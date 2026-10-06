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


class Night20261002(unittest.TestCase):
    """Real transcripts from the 2026-10-02 batch (menu text as heard at press time)."""

    def test_ready2_presses_the_damage_option_not_existing_customer(self):
        t = ("Thank you for calling. If you're calling about mold, water, or fire damage, please press one. "
             "If you're an existing customer, please press two. For all other inquiries, please press three.")
        self.assertEqual(ivr.quick_digit(t, require_complete=True)[0], "1")

    def test_oneteam_water_or_fire_loss(self):
        t = ("Thank you for calling OneTeam Restoration. If you are currently experiencing a water or fire loss in your home "
             "or business, press one. For mold remediation, press two. For roofing, press three.")
        self.assertEqual(ivr.quick_digit(t, require_complete=True)[0], "1")

    def test_classone_press_one_then_the_label_in_the_next_sentence(self):
        t = ("Thank you for contacting Class One Property Restoration. Press one. For twenty four hour emergency services. "
             "Press two for all other inquiries.")
        d = ivr.quick_digit(t, require_complete=True)
        self.assertEqual((d[0], d[2]), ("1", True))

    def test_r_and_s_dial_two_hundred(self):
        t = ("Hello. You've reached R and S Restores. For water damage emergencies, please dial two hundred. "
             "To dial by name, press nine.")
        d = ivr.quick_digit(t, require_complete=True)
        self.assertEqual((d[0], d[2]), ("200", True))
        self.assertEqual(ivr.parse_options("in an emergency dial 911. press one for sales.")[0][0], "1")   # 911 is not an option

    def test_options_that_must_not_be_picked_by_the_damage_rule(self):
        for t in (
            "Thank you for calling Rock Environmental. For asbestos services, press one. For demolition services, press two. "
            "For employment related queries, press three. For any other queries, press four.",
            "For choosing ACR. Voted number one in water damage restoration and air duct cleaning for nine years in a row. "
            "Press one for scheduling. Press two for administration and hours.",
            "If you have a billing question about a water damage claim, press one. For employment, press two.",
        ):
            self.assertIsNone(ivr.quick_digit(t, require_complete=True), t[:50])

    def test_emergency_words_still_win_over_the_damage_rule(self):
        t = "For water damage restoration, press one. If this is an emergency, press two."
        self.assertEqual(ivr.quick_digit(t, require_complete=True)[0], "2")

    def test_apex_hold_is_not_an_answer(self):
        t = "Hello. Thank you for calling Apex Restoration. Please hold. This call is being recorded."
        d = ivr.decide_tail(t, "human", "Apex Restoration And Mitigation")
        self.assertEqual((d.outcome, d.classifier), ("extended_hold", "rule"))

    def test_hold_then_something_is_not_pending(self):
        for t in ("Hold while I try to connect you. Yes. Hello? Hello? Hello?",
                  "Please hold while I try to connect you. Your call has been forwarded to the voice mail for National Water "
                  "Damage Restoration. No one is available to take your call.",
                  "Thank you for calling Westfair. How may I help you?"):
            self.assertIsNone(ivr.hold_pending(t), t[:40])

    def test_dry_ease_hold_is_not_screening_but_google_voice_screening_still_is(self):
        self.assertFalse(ivr.looks_like_alt_contact("Thank you for calling. TryEase. Please hold while I try to connect you.").is_alt_contact)
        self.assertTrue(ivr.looks_like_alt_contact("Hello. Please state your name after the tone, and Google Voice will try to connect you.").is_alt_contact)
        self.assertTrue(ivr.looks_like_alt_contact("Hi. If you record your name and reason for calling, I'll see if this person is available.").is_alt_contact)

    def test_an_emergency_number_with_the_verb_dropped_is_a_redirect(self):
        t = ("Thank you for calling Pro Restorations of North Georgia. Please leave a message after the tone. "
             "If this is an emergency, please seven seven zero three five four one six three nine. Thank you")
        self.assertTrue(ivr.looks_like_alt_contact(t).is_alt_contact)
        self.assertFalse(ivr.looks_like_alt_contact("If this is an emergency, hang up and dial nine one one.").is_alt_contact)

    def test_a_full_voicemail_box_is_never_navigated(self):
        t = ("Please leave your message for five seven one three four four three eight three seven Sorry. Mailbox is full. "
             "To send an SMS notification, press five. Or press the pound sign to continue.")
        self.assertFalse(ivr.looks_like_menu(t).is_menu)


class CapWithMenuReplaying(Outcomes):
    """One Team Restoration 2026-10-02: pressed 1 (twice), the menu then looped to the 60s cap."""

    def test_menu_replaying_at_the_cap_is_unresolved_not_a_hold(self):
        menu = ("Thank you for calling OneTeam Restoration. If you are currently experiencing a water or fire loss, press one. "
                "For mold remediation, press two. To repeat this menu, press six.")
        rec = self.rec(menu + " " + menu, idx=len(menu), digits=["1"])
        rec.hit_time_cap = True
        self.assertEqual(server._compute_outcome(rec)[0], "ivr_unresolved")

    def test_silence_at_the_cap_after_a_press_is_still_a_hold(self):
        t = "Thank you for calling Quality Cleaning. For fire and water damage emergency, press one. Your call is important to us."
        rec = self.rec(t, idx=len(t), digits=["1"])
        rec.hit_time_cap = True
        self.assertEqual(server._compute_outcome(rec)[0], "extended_hold")


class EarlyPressLeavesTheRestOfTheMenu(unittest.TestCase):
    def test_the_leftover_of_an_interrupted_menu_is_not_a_replay(self):
        # Quality Cleaning And Restoration 2026-10-02: pressed 1 mid-menu; what followed was the rest of that menu, then a hold
        self.assertIsNone(ivr.menu_replayed_after_press(" press two or Your call is important to us. We will be with you shortly."))


class SoleOptionMenus(unittest.TestCase):
    """2026-10-02: Dry Guy and Phoenix Flood And Fire hung up on us ~5s after a one-option prompt; we pressed at +11s/+17s."""

    def test_a_single_option_is_pressed_as_soon_as_its_sentence_is_complete(self):
        for t in ("Thank you for calling the Dry Guy Restoration. Press one for the Dry Guy Restoration.",
                  "Thank you for calling. Press one to continue to our main line. Thank you for calling. Press one to continue to our main line."):
            d = ivr.quick_digit(t, require_complete=True)
            self.assertEqual(d[0], "1", t[:40])

    def test_a_single_option_that_is_not_the_way_forward_is_left_alone(self):
        for t in ("Please press one to leave a message or dial the extension you are trying to reach.",
                  "To hear this message again, press one.",
                  "For billing questions, press one.",
                  "To repeat this menu, press one."):
            self.assertIsNone(ivr.quick_digit(t, require_complete=True), t[:40])


class Night20261005(unittest.TestCase):
    """Real transcripts from the 2026-10-05 UTC batch (140 calls)."""

    GOLD_STAR = ("Thank you for calling Gold Star Restoration. Please listen carefully as our menu options have changed. If you are calling "
                 "about a fire or water emergency, please hang up and call our emergency line at eight seven seven ninety five water. That is "
                 "eight seven seven nine five nine two eight three seven. If you know your party's extension, you may dial it at any time. "
                 "For a dial by name directory, dial one. To speak to someone in the office, dial two.")

    def test_4_sure_send_me_a_quick_text_is_an_alt_contact(self):
        t = ("Hello. You have reached Anthony Lemorgier. I'm currently on the phone right now assisting another customer. Please, for a "
             "fast response, send me a quick text. If not, give me about ten to fifteen minutes to call you right back.")
        self.assertTrue(ivr.looks_like_alt_contact(t).is_alt_contact)

    def test_gold_star_emergency_redirect_number_beats_the_menu(self):
        self.assertFalse(ivr.looks_like_menu(self.GOLD_STAR).is_menu)
        self.assertTrue(ivr.looks_like_alt_contact(self.GOLD_STAR).is_alt_contact)

    def test_a_menu_with_a_real_emergency_option_is_still_a_menu(self):
        t = "If this is an emergency, press one. Otherwise please call our office at five five five one two three four five six seven."
        self.assertTrue(ivr.looks_like_menu(t).is_menu)

    def test_zip_code_loop_and_solicitation_block_are_gates(self):
        self.assertTrue(ivr.looks_like_gatekeeping("Please enter the ZIP code where you need water, or fire damage restoration services. Sorry. That is not a valid ZIP code.").is_gatekeeping)
        self.assertTrue(ivr.looks_like_gatekeeping("Number does not accept solicitation calls. If you're a customer,").is_gatekeeping)

    def test_h2o_asking_the_caller_a_direct_question_is_an_agent(self):
        t = ("Thank you for calling h two o damage. Is this a fire, water, smoke, damage, or mold damage related emergency? "
             "Thank you for calling h two o damage. Is this a fire water damage, or mold damage related emergency?")
        self.assertIsNotNone(ivr.reactive_greeting(t))
        self.assertEqual(ivr.decide_tail(t, "machine_start", "H2O").outcome, "answered")
        self.assertIsNone(ivr.reactive_greeting("If this is an emergency, please call us back. Thank you for calling."))

    def test_lightning_ai_receptionist_is_answered(self):
        t = ("Hi. For calling Lightning Restoration. This is the AI after hours receptionist. But first, please state your full name "
             "and spell your last name for me. Hey. Are you still there?")
        d = ivr.decide_tail(t, "human", "Lightning Restoration")
        self.assertEqual((d.outcome, d.classifier), ("answered", "rule"))

    def test_carrier_messages_are_not_voicemail(self):
        for t in ("Sorry. Cannot connect your call at the moment. Please try again later.", "We are sorry. We are unable to complete. Your call is dialed."):
            self.assertEqual(ivr.decide_tail(t, "machine_start", "X").outcome, "unknown", t)

    def test_choice_mold_ring_then_a_voice_is_an_answer(self):
        d = ivr.decide_tail("Ground mold.", "human", "Choice Mold Removal", ring_seen=True)
        self.assertEqual(d.outcome, "answered")
        self.assertIsNone(ivr.ring_then_voice("You have reached Choice Mold. Please leave a message after the tone.", "human"))
        self.assertIsNone(ivr.ring_then_voice("Ground mold.", "machine_start"))

    def test_big_wave_a_person_after_an_early_press_is_not_a_menu_replay(self):
        self.assertIsNone(ivr.menu_replayed_after_press(" For billing, press two. For Big Wave Restoration. It's Paul."))
        self.assertTrue(ivr.menu_replayed_after_press("Thank you for calling Acme. Press one for sales. Press two for billing. Thank you for calling Acme."))

    def test_sir_clean_does_not_press_the_first_of_several_options(self):
        t = "Thank you for calling Circlean. This call will be recorded. For janitorial services, dial one. For fire and water"
        self.assertIsNone(ivr.quick_digit(t, require_complete=True))
        self.assertEqual(ivr.quick_digit("Thank you for calling Circlean. For janitorial services, dial one. For fire and water damage, dial two.", require_complete=True)[0], "2")

    def test_a_person_greeting_after_a_hold_is_not_swallowed_as_boilerplate(self):
        t = ("Please hold while we connect your call to extension one. This call will be recorded for quality and training Circling, good evening.")
        self.assertIsNone(ivr.hold_pending(t))
        self.assertIsNotNone(ivr.hold_pending("Hello. Thank you for calling Apex Restoration. Please hold. This call is being recorded."))

    def test_twm_voicemail_box_is_never_navigated(self):
        t = ("This call may be recorded for quality and training The person you are trying to reach is unavailable. Leave your message at the "
             "tone. Press pound when finished. To listen to your message, press one. To rerecord your message, press two.")
        self.assertFalse(ivr.looks_like_menu(t).is_menu)
        self.assertIsNone(ivr.quick_digit(t, require_complete=True))

    def test_the_sole_option_rule_never_picks_pound_or_star(self):
        self.assertIsNone(ivr.quick_digit("To continue, press pound.", require_complete=True))


class TriageThinMachine(Outcomes):
    def test_a_bare_greeting_heard_by_a_machine_detector_is_unknown_not_answered(self):
        rec = self.rec("Thank you for calling Triage. Property Restoration Specialist.", answered_by="machine_start", company="Triage")
        out, note = server._compute_outcome(rec)
        self.assertEqual(out, "unknown", note)


class Night20261006(Outcomes):
    """Real transcripts from the 2026-10-06 UTC batch (200 calls, the older manual_import list: answered 72%)."""

    def test_pm_leary_we_will_be_right_with_you_is_a_hold_not_a_voicemail(self):
        t = ("Thank you for calling PM Leary Restoration after hours emergency line. We'll be right with you. "
             "For quality assurance, your call is now being recorded.")
        self.assertIsNotNone(ivr.hold_pending(t))
        d = ivr.decide_tail(t, "machine_start", "PM Leary Restoration")
        self.assertEqual(d.outcome, "extended_hold")

    def test_triangle_a_name_after_the_hold_announcement_is_a_person(self):
        t = "This call will be recorded for quality purposes. For calling. After hours. Please hold for the next available agent. Triangle restoration."
        rec = self.rec(t, answered_by="machine_start", company="Triangle Restoration")
        out, note = server._compute_outcome(rec)
        self.assertEqual(out, "answered", note)

    def test_miller_a_recorded_greeting_with_no_person_is_not_an_answer_when_amd_heard_a_machine(self):
        t = "You have reached Miller Restoration. Call may be monitored and recorded for record keeping, training, and quality assurance purposes."
        rec = self.rec(t, answered_by="machine_start", company="Miller Restoration")
        out, note = server._compute_outcome(rec)
        self.assertEqual(out, "unknown", note)


class EchoedNameAfterOpening(unittest.TestCase):
    def test_a_clipped_name_after_the_disclosure_is_a_person(self):
        self.assertTrue(ivr.echoed_name_after_opening("Thank you for choosing water removal services. This call may be recorded. Water removal.", "Water Removal Services"))
        self.assertTrue(ivr.echoed_name_after_opening("This call will be recorded for quality. Elite Restoration.", "Elite Restoration Inc"))

    def test_an_opening_and_a_disclosure_with_nobody_after_is_not(self):
        self.assertFalse(ivr.echoed_name_after_opening("You have reached Miller Restoration. Call may be monitored and recorded for record keeping, training, and quality assurance purposes.", "Miller Restoration"))
        self.assertFalse(ivr.echoed_name_after_opening("Thank you for calling Triage. Property Restoration Specialist.", "Triage"))

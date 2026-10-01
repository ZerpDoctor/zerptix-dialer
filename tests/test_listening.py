"""Scripted-call tests for the listening rules in server.ivr_turn.

Each test drives the REAL /ivr/turn route with a fake far end: text and
"still speaking" state are put into the call's stream buffer exactly as the
Deepgram reader would, then a turn is fired and the TwiML reply inspected.
No network: Sheets writes are stubbed and every Anthropic call is forced to
fail so the deterministic keyword path decides (repeatable, no API cost).

Every case is a real incident from 2026-09-29 -- add new ones as they turn up;
`python -m unittest discover tests` must stay green before a push.
"""
from __future__ import annotations

import sys
import time
import types
import unittest

# The local venv (and CI) may not ship flask_sock's websocket stack; the
# routes under test do not use it.
try:  # pragma: no cover
    import flask_sock  # noqa: F401
except Exception:  # pragma: no cover
    _m = types.ModuleType("flask_sock")

    class _S:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k): return lambda f: f
    _m.Sock = _S
    sys.modules["flask_sock"] = _m

from app import ivr, media_stream, server  # noqa: E402
from app.config import CFG  # noqa: E402


def _unavailable(*a, **k):
    raise ivr.AnthropicUnavailable("tests force the keyword path")


class Call:
    """A fake far end for one call."""
    _n = 0

    def __init__(self, client, company="Test Restoration Co", answered_by="machine_start"):
        Call._n += 1
        self.client = client
        self.sid = f"CAtest{Call._n:05d}"
        server.STORE.register(self.sid, "+15555550100", company_name=company)
        server.STORE.update(self.sid, answered_by=answered_by)
        self.buf = media_stream.get_buffer(self.sid)
        self.buf.connected_at = time.time()

    # --- what the far end does -------------------------------------------
    def speak(self, text, *, interim=None):
        """Far end says `text` right now (finalized), optionally with more
        words still pending as an interim."""
        self.buf.note_activity()
        self.buf.append(text)
        if interim:
            self.buf.set_interim(interim)

    def go_quiet(self, seconds=5.0):
        self.buf.last_activity = time.time() - seconds
        self.buf.set_interim("")

    # --- what the dialer does ----------------------------------------------
    def turn(self, stage="menu", level=0):
        r = self.client.post(f"/ivr/turn/{stage}/{level}", data={"CallSid": self.sid, "To": "+15555550100"})
        return r.get_data(as_text=True)

    @property
    def rec(self):
        return server.STORE.get(self.sid)


def played(xml):
    import re
    m = re.search(r'<Play digits="([^"]+)"', xml)
    return m.group(1) if m else None


class ListeningBase(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "verify": server._verify, "resolve": server._resolve,
            "cp_save": server.call_checkpoint.save,
            "d": ivr.classify_ivr_digit, "a": ivr.classify_call_audio,
            "ac": ivr.classify_alt_contact, "g": ivr.classify_gatekeeping,
            "cfg": (CFG.stream_transcription_enabled, CFG.listen_gate_enabled),
        }
        server._verify = lambda req: True
        self.resolved = []
        server._resolve = lambda sid, hangup=True, recovered=False: self.resolved.append(sid)
        server.call_checkpoint.save = lambda rec: None
        for n in ("classify_ivr_digit", "classify_call_audio", "classify_alt_contact", "classify_gatekeeping"):
            setattr(ivr, n, _unavailable)
        CFG.stream_transcription_enabled = True
        CFG.listen_gate_enabled = True
        self.client = server.app.test_client()

    def tearDown(self):
        server._verify = self._orig["verify"]
        server._resolve = self._orig["resolve"]
        server.call_checkpoint.save = self._orig["cp_save"]
        ivr.classify_ivr_digit = self._orig["d"]
        ivr.classify_call_audio = self._orig["a"]
        ivr.classify_alt_contact = self._orig["ac"]
        ivr.classify_gatekeeping = self._orig["g"]
        CFG.stream_transcription_enabled, CFG.listen_gate_enabled = self._orig["cfg"]



class ListeningTests(ListeningBase):
    def test_does_not_press_while_prompt_is_still_being_spoken(self):
        """Greenville Restoration Services / Yeti: the digit went out while the
        far end was mid-prompt and the rest of the menu arrived as 'post-press'
        audio. Nothing may be pressed while the far end is still talking."""
        c = Call(self.client)
        c.speak("Thank you for calling Greenville. For sales press one. For service press two.",
                interim="For billing press three")
        xml = c.turn()
        self.assertIsNone(played(xml), "pressed a digit mid-prompt")
        self.assertIn("<Gather", xml, "should keep listening")
        self.assertEqual(c.rec.digits_sent, [])

    def test_presses_once_the_far_end_has_stopped_talking(self):
        c = Call(self.client)
        c.speak("Thank you for calling Greenville. If you have an emergency property damage, please press nine now.")
        c.speak("Otherwise, please try again during our usual opening hours.")
        c.go_quiet()
        c.turn()          # first quiet turn: growth gate wants one stable poll
        c.turn()
        self.assertEqual(c.rec.digits_sent[:1], ["9"])
        self.assertTrue(c.rec.emergency_route)

    def test_deferred_turn_is_not_counted_as_a_listen(self):
        c = Call(self.client)
        c.speak("Hi, this is Mike, how can I help you?")
        before = c.rec.gather_count
        c.turn()
        # add_turn bumps the count, the deferral must give it back
        self.assertEqual(c.rec.gather_count, before)
        self.assertEqual(c.rec.speaking_deferrals, 1)

    def test_deferral_is_bounded_so_continuous_speech_cannot_stall_forever(self):
        c = Call(self.client)
        for i in range(CFG.listen_max_defers + 1):
            c.speak(f"Thank you for your patience, announcement number {i}.")     # far end never stops
            c.turn()
        self.assertEqual(c.rec.speaking_deferrals, 0, "must have proceeded to decide after the cap")

    def test_no_early_answered_resolution_on_a_half_finished_greeting(self):
        """A greeting that reads as a person must not hang up the call while
        the far end is still talking (it may turn into 'please leave a message')."""
        c = Call(self.client)
        c.speak("Hi this is Mike how can I help you?")
        c.go_quiet(0.5)
        c.buf.set_interim("please leave your name and number")
        server.STORE.update(c.sid, gather_count=2)
        c.turn()
        self.assertEqual(self.resolved, [], "resolved while the far end was still speaking")

    def test_no_second_digit_after_the_emergency_option_was_pressed(self):
        """Rare Restoration (1,2), First Point (0,4), Rocky Mountain (9,1),
        Most Wanted (1,2): leftover text of the SAME menu was read as a
        second-level menu and a second digit pressed on top of the connection."""
        c = Call(self.client)
        c.speak("This call may be recorded. Press one now if you are experiencing a flood or fire "
                "for our twenty four hour emergency response line.")
        c.go_quiet()
        c.turn(); c.turn()
        self.assertEqual(c.rec.digits_sent[:1], ["1"])
        self.assertTrue(c.rec.emergency_route)
        # the rest of the same prompt keeps arriving after the press
        c.speak("Press two for mitigation customer service. Please hold while we connect your call to emergency one.")
        c.go_quiet()
        for lvl in (1, 1, 1):
            c.turn("menu", lvl)
        self.assertEqual(c.rec.digits_sent, ["1"], f"second digit pressed: {c.rec.digits_sent}")

    def test_press_timing_is_recorded_for_the_notes(self):
        c = Call(self.client)
        c.speak("For emergencies press one. For billing press two.")
        c.go_quiet()
        c.turn(); c.turn()
        self.assertIn("press 1", c.rec.press_diag)
        self.assertIn("far end quiet", c.rec.press_diag)


class PressTimingTests(ListeningBase):
    """Presses must land inside the phone system's input window (~3-5s after
    the prompt). 2026-09-30 franchise tests pressed 3-10s late and SERVPRO East
    Nashville answered "Invalid input" before our digit arrived."""

    MENU = ("Thank you for calling. Press one for sales. Press two for scheduling. "
            "Press zero for the operator.")

    def test_presses_on_the_first_turn_once_the_menu_is_over(self):
        c = Call(self.client)
        c.speak(self.MENU)
        c.go_quiet(2.5)                     # the prompt ended 2.5s ago
        c.turn()
        self.assertEqual(len(c.rec.digits_sent), 1, "no extra stability poll should be needed")

    def test_waits_while_the_silence_is_shorter_than_the_press_quiet(self):
        c = Call(self.client)
        c.speak(self.MENU)
        c.go_quiet(1.4)                     # past 'mid-word', not yet 'menu over'
        c.turn()
        self.assertEqual(c.rec.digits_sent, [])
        self.assertEqual(c.rec.press_waits, 1)
        c.go_quiet(2.6)
        c.turn()
        self.assertEqual(len(c.rec.digits_sent), 1)

    def test_a_looping_menu_is_pressed_without_waiting_for_a_long_silence(self):
        c = Call(self.client)
        c.speak("Press one for the next available customer service representative.")
        c.speak("Press one for the next available customer service representative.")
        c.buf.last_activity = time.time() - 0.4     # the loop restarts almost immediately
        c.turn()
        self.assertEqual(c.rec.digits_sent, ["1"])

    def test_the_press_wait_is_bounded(self):
        c = Call(self.client)
        for _ in range(CFG.listen_press_max_waits + 2):
            c.speak("Press one for sales.")
            c.buf.last_activity = time.time() - 1.4
            c.turn()
        self.assertTrue(c.rec.digits_sent, "must press eventually even if the line never gets quiet enough")


class QuickDigitTests(unittest.TestCase):
    def q(self, t):
        r = ivr.quick_digit(t)
        return None if r is None else r[0]

    def test_the_menu_names_its_emergency_option(self):
        self.assertEqual(self.q("Press one for sales. Press two for billing. Press three for emergency service."), "3")

    def test_the_only_option_connects_to_a_person(self):
        self.assertEqual(self.q("Please press one to be connected."), "1")                      # RestoreCo
        self.assertEqual(self.q("Welcome to Property Craft. Press one to speak to our representative."), "1")
        self.assertEqual(self.q("Press one for the next available customer service representative."), "1")  # 911

    def test_a_menu_that_has_already_looped_is_still_quick(self):
        """911 Restoration Dallas: after one full play, the option's label ran on into
        'this call may be recorded' and the word 'record' blocked the rule."""
        t = ("One one restoration. Call may be recorded for quality assurance. Press one for the next available "
             "customer service representative. Thank you. Thank you for calling nine one one Restoration. "
             "This call may be recorded for quality assurance. Press one for the next available customer "
             "service representative. Thank you.")
        self.assertEqual(self.q(t), "1")

    def test_a_spoken_seven_is_not_a_menu_option(self):
        """S And S Repair: 'available twenty four seven to assist you' parsed as option 7."""
        t = ("We are available twenty four seven to assist you. If this is an emergency and you require "
             "immediate assistance, please press one now to be connected to our emergency team.")
        self.assertEqual(self.q(t), "1")

    def test_ambiguous_menus_go_to_the_model(self):
        self.assertIsNone(self.q("Press one for sales. Press two for support. Press zero for the operator."))

    def test_voicemail_menus_and_message_options_are_never_quick(self):
        self.assertIsNone(self.q("Please leave a message. To review, press one. To re-record, press two."))
        self.assertIsNone(self.q("If this is an emergency, press two to leave an emergency message."))


class StreamRecoveryTests(ListeningBase):
    """2 of 11 franchise tests were blank because the media stream failed:
    'never connected', and '12 frames in a 48s call'."""

    def aged(self, c, seconds=10.0):
        server.STORE.update(c.sid, answered_at=time.time() - seconds)

    def test_a_stream_that_never_connected_is_restarted(self):
        c = Call(self.client)
        c.buf.connected_at = None
        self.aged(c)
        xml = c.turn()
        self.assertIn('<Stream name="rs1"', xml)
        self.assertEqual(c.rec.stream_restarts, 1)
        self.assertIn("stream restart #1", c.rec.press_diag)

    def test_a_stream_that_stops_sending_audio_is_restarted(self):
        c = Call(self.client)
        self.aged(c)
        c.buf.frames = 12
        c.turn()                                              # first sight of 12 frames
        server.STORE.update(c.sid, stream_frames_changed_at=time.time() - 7)
        xml = c.turn()                                        # still 12, unchanged for 7s
        self.assertIn('<Stream name="rs1"', xml)

    def test_a_healthy_stream_is_left_alone(self):
        c = Call(self.client)
        self.aged(c)
        c.buf.frames = 100
        c.turn()
        c.buf.frames = 300
        self.assertNotIn("<Stream", c.turn())

    def test_a_brand_new_call_is_given_time_to_connect(self):
        c = Call(self.client)
        c.buf.connected_at = None
        self.aged(c, 3.0)
        self.assertNotIn("<Stream", c.turn())

    def test_restarts_are_bounded(self):
        c = Call(self.client)
        c.buf.connected_at = None
        self.aged(c)
        for _ in range(CFG.stream_max_restarts + 3):
            c.turn()
        self.assertEqual(c.rec.stream_restarts, CFG.stream_max_restarts)


class EarlyPressTests(ListeningBase):
    """SERVPRO East Nashville: the emergency option is FIRST in a 26s menu that never goes
    quiet for 2s before the phone system times out and replays it. A clear pick whose
    sentence is complete must not wait for the rest of the menu."""

    def test_presses_the_emergency_option_while_the_rest_of_the_menu_is_still_playing(self):
        c = Call(self.client)
        c.speak("Please listen carefully to the following options. Press one for emergency services. "
                "Press two to speak with a member of our office staff regarding scheduling.")
        c.turn()                                  # the far end is speaking right now
        self.assertEqual(c.rec.digits_sent, ["1"], "should press as soon as the emergency sentence is complete")
        self.assertTrue(c.rec.emergency_route)

    def test_does_not_press_while_the_emergency_sentence_is_unfinished(self):
        c = Call(self.client)
        c.speak("Please listen carefully to the following options. Press one for emergency")
        c.turn()
        self.assertEqual(c.rec.digits_sent, [])

    def test_a_menu_without_a_clear_pick_still_waits_for_the_end(self):
        c = Call(self.client)
        c.speak("Press one for sales. Press two for support. Press three for billing.")
        c.turn()
        self.assertEqual(c.rec.digits_sent, [])


class OnePressForAPersonTests(ListeningBase):
    """Albany Environmental pressed 3 ("speak to a client representative") and then 8 ("name
    directory") from the leftover of the same menu."""

    def test_no_second_digit_after_pressing_the_option_that_reaches_a_person(self):
        c = Call(self.client)
        c.speak("If you know your party's extension, you may dial it at any time. To speak to a client "
                "representative now, please press three. For the name directory, please press eight.")
        c.go_quiet(2.5)
        c.turn(); c.turn()
        self.assertEqual(c.rec.digits_sent[:1], ["3"])
        self.assertTrue(c.rec.stop_navigating)
        c.speak("You could also email us. For the name directory, please press eight.")
        c.go_quiet(3.0)
        for lvl in (1, 1, 1):
            c.turn("menu", lvl)
        self.assertEqual(c.rec.digits_sent, ["3"], f"pressed again: {c.rec.digits_sent}")


class LongMenuTests(ListeningBase):
    def test_a_long_menu_is_not_pressed_before_it_is_over(self):
        """A 25-30s prompt must not make the gate fail open (the general cap is
        10 deferrals) and press an early option before the last one is heard."""
        c = Call(self.client)
        c.speak("Press one for sales. Press two for scheduling.")
        for i in range(CFG.listen_max_defers + 5):       # well past the general cap
            c.speak(f"Press {i + 3} for department {i}.")
            c.turn()
            self.assertEqual(c.rec.digits_sent, [], f"pressed too early on turn {i + 1}")

    def test_the_turn_trace_records_deferrals_waits_and_the_press(self):
        c = Call(self.client)
        c.speak("Press one for sales. Press two for scheduling. Press zero for the operator.")
        c.turn()                                   # speaking -> D
        c.go_quiet(1.5)
        c.turn()                                   # quiet but < press quiet -> W
        c.go_quiet(2.6)
        c.turn()                                   # press -> P1
        trace = c.rec.turn_trace
        self.assertIn("D@", trace)
        self.assertIn("W@", trace)
        self.assertRegex(trace, r"P\d@")


class EmergencyFlagTests(unittest.TestCase):
    def test_flag_follows_the_last_digit_not_any_digit(self):
        """Rare Restoration pressed 1 (emergency) then 2 (mitigation customer
        service) and the row still said routed_to_emergency_line=true."""
        sid = "CAflag00001"
        server.STORE.register(sid, "+15555550101")
        server.STORE.record_digit(sid, "1", "haiku", False, "emergency", is_emergency_route=True)
        self.assertTrue(server.STORE.get(sid).emergency_route)
        server.STORE.record_digit(sid, "2", "haiku", False, "mitigation", is_emergency_route=False)
        self.assertFalse(server.STORE.get(sid).emergency_route)


class HoldBudgetTests(unittest.TestCase):
    def rec(self, answered_at=1000.0, pressed_at=None):
        return types.SimpleNamespace(answered_at=answered_at, last_digit_at=pressed_at)

    def test_no_press_keeps_the_60s_master_timer(self):
        self.assertEqual(server._deadline_seconds(self.rec()), 60)

    def test_hold_budget_counts_from_the_last_press(self):
        # pressed 25s in -> 60s of hold -> 85s from answer (used to be 60 total)
        self.assertEqual(server._deadline_seconds(self.rec(pressed_at=1025.0)), 85)

    def test_early_press_never_shortens_below_the_master_timer(self):
        self.assertEqual(server._deadline_seconds(self.rec(pressed_at=1002.0)), 62)

    def test_absolute_ceiling(self):
        self.assertEqual(server._deadline_seconds(self.rec(pressed_at=1090.0)), CFG.ivr_hard_cap_seconds)
        self.assertLess(CFG.ivr_hard_cap_seconds, 150, "must stay under the scheduler's pool wait backstop")


if __name__ == "__main__":
    unittest.main()

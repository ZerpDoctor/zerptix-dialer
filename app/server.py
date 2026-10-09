"""Flask web server: Twilio webhooks for the outbound call loop + IVR navigation.

Endpoints:
  GET  /healthz                     -- liveness check
  POST /calls                       -- local trigger (used by `python -m app.dial`)
  POST /ivr/start                   -- TwiML served when the callee picks up
  POST /ivr/turn/<stage>/<level>    -- <Gather> speech results (the IVR state machine)
  POST /webhooks/amd                -- async Answering Machine Detection result
  POST /webhooks/status             -- call status callbacks
  POST /webhooks/recording          -- recording ready (only if RECORD_CALLS=true)

Every Twilio webhook is signature-verified. Outcome writes are idempotent on
CallSid so duplicate webhook delivery cannot double-log.

IVR flow (spec sections 3-4): on connect we <Gather> the initial audio and
accumulate the transcript across gather cycles. Only genuine end-of-speech
(empty result), a confirmed menu, or a hard cap concludes a turn -- a recording
disclosure that precedes the real menu instruction does not. A detected menu is
confirmed by Claude Haiku (app/anthropic_client.py); on any Anthropic failure we
log loudly and fall back to keyword-priority selection. Because Twilio's AMD
judges the *menu* audio, menu calls resolve from a post-navigation transcript
classification, not from AMD.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from flask import Flask, Response, request
from flask_sock import Sock
from twilio.request_validator import RequestValidator

from .config import CFG
from .call_store import STORE
from . import outcomes
from . import email_safety
from . import google_sheets
from . import ivr
from . import media_stream
from . import amd_checkpoint
from . import call_checkpoint
from .dialer import hang_up, place_call, get_answered_by, DialError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("dialer.server")

app = Flask(__name__)
sock = Sock(app)

# Both providers verify webhooks with the exact same HMAC-SHA1(secret, url +
# sorted-concatenated-form-params) scheme -- confirmed by reading SignalWire's
# own signature-validation reference alongside twilio.request_validator's
# source. Only the secret and the header name differ, so one validator class
# covers both; no SignalWire-specific signing library is needed.
_twilio_validator = RequestValidator(CFG.twilio_auth_token)
_signalwire_validator = RequestValidator(CFG.signalwire_signing_key)

_DECISIVE_AMD = ("human", "fax")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _verify(req) -> bool:
    is_twilio = CFG.telephony_provider == "twilio"
    validate_flag = CFG.twilio_validate_signature if is_twilio else CFG.signalwire_validate_signature
    if not validate_flag:
        log.warning("%s signature verification DISABLED", CFG.telephony_provider)
        return True
    header_name = "X-Twilio-Signature" if is_twilio else "X-SignalWire-Signature"
    validator = _twilio_validator if is_twilio else _signalwire_validator
    signature = req.headers.get(header_name, "")
    path = req.path
    if req.query_string:
        path = f"{path}?{req.query_string.decode()}"
    url = CFG.public_base_url.rstrip("/") + path
    ok = validator.validate(url, req.form.to_dict(), signature)
    if not ok:
        log.warning("Rejected webhook with bad signature: %s", req.path)
    return ok


def _maybe_rehydrate(call_sid: str) -> None:
    """Called at the top of every webhook that touches a call_sid, before any
    STORE lookup that would otherwise silently create a blank stub. If this
    process has no memory of call_sid but it's genuinely still in flight
    (not already resolved), reconstruct its full pre-restart state from
    call_checkpoint.py instead of starting from nothing -- see that module's
    docstring for the real incident (58% of 'unknown' rows) this closes.

    Order matters: call_sid_already_logged() against the real Calls tab is
    checked FIRST and is the only trusted source for "is this call already
    resolved" -- a checkpoint's own data is never trusted for that, since an
    already-resolved call's last checkpoint row could still be sitting there
    unpruned. Only rehydrates when genuinely unknown to this process AND not
    already logged; a no-op (and cheap: one in-memory dict lookup) for every
    normal call that never experiences a restart."""
    if STORE.get(call_sid) is not None:
        return
    try:
        if google_sheets.call_sid_already_logged(call_sid):
            return
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("call_sid_already_logged check failed during rehydrate for %s: %s", call_sid, e)
        return
    data = call_checkpoint.load(call_sid)
    if data:
        STORE.rehydrate(call_sid, data)
        log.info("Rehydrated call_sid=%s from checkpoint (gather=%s, ivr_detected=%s, digits=%s)",
                  call_sid, data.get("gather_count"), data.get("ivr_detected"), data.get("digits_sent"))


def _lookup_queue_row(phone: str):
    """Best-effort Queue-tab lookup by phone -- fallback path for calls that
    didn't come through the scheduler (e.g. inbound), which don't already have
    company identity attached to their CallRecord. Returns None on any
    failure -- never raises, this is display-only."""
    try:
        from .queue_backend import SheetQueue

        return SheetQueue(CFG.sched_queue_tab).find_by_phone(phone)
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("Queue lookup failed for %s: %s", phone, e)
        return None


def _format_local(now_utc: datetime, tz_name: str) -> tuple[str, str]:
    """(date, time) in the called company's own local timezone, falling back
    to UTC if there's no valid timezone. Split into separate cells so email
    mail-merge can reference just the time without the date. Time is bare (no
    tz abbreviation) since it's already in the recipient's own local time --
    adding e.g. "EDT" would be redundant for them. Never raises -- this is a
    display convenience, not something call resolution depends on."""
    try:
        from .timezones import parse_tz

        tz = parse_tz(tz_name) if tz_name else None
        if tz is not None:
            local = now_utc.astimezone(tz)
            return local.strftime("%b %d, %Y"), local.strftime("%I:%M %p")
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("logged_at_local formatting failed: %s", e)
    return now_utc.strftime("%b %d, %Y"), now_utc.strftime("%I:%M %p UTC")


def _twiml(body: str) -> Response:
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        mimetype="text/xml",
    )


def _gather(stage: str, level: int, timeout: int) -> str:
    action = CFG.callback_url(f"ivr/turn/{stage}/{level}")
    # STREAM_TRANSCRIPTION_ENABLED: drop the speech recognition SignalWire
    # would otherwise bill for on this verb -- the Media Stream (started
    # once, in /ivr/start) is providing the transcript instead. Gather still
    # does its original job unmodified: DTMF capture and the same
    # timeout/actionOnEmptyResult turn-cadence every call site here already
    # relies on.
    input_modes = "dtmf" if CFG.stream_transcription_enabled else "speech"
    speech_attrs = (
        "" if CFG.stream_transcription_enabled
        else f'speechTimeout="auto" speechModel="{CFG.ivr_speech_model}" language="en-US" '
    )
    return (
        f'<Gather input="{input_modes}" {speech_attrs}'
        f'timeout="{timeout}" actionOnEmptyResult="true" '
        f'method="POST" action="{action}"/>'
    )


@sock.route("/media-stream/<call_sid>")
def media_stream_ws(ws, call_sid: str) -> None:
    """SignalWire connects to this as a WebSocket when a call's TwiML
    includes <Start><Stream .../></Start> (only added when
    STREAM_TRANSCRIPTION_ENABLED). Runs for the call's lifetime on its own
    gunicorn gthread worker thread; see media_stream.py for the actual
    SignalWire<->Deepgram relay.

    Logged as its own checkpoint (2026-09-25 diagnostic): some real calls
    show zero media_stream.py log output at all -- no connect, no error,
    nothing -- meaning SignalWire never actually reached this route, as
    distinct from reaching it and failing inside handle_signalwire_stream.
    This line is the only way to tell those two failure shapes apart."""
    log.info("media_stream_ws route HIT for call_sid=%s -- WebSocket connection accepted", call_sid)
    try:
        media_stream.handle_signalwire_stream(ws, call_sid)
    except Exception as e:  # noqa: BLE001 -- a WS handler exception must not affect the call itself
        log.error("media_stream_ws route CRASHED for call_sid=%s: %s", call_sid, e, exc_info=True)


def _trace(call_sid: str, rec, code: str, buf=None) -> None:
    """Append one compact entry to this call's turn trace: code@seconds-in/far-end-quiet.
    Written to the Calls notes for any call that pressed a digit, so press timing
    can be read from the Sheet instead of inferred."""
    if len(rec.turn_trace) > 700:
        return
    q = buf.quiet_for() if buf is not None else None
    entry = f"{code}@{STORE.seconds_since_answered(call_sid):.0f}" + ("" if q is None else f"/q{q:.1f}")
    STORE.update(call_sid, turn_trace=(rec.turn_trace + " " + entry).strip())


def _stream_problem(call_sid: str, rec, buf) -> str | None:
    """Why this call's media stream needs restarting, or None if it looks fine.
    Never connected, or stopped sending audio. Real blank-transcript calls
    2026-09-30: SERVPRO Charlotte ("stream: never connected") and Rainbow Far
    North Dallas ("connected, 12 audio frames" in a 48s call)."""
    age = STORE.seconds_since_answered(call_sid)
    now = time.time()
    if buf.frames != rec.stream_frames_seen:
        STORE.update(call_sid, stream_frames_seen=buf.frames, stream_frames_changed_at=now)
        changed = True
    else:
        changed = False
    if age < 7 or rec.stream_restarts >= CFG.stream_max_restarts:
        return None
    if buf.error and "not set" in buf.error:
        return None                      # a config problem; restarting cannot help
    if buf.connected_at is None:
        return "the stream never connected"
    if not changed:
        since = now - (rec.stream_frames_changed_at or now)
        if since >= 6.0:
            return f"audio stopped arriving after {buf.frames} frames"
    return None


def _deadline_seconds(rec) -> float:
    """Seconds after answer at which a call's wait ends.

    The 60s master timer used to cover navigation AND hold together, so time
    spent in a menu came straight out of the wait for a person: a call that
    pressed a digit at 25s only got ~35s of hold. The hold budget now counts
    from the LAST digit press (60s), with the old master timer as the floor
    and CFG.ivr_hard_cap_seconds as the absolute ceiling. A call with no
    press is unchanged (60s from answer)."""
    floor = float(CFG.ivr_master_timeout_seconds)
    pressed = getattr(rec, "last_digit_at", None)
    answered = getattr(rec, "answered_at", None)
    if pressed and answered:
        floor = max(floor, (pressed - answered) + CFG.ivr_hold_budget_seconds)
    return min(floor, float(CFG.ivr_hard_cap_seconds))


def _budget_exhausted(call_sid: str, rec, margin: float = 0.0) -> bool:
    return STORE.seconds_since_answered(call_sid) >= _deadline_seconds(rec) - margin


def _amd_pause_seconds(call_sid: str) -> int:
    rec = STORE.get(call_sid)
    remaining = _deadline_seconds(rec) - STORE.seconds_since_answered(call_sid)
    return max(3, min(int(remaining), 30))


# --------------------------------------------------------------------------- #
# outcome resolution
# --------------------------------------------------------------------------- #

def _write_sheet_with_retry(row: dict, attempts: int = 4) -> None:
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            google_sheets.append_result(row)
            return
        except Exception as e:  # noqa: BLE001
            if i == attempts:
                log.error(
                    "Sheet write FAILED after %d attempts for call_sid=%s. Row=%s | error: %s",
                    attempts, row.get("call_sid"), row, e,
                )
                raise
            log.warning("Sheet write attempt %d failed (%s); retrying in %.1fs", i, e, delay)
            time.sleep(delay)
            delay *= 2


def _tail_of(rec) -> str:
    """The transcript that counts as this call's outcome audio: everything
    after the last digit press, minus the rest of the menu prompt still
    arriving at press time (see ivr.post_press_tail). A call with no press
    uses the whole transcript, unchanged."""
    full = rec.transcript_accum
    if rec.ivr_detected:
        if rec.digits_sent:
            return ivr.post_press_tail(full, rec.transcript_at_last_digit)
        return full[rec.transcript_at_last_digit:].strip()
    return full.strip()


def _compute_outcome(rec) -> tuple[str, str]:
    """Return (outcome, note). The transcript is the primary signal for what
    happened on ANY connected call -- not just menu calls -- with AMD only as
    a fallback when there's no usable transcript. This generalizes the
    original menu-only design ("AMD judges the menu audio, not the outcome")
    to close a real gap: non-menu calls used to trust AMD alone and never
    looked at the transcript they'd already captured, which produced real
    false "voicemail" outcomes for calls a live human actually answered
    (confirmed 2026-09-14: AMD's fast Enable-mode "machine_start" is not
    reliable enough to trust blindly). Call-status events short-circuit both
    paths, since those come from the telephony layer, not audio content."""
    cs = (rec.call_status or "").lower()
    by_status = outcomes.from_call_status(cs)
    if by_status:
        if cs in ("failed", "canceled"):
            return by_status, f"carrier reported the call {cs} before it connected (not a confirmed dead number; it will be retried)"
        return by_status, ""

    # Recover a real AMD verdict this process's in-memory copy is missing.
    # Two different failure modes, two fallbacks, in order of how cheap/
    # likely they are: (1) lost to a mid-call restart -- see
    # app/amd_checkpoint.py -- recoverable from our own durable checkpoint;
    # (2) the AsyncAmdStatusCallback webhook itself never arrived at all
    # (real incident 2026-09-28, 911 Restoration test call: SignalWire had
    # computed 'machine_start' on their side, confirmed via their own Call
    # resource, but no webhook, no restart, no rejected-signature log line --
    # it simply never showed up). A checkpoint can't recover a signal no
    # process ever received, so the second fallback asks the provider
    # directly instead of only ever waiting for it to be pushed to us.
    # recovery_source feeds the row note below so it's accurate about which
    # (if either) fallback actually fired -- both only run on this already-
    # rare blank-answered_by path, never on a normal call.
    answered_by = (rec.answered_by or "").strip()
    recovery_source = None
    if not answered_by:
        answered_by = amd_checkpoint.lookup(rec.call_sid)
        if answered_by:
            recovery_source = "checkpoint after a restart"
    if not answered_by:
        answered_by = get_answered_by(rec.call_sid)
        if answered_by:
            recovery_source = "a direct SignalWire lookup (webhook never arrived)"

    if ivr.promo_recording(rec.transcript_accum):
        return "unknown", "an unrelated promotional recording answered (wrong number); nothing here is a claim about the company"

    if rec.gatekeeping_detected:
        # reasoning goes into the row via rec.gatekeeping_reasoning in
        # _build_row (same pattern as rec.ivr_reasoning), not duplicated here.
        return "gatekeeping_miss", ""

    if rec.alt_contact_detected:
        # reasoning goes into the row via rec.alt_contact_reasoning in
        # _build_row, not duplicated here.
        return "alt_miss", ""

    if rec.hit_time_cap:
        # A transcript captured before the timer expired is real evidence and
        # outranks the blunt "ran out of time" fallback -- confirmed real
        # incident: Dry Source Property Restoration correctly pressed the
        # emergency digit, the tail clearly captured voicemail language
        # ("please leave your name number..."), but hitting the 60s cap right
        # after discarded all of that and returned extended_hold without ever
        # looking at what was actually heard.
        cap_transcript = _tail_of(rec)
        cap_note = ""
        if ivr.emergency_redirect_number(rec.transcript_accum):
            # Graystone Restoration 2026-10-07: rang 32s, then "if this is an after hours emergency, please hang up and call
            # eight one three seven three four..." (cut off) -- we waited out the budget and logged a hold.
            return "alt_miss", "the greeting gives an emergency number to call (heard before the budget ran out)"
        if cap_transcript:
            decision = ivr.decide_tail(cap_transcript, answered_by, rec.company_name, ring_seen=getattr(rec, "ring_seen", False))
            # amd_fallback means neither a real keyword match NOR a confident
            # Haiku read found anything -- decide_tail is then trusting AMD's
            # fast-mode verdict alone, which this codebase already found
            # unreliable enough that the transcript became the primary signal
            # in the first place (see decide_tail's own docstring). Doing
            # exactly that -- confidently trusting AMD -- specifically when
            # the call ALSO ran the full 60s with nothing more said is the
            # one place that unreliable signal was still deciding the
            # outcome on its own. Real incident 2026-09-21: Bluestone
            # Environmental and Clean Usa Water Mold And Fire Restoration
            # both captured only a generic opener ("your call is very
            # important to us" / "call will be recorded"), then real silence
            # for the rest of a 34-95s call, and got a confident 'voicemail'
            # from AMD alone -- extended_hold ("silence after ..., likely on
            # hold") is the honest description of what actually happened,
            # not a guessed answered/voicemail. Real keyword/Haiku evidence
            # (the Dry Source Property Restoration case above) still wins
            # outright -- only the pure-AMD-guess path is downgraded.
            # mentions_incoming_menu guard added 2026-09-22 alongside the
            # same guard in _conclude_not_menu: a transcript announcing a
            # menu that never actually arrived (e.g. "please choose from 1
            # of the following options" with nothing after it) shouldn't be
            # confidently resolved from the surrounding framing alone, even
            # via a real (non-amd_fallback) read -- we genuinely don't know
            # what was in that menu, possibly including an emergency option.
            if (decision.outcome != "unknown" and decision.classifier != "amd_fallback"
                    and not ivr.mentions_incoming_menu(cap_transcript)):
                note = f"tail: {decision.reasoning}" if decision.reasoning else ""
                return decision.outcome, note
            # Not confident enough to override the timer -- outcome stays
            # extended_hold, but keep WHY (e.g. "anthropic unavailable"), which
            # this line used to discard: 2026-09-29's credit outage left
            # Restore Pros and Exceptional Restoration as extended_hold with
            # a note that said nothing about the classifier having been down.
            if decision.reasoning:
                cap_note = f"; last tail read: {decision.reasoning}"
        # The digit went out and all that came back, to the end of the budget, was the menu playing
        # again: nobody picked up and nothing was held. One Team Restoration 2026-10-02 (pressed 1 twice,
        # the menu looped for 114s) was logged extended_hold -- the same rule the non-cap path applies.
        if rec.digits_sent:
            _raw_cap = rec.transcript_accum[rec.transcript_at_last_digit:]
            _replay = ivr.menu_replayed_after_press(_raw_cap) or ivr.menu_recording_only(_raw_cap)
            # An early press leaves the REST of the interrupted menu in the post-press text ("...press
            # two or Your call is important to us" -- Quality Cleaning, a real hold). Only a menu that
            # played again in full -- several options, or the same option twice -- is a replay.
            _opts = {m.group("d1") for m in ivr._OPT_ANCHOR.finditer(_raw_cap.lower()) if m.group("d1")}
            if _replay and not ivr.queue_or_callback(_raw_cap) and (len(_opts) >= 2 or ivr.menu_repeats(_raw_cap)):
                return "ivr_unresolved", _replay + "; check recording"
        if getattr(rec, "ring_seen", False) and not cap_transcript.strip() and not rec.digits_sent:
            return "unknown", "still ringing when the budget ran out (ring pattern, no text) -- nobody picked up; not a miss claim"
        return "extended_hold", "hit 60s master timer with no resolution" + cap_note

    if rec.ivr_detected and not rec.digits_sent:
        return "ivr_unresolved", "menu detected but no digit could be determined; check recording"

    transcript = _tail_of(rec)

    if not transcript:
        if rec.ivr_detected:
            # extended_hold means we waited out the budget (~60s) and nobody
            # came. A call that ended well short of that with nothing heard
            # after the press was hung up on by the far end (or dropped) --
            # we don't know why, so it is unresolved, not a hold. 12 of the
            # last 20 rows logged this way ended in 17-49s: RestoreCo (19s),
            # Complete Restoration LLC (29s), Property Craft (30s) on
            # 2026-09-29. 0.0 means the answered-time is unknown (a rehydrated
            # record), where the old behavior is kept.
            elapsed = STORE.seconds_since_answered(rec.call_sid)
            _pressed = getattr(rec, "last_digit_at", None)
            held = (time.time() - _pressed) if _pressed else elapsed
            if 0.0 < elapsed and held < CFG.ivr_hold_budget_seconds - 10:
                return "ivr_unresolved", (f"call ended {int(held)}s after the digit press with nothing heard "
                                          "(not a hold -- the hold budget was not used); check recording")
            return "extended_hold", "silence after menu navigation (likely on hold / in queue)"
        # AMD alone on a connected call with literally nothing transcribed is
        # the same pure-guess pattern already downgraded at every other
        # resolution site tonight (2026-09-22) -- AMD's fast-mode verdict
        # isn't reliable enough to trust blindly, and here there isn't even a
        # thin transcript to weigh it against. Real incident: Columbus
        # Restoration, Dry Patrol Akron, Hydrodry Restoration, and Choice
        # Mold Removal all resolved 'answered' purely on answered_by='human'
        # with zero transcript captured -- no way to actually verify any of
        # them, exactly the "single unverified signal" this codebase stopped
        # trusting everywhere else.
        if answered_by:
            tag = f" (recovered via {recovery_source})" if recovery_source else ""
            return "unknown", f"call completed with AMD={answered_by!r}{tag} but nothing was transcribed; check recording"
        if cs == "completed":
            return "unknown", "call completed; AMD gave no usable result"
        return "unknown", ""

    # An AI receptionist that introduces itself AND offers help counts as an answer wherever in the call it spoke --
    # not only in the text after the last digit (Rock Environmental, 2026-10-06: it picked up between our two
    # presses, so the post-press text was only its "I didn't catch that" apologies).
    _whole = rec.transcript_accum.lower()
    if (ivr._AI_RECEPTIONIST.search(_whole) and ivr._OFFER_HELP.search(_whole)
            and not any(p in _whole for p in ivr._STRONG_VOICEMAIL_GREETING)):
        return "answered", "tail: an AI receptionist answered (owner policy: counts as answered)"

    if rec.digits_sent:
        # After a digit press, if all that came back is the menu being read out
        # (options, no person after them) nobody has picked up. Judged on the
        # RAW post-press slice -- _tail_of() has already stripped leading menu
        # sentences. Real incident 2026-09-29: BoneDry Services logged answered
        # from "...press five for billing. Thank you for calling Bone Dry
        # Services."
        _raw_post = rec.transcript_accum[rec.transcript_at_last_digit:]
        if "zip code" in _raw_post.lower() and ivr.looks_like_gatekeeping(_raw_post).is_gatekeeping:
            return "gatekeeping_miss", "tail: after the press the line demands a ZIP code before connecting"
        _queue = ivr.queue_or_callback(_raw_post)
        _mro = None if _queue else (ivr.menu_replayed_after_press(_raw_post) or ivr.menu_recording_only(_raw_post))
        if _mro:
            return "ivr_unresolved", f"tail: {_mro}"
        if _queue:
            # Pressed, and what came back is the company's call queue (VIP Restoration: "higher than normal call
            # volume ... press one for a callback"). Nobody answered; it is a hold, once the hold budget was used.
            _el = STORE.seconds_since_answered(rec.call_sid)
            if 0.0 < _el < CFG.ivr_hold_budget_seconds - 10:
                return "ivr_unresolved", f"tail: {_queue}; the call ended after {int(_el)}s, before the hold budget was used"
            return "extended_hold", f"tail: {_queue}"

    decision = ivr.decide_tail(transcript, answered_by, rec.company_name, ring_seen=getattr(rec, "ring_seen", False))
    note = f"tail: {decision.reasoning}" if decision.reasoning else ""
    # A greeting that hands the caller an emergency NUMBER, or a call-screening prompt, with nobody having answered, is an
    # alternative-contact miss -- decided by rule, not left to a model that said no (MGM Recovery, Delaware County,
    # Environmental Resources, Liberty Restoration, 2026-10-07).
    if decision.outcome != "answered":
        _why = ("the greeting gives an emergency number to call" if ivr.emergency_redirect_number(rec.transcript_accum)
                else "a call-screening prompt (no person answered)" if ivr.call_screening(rec.transcript_accum) else "")
        if _why:
            return "alt_miss", f"tail: {_why}" + (f" ({decision.reasoning})" if decision.reasoning else "")
    # Triage 2026-10-05: AMD heard a machine and all we ever got was "Thank you for calling Triage. Property
    # Restoration Specialist." -- the start of a greeting or menu, not an answer. Keyword-only, thin, machine = unknown.
    # (Deliberate rules -- a person's name after a hold announcement, an AI receptionist -- are classifier "rule"
    # and are NOT second-guessed here: Triangle Restoration, 2026-10-06, was turned unknown by this guard.)
    if (decision.outcome == "answered" and decision.classifier != "rule" and (answered_by or "").startswith("machine")
            and ivr.thin_answer_reason(transcript) and not ivr.echoed_name_after_opening(transcript, rec.company_name)):
        return "unknown", f"thin: only a greeting/business name was heard and AMD heard a machine ({decision.reasoning})"
    # classifier == "amd_fallback" gets the same honest-unknown treatment as
    # a genuine "unknown" verdict, added 2026-09-22 alongside the same fix
    # in hit_time_cap and _conclude_not_menu above: this is the third and
    # final place a bare AMD guess (Haiku low-confidence or no keyword
    # match, nothing but AMD's already-established-unreliable fast-mode
    # verdict) was being trusted as a confident final outcome instead of
    # honestly flagged as inconclusive. Real keyword/Haiku evidence is
    # unaffected -- only the pure-guess path changes.
    # mentions_incoming_menu guard added 2026-09-22 alongside the same guard
    # at the other two resolution sites: a transcript announcing a menu that
    # never actually arrived shouldn't be trusted even via a real read --
    # see ivr.mentions_incoming_menu's docstring for the real incident.
    # A bare greeting and then the line RINGING is a transfer nobody has picked up, whatever AMD said (Aeret, Royal
    # Restoration 2026-10-06: "Thank you for calling." + 3-on/3-off ringback until we hung up).
    if decision.outcome == "answered" and decision.classifier != "rule" and ivr.thin_answer_reason(transcript):
        _rb = media_stream.peek_buffer(rec.call_sid)
        if _rb is not None and media_stream.ringing_after_greeting(list(_rb.energy)):
            return "unknown", f"thin: only a greeting was heard, then the line rang (a transfer nobody picked up) ({decision.reasoning})"
    confident = (decision.outcome != "unknown" and decision.classifier != "amd_fallback"
                 and not ivr.mentions_incoming_menu(transcript))
    # A genuine, confirmed LIVE pickup always outranks anything below -- a
    # redirect or gatekeeping demand mentioned earlier in the same call
    # doesn't matter if a real person actually answered anyway. A confident
    # "voicemail" does NOT block this, deliberately -- alt_miss is a more
    # specific refinement of "didn't reach a live person" (a voicemail that
    # happens to also give a redirect), not a competing outcome, so it should
    # still be allowed to upgrade a plain voicemail read. Real incident: Fire
    # Water Pros' voicemail read correctly as "voicemail" on its own, but the
    # personal-redirect it also contained ("contact the office manager...at
    # his cell") makes alt_miss the more useful, specific label.
    resolved = confident and decision.outcome == "answered"

    if not resolved and not rec.gatekeeping_detected and not rec.alt_contact_detected:
        # Real incident 2026-09-28: Fire Water Pros' voicemail was a personal
        # out-of-office redirect ("contact the office manager, Lucas
        # Riegelman, at his cell...") that should be alt_miss, not plain
        # voicemail; Ferris Water Damage Restoration's zip-code gatekeeping
        # demand ("enter your ZIP code after the beep") arrived in the TAIL
        # stage, post-press -- a stage that never runs gatekeeping/alt_contact
        # checks at all (see _ivr_tail, which only calls classify_tail()).
        # Menus already get a retroactive check (below) for the same "the
        # live turn that would have caught this never got the chance" gap;
        # alt_contact/gatekeeping never did, added 2026-09-29 for symmetry.
        # Same cheap keyword pre-filter before the real (Anthropic) check as
        # the live version in ivr_turn, so this doesn't add API cost for the
        # common case where neither pattern is even present.
        if ivr.looks_like_alt_contact(transcript).is_alt_contact:
            ac = ivr.decide_alt_contact(transcript)
            if ac.is_alt_contact and not ac.has_digit_option:
                return "alt_miss", f"alt_contact detected in final transcript but call ended before this was caught live: {ac.reasoning}"
        if ivr.looks_like_gatekeeping(transcript).is_gatekeeping:
            gk = ivr.decide_gatekeeping(transcript)
            if gk.is_gatekeeping and not gk.has_digit_option:
                return "gatekeeping_miss", f"gatekeeping detected in final transcript but call ended before this was caught live: {gk.reasoning}"

    if decision.classifier == "menu_start" and not rec.ivr_detected:
        # Only the start of an automated greeting/menu was ever heard -- see
        # ivr.menu_start_reason. Inconclusive, but not "unknown nothing".
        return "ivr_unresolved", note

    if not confident and not rec.ivr_detected and ivr.parse_options(transcript):
        # Real incident 2026-09-28 (Paul Davis Restoration): the far end hung
        # up right after playing its full menu, before any /ivr/turn ever got
        # a stable, complete segment to run decide_digit on -- transcript_accum
        # ended up with the whole menu but rec.ivr_detected stayed False.
        # Fixed 2026-09-29: this check originally ran BEFORE decide_tail and
        # used looks_like_menu() -- a loose phrase-scorer (matches generic
        # words like "to reach", "remain on the line") -- which discarded a
        # genuinely confident decide_tail() answer sitting later in the same
        # transcript. Real incidents: Restoration Doctor ("...This is Paula.
        # How may I help you?" -- a real live pickup -- got thrown away
        # because the transcript's OPENING also said "remain on the line");
        # Crystal Restoration Services (no real menu exists at all -- a hold
        # message with a personal-cell alt-contact redirect -- still tripped
        # the loose phrase score). Now only fires when decide_tail ISN'T
        # already confident, and requires parse_options() to find a REAL
        # "press N" structure, not a loose phrase match.
        return "ivr_unresolved", "menu detected in final transcript but call ended before navigation could run; check recording"

    if not confident:
        if rec.ivr_detected:
            return "ivr_unresolved", note or "post-menu audio inconclusive; check recording"
        return "unknown", note or "call completed; audio inconclusive"
    if decision.outcome == "extended_hold":
        # extended_hold means we waited out the hold budget and nobody came, which
        # locks the company out for the quarter. A call that ended well short of it
        # (Houzpital, 2026-10-01: a hold announcement, call over after 30s) has not
        # shown that. 0.0 means the call length is unknown, where the verdict stands.
        elapsed = STORE.seconds_since_answered(rec.call_sid)
        if 0.0 < elapsed < CFG.ivr_hold_budget_seconds - 10:
            return "ivr_unresolved", (note + "; " if note else "") + (
                f"hold announcement heard, but the call ended after {int(elapsed)}s, before the hold budget was used")
    return decision.outcome, note


def _build_row(rec, outcome: str, note: str) -> dict:
    notes = [n for n in (note, rec.resolve_note) if n]
    if rec.ivr_reasoning:
        notes.append(f"ivr: {rec.ivr_reasoning}")
    if rec.gatekeeping_reasoning:
        notes.append(f"gatekeeping: {rec.gatekeeping_reasoning}")
    if rec.alt_contact_reasoning:
        notes.append(f"alt_contact: {rec.alt_contact_reasoning}")
    if rec.press_diag:
        notes.append(f"diag: {rec.press_diag}")
    if rec.turn_trace and rec.digits_sent:
        notes.append(f"trace: {rec.turn_trace}")
    # extended_hold included (AllPro Restoration & Janitorial 2026-10-06: pressed 1, then 60s of nothing -- ringing,
    # silence or hold music cannot be told apart without the audio levels).
    if outcome in ("unknown", "ivr_unresolved", "extended_hold") or not rec.transcript_accum.strip():
        _sbuf = media_stream.peek_buffer(rec.call_sid)
        if _sbuf is not None:
            notes.append(_sbuf.stats_note(rec.answered_at))
    if outcome == "answered":
        _thin = ivr.thin_answer_reason(_tail_of(rec))
        if _thin:
            notes.append(f"evidence: thin ({_thin})")
            # Audio levels for thin answers too, so a greeting followed by silence (a person waiting) can be
            # told from one followed by hold music or a menu we never heard (Total Care / Miller, 2026-10-06).
            _tb = media_stream.peek_buffer(rec.call_sid)
            if _tb is not None and _tb.energy_note():
                notes.append(_tb.energy_note())
    if CFG.test_mode:
        notes.append("TEST_MODE")
    now_utc = datetime.now(timezone.utc)
    phone = rec.to_number or request.form.get("To", "")
    # Prefer identity captured at dial time (scheduler calls already know this
    # from the Queue row they just read to decide to dial) over a fresh Sheets
    # lookup by phone -- avoids a redundant read per call during a batch, which
    # was blowing the Sheets API read quota under burst load (2026-09-20).
    if rec.company_name:
        company_name, tz_name = rec.company_name, rec.company_timezone
    else:
        queue_row = _lookup_queue_row(phone)
        company_name = queue_row.company_name if queue_row is not None else ""
        tz_name = queue_row.timezone if queue_row is not None else ""
    logged_at_date, logged_at_time = _format_local(now_utc, tz_name)
    row = {
        "company_name": company_name,
        "logged_at_iso": now_utc.isoformat(),
        "logged_at_date": logged_at_date,
        "logged_at_time": logged_at_time,
        "phone_e164": phone,
        "outcome": outcome,
        "answered_by": rec.answered_by or "",
        "twilio_call_status": rec.call_status or "",
        "call_sid": rec.call_sid,
        "duration_sec": request.form.get("CallDuration", "") or rec.call_duration,
        "recording_url": rec.recording_url or "",
        "notes": "; ".join(notes),
        "ivr_detected": "yes" if rec.ivr_detected else "no",
        "ivr_levels": rec.menu_levels,
        "digits_sent": ",".join(rec.digits_sent),
        "ivr_fallback_flagged": "yes" if rec.ivr_fallback_flagged else "no",
        "classifier": rec.classifier,
        "ivr_transcript": rec.transcript_accum[:5000],
        "from_number": rec.from_number or request.form.get("From", ""),
        "needs_ai_copy": "yes" if outcome == "alt_miss" else "no",
        "routed_to_emergency_line": (
            "true" if rec.emergency_route
            else "false" if rec.ivr_detected
            else "not_applicable"
        ),    }
    row["email_safe"], row["email_safe_reason"] = email_safety.assess(row)
    return row


def _update_queue_row(rec, outcome: str) -> None:
    """If this call's number belongs to a company in the Queue tab, write the
    outcome + quarter status back to that row (spec section 7), under the
    per-phone write lock.

    Retries on transient failure -- same backoff shape as record_attempt's
    own retry and _write_sheet_with_retry. Real incident 2026-09-25, found
    via live testing: Alpha Restoration's Queue row never got today's
    outcome at all (last_outcome/last_updated_at/call_sid_history all still
    showed a call from two days earlier) even though the Calls tab logged
    the real result correctly -- this call site had a bare try/except that
    silently gave up on the first failure with no retry and no
    reconciliation, the exact same unsafe shape record_attempt's own retry
    was already added to fix. A burst of rapid test calls is exactly the
    kind of transient-Sheets-load window that trips this."""
    from .queue_backend import SheetQueue
    from . import queue_writer

    q = SheetQueue(CFG.sched_queue_tab)
    delay = 1.0
    for i in range(1, 5):
        try:
            fields = queue_writer.apply_outcome(
                q, rec.to_number, outcome,
                call_sid=rec.call_sid,
                recording_url=rec.recording_url or "",
                ivr_flagged=rec.ivr_fallback_flagged,
                heal_missed_attempt=rec.is_scheduled_attempt,
                do_not_call=ivr.refuses_solicitation(rec.transcript_accum or ""),
            )
            if fields is None:
                return  # not a queued company
            log.info("Queue write-back: %s -> %s", rec.to_number,
                     fields.get("this_quarter_status", "in_progress (unchanged)"))
            return
        except Exception as e:  # noqa: BLE001
            if i == 4:
                log.error(
                    "Queue write-back FAILED after 4 attempts for %s (call_sid=%s) -- "
                    "outcome logged to Calls tab but Queue row NOT updated: %s",
                    rec.to_number, rec.call_sid, e,
                )
                return
            log.warning("Queue write-back attempt %d failed for %s (%s); retrying in %.1fs",
                        i, rec.to_number, e, delay)
            time.sleep(delay)
            delay *= 2


def _resolve(call_sid: str, hangup: bool = True, recovered: bool = False) -> None:
    """Confirmed real incident: a plain human pickup took ~25s to hang up --
    all spent on a real Anthropic call plus 2-3 Google Sheets round-trips
    (Calls append, Queue read, Queue write) happening BEFORE hang_up(),
    while the caller sat on a connected, silent line the whole time.
    hang_up() needs nothing from that work -- just the call_sid -- so it now
    fires first, immediately after the idempotency check. This does not
    change WHEN a call is resolved (that logic is untouched); it only
    changes what happens mechanically once resolution has already been
    decided.

    `recovered=True` means the caller detected this process had no in-memory
    record for call_sid before its own upsert just created one -- i.e. this
    is very likely a restart-recovered stub, not a call this process has any
    real history for. Confirmed real incident 2026-09-17/18: a redeploy mid-
    batch wiped in-memory state; SignalWire's terminal status callback for
    calls already resolved just before the restart then landed on the fresh
    process and got logged a second time, blank, with a phantom Queue
    attempt. When recovered, check the Sheet (durable across restarts)
    before trusting a blank record."""
    if not STORE.mark_logged(call_sid):
        log.info("call_sid=%s already logged; ignoring duplicate", call_sid)
        return
    if recovered:
        try:
            already_in_sheet = google_sheets.call_sid_already_logged(call_sid)
        except Exception as e:  # noqa: BLE001 - never block resolution on this check
            log.warning("Duplicate-check against Sheet failed for %s: %s; proceeding", call_sid, e)
            already_in_sheet = False
        if already_in_sheet:
            log.warning(
                "call_sid=%s already in Sheet after process restart; "
                "skipping duplicate log/Queue update", call_sid,
            )
            if hangup:
                hang_up(call_sid)
            return
    if hangup:
        hang_up(call_sid)
    if CFG.stream_transcription_enabled:
        # Real incident 2026-09-25: Mays answered, Deepgram correctly
        # captured a real greeting plus three "Hello?"s (a person reacting
        # to silence, textbook answered), but the call ended in 18s and
        # never once completed a full Gather cycle -- DTMF-only Gather has
        # nothing to end a turn early on besides an actual digit press, so
        # with no digits pressed it just runs its full fixed timeout, and a
        # call that hangs up before that timeout elapses never fires a
        # single /ivr/turn. The transcript was sitting in the buffer the
        # whole time, correctly captured, just never read by anything. Only
        # backfill when transcript_accum is still empty -- a call that DID
        # complete at least one real turn (menu navigation, a tail read)
        # already has its transcript built the normal way; this is purely
        # for the case where that never got the chance to happen at all.
        _sb = media_stream.peek_buffer(call_sid)
        if _sb is not None:
            # A call the far end hung up: let the stream handler finish flushing Deepgram first (the last seconds of
            # speech are still being transcribed when the status callback lands).
            if not hangup and _sb.connected_at is not None and _sb.error is None:
                _sb.flushed.wait(3.0)
            # Everything the buffer holds that no turn has read yet. Turns only read every few seconds, so the last
            # utterance before a hang-up -- often the menu itself -- was never in the transcript (Pro Services,
            # 2026-10-06: the call ended as the menu began). When no turn ever completed this is the whole call.
            _unread = _sb.text_since_last_read()
            if _unread.strip():
                pre = STORE.get(call_sid)
                if pre is not None:
                    log.info("Merging unread stream text into the transcript for call_sid=%s: %r", call_sid, _unread[:200])
                    STORE.update(call_sid, transcript_accum=((pre.transcript_accum + " ") if pre.transcript_accum else "") + _unread.strip())
    # snapshot, not get(): _compute_outcome() and _build_row() both read rec's
    # fields, and a concurrent webhook thread mutating the live record in
    # between the two would decide the outcome from one instant and build the
    # notes from another -- see CallStore.snapshot's docstring for the real
    # incident this fixes.
    # Who ended the call. hangup=True means WE sent the hangup; False means
    # the call was already over when we got here (the far end hung up or the
    # network dropped it). Without this there is no way to tell, from the
    # Sheet, a call we cut short from one the other side ended -- the
    # question raised by 7 calls on 2026-09-29 that ended 19-49s after a
    # digit press with nothing heard afterwards.
    STORE.update(call_sid, resolve_note=("ended by: us (we hung up)" if hangup
                                         else "ended by: far end / network (call already over)"))
    rec = STORE.snapshot(call_sid)
    outcome, note = _compute_outcome(rec)
    _row = _build_row(rec, outcome, note)
    _write_sheet_with_retry(_row)
    # The terminal status callback may have arrived while the row was being built.
    _late = STORE.get(call_sid)
    if (_late is not None and not _row.get("duration_sec") and _late.call_duration
            and (_late.call_status or "").lower() in _TERMINAL_CALL_STATUSES):
        try:
            google_sheets.backfill_call_status(call_sid, _late.call_status, _late.call_duration)
        except Exception as e:  # noqa: BLE001 - best effort only
            log.warning("Late status backfill failed for %s: %s", call_sid, e)
    STORE.update(call_sid, phase="resolved")
    log.info(
        "LOGGED call_sid=%s outcome=%s number=%s ivr=%s digits=%s classifier=%s flagged=%s",
        call_sid, outcome, rec.to_number, rec.ivr_detected,
        ",".join(rec.digits_sent) or "-", rec.classifier, rec.ivr_fallback_flagged,
    )
    _update_queue_row(rec, outcome)
    media_stream.drop_buffer(call_sid)  # no-op if this call never had one


# --------------------------------------------------------------------------- #
# basic endpoints
# --------------------------------------------------------------------------- #

@app.get("/healthz")
def healthz() -> Response:
    return Response("ok", mimetype="text/plain")


@app.get("/calls/<call_sid>/status")
def call_status(call_sid: str):
    """Cheap in-memory status check, for the scheduler's bounded-concurrency
    dial pool (2026-09-21) -- lets the worker process (a separate container
    from this one on Railway) know when a call it placed has resolved,
    without polling the Sheet (already hit its read-quota limit more than
    once tonight) and without waiting on a fixed guessed delay. A call_sid
    this process has no record of (STORE.get returns None) is reported
    resolved=True -- happens after a restart, or once enough time has passed
    that it's not worth this process's memory; either way the pool shouldn't
    treat an unknown call_sid as a stuck slot forever."""
    rec = STORE.get(call_sid)
    if rec is None:
        return {"resolved": True, "known": False}, 200
    return {"resolved": rec.logged, "known": True}, 200


@app.get("/calls/inflight_count")
def calls_inflight_count():
    """Global, single-source-of-truth count of calls placed and not yet
    resolved, across every destination -- real incident 2026-09-21: the
    scheduler's dial pool tracked its own local list of in-flight call_sids
    in the worker process's memory, capped at 5, but a genuine interval-
    overlap analysis of a live batch showed 10 calls truly concurrent at
    peak. Traced to a worker process restart resetting that local list to
    empty -- the new process assumed 0 in flight and dialed 5 more while
    the previous process's 5 were still resolving here. The pool now asks
    for this count directly instead of tracking it itself, which is immune
    to that: a fresh worker process gets the real current number, not an
    assumed zero."""
    return {"count": STORE.inflight_count()}, 200


@app.post("/calls")
def place_call_endpoint():
    data = request.get_json(silent=True) or request.form
    to_number = (data.get("to_number") or "").strip()
    record_attempt = str(data.get("record_attempt", "")).lower() in ("1", "true", "yes")
    # Opt-in per-call recording override for a manual test call only (e.g. to
    # verify DTMF actually transmits) -- distinct from record_attempt above
    # (whether to write a Queue attempt). None/unset never changes behavior
    # for a real scheduler call, which never sends this field at all.
    record_call = None
    if "record_call" in data:
        record_call = str(data.get("record_call")).lower() in ("1", "true", "yes")
    window = (data.get("window") or "").strip()
    local_date_iso = (data.get("local_date") or "").strip()
    # Which pool number to place this call from -- set by the scheduler's
    # round-robin distribution; blank for a manual/one-off app.dial call, which
    # falls back to CFG.signalwire_from_number inside place_call().
    from_number = (data.get("from_number") or "").strip() or None

    if not to_number.startswith("+"):
        return {"error": "to_number must be E.164 (start with +)"}, 400
    if CFG.test_mode and to_number not in CFG.test_allowlist:
        return {"error": f"TEST_MODE=true and {to_number} is not in TEST_ALLOWLIST"}, 403

    if not record_attempt:
        # manual / simulated call -- no Queue interaction
        if STORE.has_inflight_to(to_number):
            return {"error": f"a call to {to_number} is already in flight"}, 409
        try:
            call_sid = place_call(to_number, from_number=from_number, record=record_call)
        except DialError as e:
            log.error("Call placement failed for %s: %s", to_number, e)
            return {"error": str(e)}, 502
        used_from = from_number or CFG.signalwire_from_number
        STORE.register(call_sid, to_number, from_number=used_from)
        return {"call_sid": call_sid, "to": to_number, "from_number": used_from}, 201

    # Scheduler call: hold the per-phone Queue write lock across the whole
    # place-then-record so two ticks can't double-dial or double-count.
    from datetime import date as _date, datetime as _dt, timezone as _tz
    from .queue_backend import SheetQueue
    from . import queue_writer

    q = SheetQueue(CFG.sched_queue_tab)
    with queue_writer.lock_for(to_number):
        row = q.find_by_phone(to_number)
        if row is not None and local_date_iso and row.last_call_date == local_date_iso:
            return {"skipped": "already attempted today"}, 409
        if STORE.has_inflight_to(to_number):
            return {"skipped": f"a call to {to_number} is already in flight"}, 409
        try:
            call_sid = place_call(to_number, from_number=from_number)
        except DialError as e:
            log.error("Call placement failed for %s: %s", to_number, e)
            return {"error": str(e)}, 502
        used_from = from_number or CFG.signalwire_from_number
        STORE.register(call_sid, to_number, from_number=used_from,
                        company_name=row.company_name if row is not None else "",
                        company_timezone=row.timezone if row is not None else "",
                        is_scheduled_attempt=True)

        fields = None
        if row is not None:
            try:
                ld = _date.fromisoformat(local_date_iso) if local_date_iso else _dt.now(_tz.utc).date()
            except ValueError:
                ld = _dt.now(_tz.utc).date()
            # place_call() above already happened -- irreversible, a real
            # phone rang. record_attempt() is what stops the NEXT scheduler
            # tick from redialing this same number, so a transient failure
            # here (still inside the per-phone lock, so no concurrent tick
            # can race the retries) must not be allowed to silently drop the
            # Queue write. Real incident 2026-09-22: Emergency Restoration
            # Solutions, a real prospect, got dialed 3 times in 4 minutes --
            # 3 distinct call_sids but current_quarter_attempts only ever
            # reached 1, meaning 2 of the 3 record_attempt calls never wrote
            # through, leaving last_call_date unset and the row looking
            # still-eligible on the next tick. Retrying (same backoff shape
            # as _write_sheet_with_retry) closes that window.
            delay = 1.0
            for i in range(1, 5):
                try:
                    fields = queue_writer.record_attempt(
                        q, to_number, call_sid=call_sid, window=window,
                        local_date=ld, now_utc=_dt.now(_tz.utc),
                    )
                    break
                except Exception as e:  # noqa: BLE001
                    if i == 4:
                        log.error(
                            "record_attempt FAILED after 4 attempts for %s (call_sid=%s already "
                            "placed) -- Queue row NOT marked attempted, risk of redial: %s",
                            to_number, call_sid, e,
                        )
                        break
                    log.warning("record_attempt attempt %d failed for %s (%s); retrying in %.1fs",
                                i, to_number, e, delay)
                    time.sleep(delay)
                    delay *= 2
    log.info("Scheduler call placed %s -> %s from=%s (attempt=%s)", call_sid, to_number, used_from,
             fields.get("current_quarter_attempts") if fields else "not a queued number")
    return {"call_sid": call_sid, "to": to_number, "from_number": used_from,
            "attempt_recorded": fields is not None}, 201


# --------------------------------------------------------------------------- #
# IVR flow
# --------------------------------------------------------------------------- #

@app.post("/ivr/start")
def ivr_start() -> Response:
    if not _verify(request):
        return Response("invalid signature", status=403)
    call_sid = request.form.get("CallSid", "")
    STORE.update(call_sid, to_number=request.form.get("To") or None)
    STORE.ensure_answered(call_sid)
    log.info("Call connected call_sid=%s ivr_enabled=%s", call_sid, CFG.ivr_enabled)

    if not CFG.ivr_enabled:
        return _twiml(f'<Pause length="{CFG.amd_wait_seconds}"/><Hangup/>')
    if CFG.stream_diagnostic_no_gather:
        # ONE-OFF DIAGNOSTIC (see config.py) -- Stream only, zero Gather, to
        # isolate whether Speech Recognition billing is coming from Stream
        # itself or from Gather even in dtmf-only mode.
        stream_url = CFG.stream_url(f"media-stream/{call_sid}")
        log.info("DIAGNOSTIC call_sid=%s: Stream-only, no Gather at all", call_sid)
        return _twiml(f'<Start><Stream url="{stream_url}"/></Start><Pause length="25"/><Hangup/>')
    stream = ""
    if CFG.stream_transcription_enabled:
        # <Start> is asynchronous -- per SignalWire's own docs it "continues
        # with the next cXML instruction at once" -- so this runs alongside
        # the Gather below for the whole call, not instead of it.
        stream_url = CFG.stream_url(f"media-stream/{call_sid}")
        stream = f'<Start><Stream url="{stream_url}"/></Start>'
        log.info("Requesting Media Stream for call_sid=%s at %s", call_sid, stream_url)
    return _twiml(stream + _gather("menu", 0, CFG.ivr_initial_timeout_seconds))


@app.post("/ivr/turn/<stage>/<int:level>")
def ivr_turn(stage: str, level: int) -> Response:
    if not _verify(request):
        return Response("invalid signature", status=403)

    call_sid = request.form.get("CallSid", "")
    _maybe_rehydrate(call_sid)
    if CFG.stream_transcription_enabled:
        # Gather is DTMF-only in this mode (see _gather) -- SpeechResult is
        # never populated, the buffer media_stream.py fills from Deepgram is
        # the real source now. text_since_last_read() returns only what
        # arrived since the PREVIOUS turn, the same per-turn shape
        # SpeechResult already had. A fresh (rehydrated) process's own
        # stream buffer only has NEW speech since ITS stream connected --
        # rec.transcript_accum was just restored with everything before
        # that, and add_turn() below appends onto it, so continuity holds.
        speech = media_stream.get_buffer(call_sid).text_since_last_read()
    else:
        speech = (request.form.get("SpeechResult") or "").strip()
    STORE.update(call_sid, to_number=request.form.get("To") or None)
    STORE.ensure_answered(call_sid)
    rec = STORE.add_turn(call_sid, speech)
    call_checkpoint.save(rec)
    log.info(
        "IVR turn call_sid=%s stage=%s level=%s gather=%d speech=%r",
        call_sid, stage, level, rec.gather_count, speech[:200],
    )

    # 1. Master timer -- 60s from answer, extended to 60s of hold counted from
    # the last digit press (hard-capped), see _deadline_seconds.
    if _budget_exhausted(call_sid, rec):
        STORE.update(call_sid, hit_time_cap=True)
        _resolve(call_sid)
        return _twiml("<Hangup/>")

    # 1a. Recover a dead media stream. A call whose stream never connected or
    # stopped sending audio produces a blank transcript no matter what the far
    # end says. A new <Start><Stream> on this turn's reply opens a fresh one.
    if CFG.stream_transcription_enabled and CFG.stream_restart_enabled:
        _sbuf = media_stream.get_buffer(call_sid)
        _problem = _stream_problem(call_sid, rec, _sbuf)
        if _problem:
            n = rec.stream_restarts + 1
            log.warning("Restarting media stream #%d call_sid=%s: %s", n, call_sid, _problem)
            STORE.update(call_sid, stream_restarts=n, stream_frames_seen=-1, stream_frames_changed_at=None,
                         gather_count=max(0, rec.gather_count - 1),
                         press_diag=(rec.press_diag + "; " if rec.press_diag else "")
                                    + f"stream restart #{n} at +{STORE.seconds_since_answered(call_sid):.0f}s ({_problem})")
            _url = CFG.stream_url(f"media-stream/{call_sid}")
            return _twiml(f'<Start><Stream name="rs{n}" url="{_url}"/></Start>'
                          + _gather(stage, level, CFG.listen_recheck_seconds))

    # 1b. Never decide while the far end is still mid-speech. Everything below
    # -- pressing a digit, concluding "not a menu", the early answered/voicemail
    # resolutions, gatekeeping/alt-contact hangups, the post-press tail -- acts
    # on a transcript that is still being spoken if this turn lands mid-prompt
    # (the fixed ~4s poll has no relationship to where a sentence ends). That
    # is the root of the cut-off transcripts, mid-prompt digit presses and
    # leftover-menu-as-outcome bugs. The text heard so far is already stored
    # (add_turn above), so deferring loses nothing; it is bounded by
    # listen_max_defers in a row and by the master timer above.
    if CFG.stream_transcription_enabled and CFG.listen_gate_enabled:
        _buf = media_stream.get_buffer(call_sid)
        # A menu that has already been read through twice is complete and
        # looping (911 Restoration repeated "press one for the next available
        # representative" for ~27s before we pressed): nothing left to wait for.
        _seg = rec.transcript_accum[rec.transcript_at_last_digit:]
        _looping = stage == "menu" and ivr.menu_repeats(_seg)
        # A clear pick whose sentence is already complete -- the menu names its
        # emergency option, or its only option connects to a person -- does not
        # depend on anything the menu says later, so there is nothing to wait
        # for. SERVPRO East Nashville's 26s menu never went quiet for 2s before
        # it timed out ("Invalid input") and replayed; we pressed at +39s and
        # +52s. Its FIRST option is the emergency one.
        _early = stage == "menu" and not rec.digits_sent and ivr.quick_digit(_seg, require_complete=True) is not None
        _looping = _looping or _early
        _cap = CFG.listen_menu_max_defers if (stage == "menu" and ivr.parse_options(_seg)) else CFG.listen_max_defers
        if (_buf.is_speaking(CFG.listen_quiet_seconds) and not _looping
                and rec.speaking_deferrals < _cap):
            _trace(call_sid, rec, "D", _buf)
            # A deferred turn is not a "listen": several early-resolution rules
            # count turns ("heard at least 2 turns before trusting answered"),
            # so a 2s deferral must not satisfy them on a greeting that has not
            # finished.
            STORE.update(call_sid, speaking_deferrals=rec.speaking_deferrals + 1,
                         gather_count=max(0, rec.gather_count - 1))
            log.info("IVR turn deferred (far end still speaking) call_sid=%s stage=%s defers=%d",
                     call_sid, stage, rec.speaking_deferrals + 1)
            return _twiml(_gather(stage, level, CFG.listen_recheck_seconds))
        if rec.speaking_deferrals:
            STORE.update(call_sid, speaking_deferrals=0)

    # NOTE: there used to be a step 2 here -- "AMD said human, no menu yet,
    # no digits pressed -> trust it and resolve immediately." Removed
    # entirely 2026-09-17: it fired on turn 1 or 2 of ANY call, before a
    # longer scripted greeting (marketing copy, hours, THEN "press 9 for...")
    # had a real chance to reveal its menu. A gather_count>=2 threshold was
    # tried first and still resolved too early (confirmed live against
    # Railway: hung up on turn 2 of a 6-turn test, nowhere near the digit
    # instruction on turn 6). _conclude_not_menu() below already resolves
    # correctly on a genuinely empty turn or the gather-cycle cap using the
    # exact same AMD verdict -- that is the right point for this decision,
    # not an early guess based on a partial fragment. A live human is barely
    # slower this way: their own silence naturally produces an empty turn
    # almost immediately, which resolves the same way, just at the correct
    # moment instead of a preemptive one.

    empty = speech == ""
    if empty and CFG.stream_transcription_enabled:
        buf = media_stream.get_buffer(call_sid)
        if buf.has_interim():
            # Deepgram is still mid-recognizing a real utterance -- not silence,
            # just not finalized yet. Treat this turn as "keep listening"
            # instead of letting the empty-turn fast path below conclude and
            # hang up on a technically-empty-but-actually-in-progress transcript.
            # Real incident 2026-09-28: Paul Davis Restoration's actual greeting
            # ("Thank you for calling the Paul Davis Rest...") was cut off
            # mid-word by exactly this race -- AMD said 'human', the finalized
            # transcript was empty, and _conclude_not_menu's fast path hung up
            # right as Deepgram was still transcribing real words it had already
            # heard. Still bounded by the gather-cycle cap and master timer
            # below either way -- this can't wait forever.
            empty = False
        elif buf.error is None and buf.connected_at is None:
            # SignalWire's own WebSocket hasn't even reached us yet -- distinct
            # from "connected but Deepgram is slow" below. A fresh WS/TLS
            # handshake under concurrent dialing load can plausibly take
            # longer than one extra cycle to complete, and waiting for it
            # costs nothing (still bounded by the exact same gather-cycle cap
            # and master timer every other path already relies on -- this
            # can't wait forever either). Deliberately NOT capped to
            # gather_count==1 like the branch below: as long as the stream
            # still hasn't connected, there's no new information to act on by
            # giving up sooner, and the outer bounds already stop it if it
            # never connects at all for the whole call. Explicitly excludes
            # buf.error (a real, already-known failure -- e.g. missing
            # DEEPGRAM_API_KEY or the WS connect itself throwing) since
            # waiting longer for a connection we already know failed can't
            # help; that case still resolves exactly as before this fix.
            empty = False
        elif rec.gather_count == 1 and buf.error is None and not buf.full_text().strip():
            # has_interim() above can't catch this variant -- there's no
            # interim to see because the pipeline hasn't produced ANYTHING
            # yet, not even a partial. Real incident 2026-09-28: a full-night
            # audit found 15/90 calls that same batch resolving "unknown,
            # nothing transcribed" within ~11-14s; checkpoint traces (not
            # just the Sheet's summary row) showed transcript_accum was
            # LITERALLY EMPTY -- zero characters -- at this exact first turn,
            # for calls at every point across an 11-minute, 5-way-concurrent
            # dialing burst. Most likely explanation: the SignalWire ->
            # media-stream-websocket -> Deepgram handshake chain hadn't
            # finished by the 5s initial gather window under that load, not
            # 15 different callers all staying silent. Gated strictly to
            # gather_count==1 (only the very first turn of the whole call --
            # this counter never resets per navigation level, so a real
            # empty turn later in the call is never affected) and skipped
            # entirely if the stream already reported a hard failure
            # (buf.error set, e.g. DEEPGRAM_API_KEY missing or the WS
            # connect itself failed) -- waiting longer can't help there, so
            # that case resolves exactly as before. A genuinely silent human
            # on turn 1 still resolves via the unchanged gather-cap/
            # master-timer paths below, just one ivr_tail_gather_seconds
            # cycle (~4s) later than before this fix.
            empty = False

    if stage == "tail":
        return _ivr_tail(call_sid, rec)

    # stage == "menu" -- only consider speech heard since the last digit press,
    # so an earlier menu's text doesn't re-trigger navigation at the next level.
    segment = rec.transcript_accum[rec.transcript_at_last_digit:]

    # Wait for one full poll cycle with no new content before evaluating this
    # segment for a menu/gatekeeping/alt-contact decision. Real incident
    # 2026-09-28 (live test against PuroClean's real IVR, right after
    # shortening ivr_tail_gather_seconds to react faster): deciding on a
    # still-playing announcement's early fragment (correctly pressing 1 for
    # emergency), then reacting again next cycle to the SAME recording's
    # later sentences -- many real IVR systems keep playing their full script
    # to completion regardless of DTMF input, no barge-in -- read that
    # residual leftover audio as a genuine second-level menu and pressed a
    # second, wrong digit (3, "franchise owner") that was never actually
    # offered in response to anything we did. Only costs one extra
    # ivr_tail_gather_seconds (~4s) in the common case, not the old fixed
    # wait this was built to remove. Doesn't touch the confident-answered
    # fast-paths below -- those react to whatever's heard each turn by
    # design and don't commit to a digit, so they aren't vulnerable to this.
    grew = len(segment) > rec.segment_len_at_last_check
    STORE.update(call_sid, segment_len_at_last_check=len(segment))
    if CFG.stream_transcription_enabled and CFG.listen_gate_enabled:
        # The listening gate above already established that the far end is not
        # mid-speech; waiting a further whole poll cycle (~4s) for "no new
        # text" only made digits arrive after the phone system's input window
        # had closed. Pressing still waits for a longer quiet (below).
        grew = False

    if grew and level == 0:
        # Cap consecutive deferrals, but ONLY at level 0 (before any digit
        # has been pressed yet on this call). Real incident 2026-09-28 (911
        # Restoration): a menu that LOOPS its own announcement (repeats when
        # nothing registers, with only a brief gap between loops) never gave
        # this gate a real pause to evaluate on -- it deferred for all 8
        # turns/66s of the call, so decide_digit() never ran even once,
        # despite a clear "press one for..." option being heard four
        # separate times. Forcing evaluation here doesn't risk a wrong/early
        # press -- decide_digit()'s own confidence/grounding checks already
        # correctly decline and fall through to "keep listening" if the
        # segment genuinely isn't decidable yet, same as before this gate
        # existed at all. Deliberately NOT applied for level >= 1 (after a
        # real digit press): that's exactly where the PuroClean incident
        # this gate was built for lives -- a no-barge-in system keeps
        # playing its OWN remaining script after a press, and forcing early
        # evaluation there would risk reading that leftover audio as a
        # genuine next-level menu again, the original bug.
        consecutive = rec.consecutive_growth_turns + 1
        if consecutive >= 2:
            grew = False
            STORE.update(call_sid, consecutive_growth_turns=0)
        else:
            STORE.update(call_sid, consecutive_growth_turns=consecutive)
    else:
        STORE.update(call_sid, consecutive_growth_turns=0)

    menu_look = None
    gatekeeping = None
    alt_contact = None
    if not grew:
        menu_look = ivr.looks_like_menu(segment)

        # 3. Digital-gatekeeping check -- only ever consulted when the menu
        # keyword gate already said this is NOT a menu, so a real digit-press
        # option always takes priority (handled by the menu branch below)
        # before this even runs.
        if CFG.gatekeeping_detection_enabled and not menu_look.is_menu:
            gk_look = ivr.looks_like_gatekeeping(segment)
            if gk_look.is_gatekeeping:
                gatekeeping = ivr.decide_gatekeeping(segment)
                log.info(
                    "Gatekeeping check call_sid=%s is_gatekeeping=%s has_digit=%s (%s)",
                    call_sid, gatekeeping.is_gatekeeping, gatekeeping.has_digit_option,
                    gatekeeping.reasoning,
                )

        # 4. Alternative-contact-method check (text/email/website redirect) --
        # same not-a-menu-yet gate as gatekeeping, so a real digit-press
        # option (even one that also mentions a phone number or website)
        # always takes priority.
        if CFG.gatekeeping_detection_enabled and not menu_look.is_menu:
            ac_look = ivr.looks_like_alt_contact(segment)
            if ac_look.is_alt_contact:
                alt_contact = ivr.decide_alt_contact(segment)
                log.info(
                    "Alt-contact check call_sid=%s is_alt_contact=%s has_digit=%s (%s)",
                    call_sid, alt_contact.is_alt_contact, alt_contact.has_digit_option,
                    alt_contact.reasoning,
                )

    # Once the emergency option has been pressed we are done navigating: what
    # follows is the answer to that press (a person, hold, voicemail). Real
    # incidents 2026-09-29 (Rare Restoration 1,2; First Point 0,4; Rocky
    # Mountain 9,1; Most Wanted 1,2): the rest of the SAME menu prompt kept
    # arriving after the press, read as a second-level menu, and a second
    # digit -- never an answer to anything we did -- was pressed on top of a
    # connection already in progress.
    already_on_emergency_route = bool((rec.emergency_route or rec.stop_navigating) and rec.digits_sent)

    if already_on_emergency_route and len(rec.digits_sent) == 1 and stage == "menu":
        _dig = ivr.second_emergency_digit(ivr.post_press_tail(rec.transcript_accum, rec.transcript_at_last_digit))
        _qb = media_stream.get_buffer(call_sid).quiet_for() if CFG.stream_transcription_enabled else None
        if _dig and (_qb is None or _qb >= CFG.listen_press_quiet_seconds):
            STORE.record_digit(call_sid, _dig, "keyword", False, "rule: a second recording names the emergency option again",
                               is_emergency_route=True)
            STORE.update(call_sid, stop_navigating=True, phase="awaiting_tail",
                         press_diag=(rec.press_diag + "; " if rec.press_diag else "")
                         + f"second emergency prompt: press {_dig} @+{STORE.seconds_since_answered(call_sid):.0f}s")
            log.info("IVR second emergency press call_sid=%s digit=%s", call_sid, _dig)
            return _twiml(f'<Play digits="{_dig}"/>' + _gather("tail", 0, CFG.ivr_tail_gather_seconds))

    if (menu_look and not already_on_emergency_route
            and (menu_look.is_menu or (gatekeeping and gatekeeping.has_digit_option)
                 or (alt_contact and alt_contact.has_digit_option))):
        # Press only once the menu is over: a longer silence than the general
        # listening gate, or the menu repeating (complete and looping).
        if CFG.stream_transcription_enabled and CFG.listen_gate_enabled:
            _q = media_stream.get_buffer(call_sid).quiet_for()
            if (_q is not None and _q < CFG.listen_press_quiet_seconds
                    and not ivr.menu_repeats(segment)
                    and ivr.quick_digit(segment, require_complete=True) is None
                    and rec.press_waits < CFG.listen_press_max_waits):
                STORE.update(call_sid, press_waits=rec.press_waits + 1,
                             gather_count=max(0, rec.gather_count - 1))
                _trace(call_sid, rec, "W", media_stream.get_buffer(call_sid))
                return _twiml(_gather(stage, level, 1))
        if rec.press_waits:
            STORE.update(call_sid, press_waits=0)

        decision = ivr.decide_digit(segment)

        if decision.is_menu:
            if decision.digit is None:
                STORE.mark_menu_no_digit(call_sid, decision.classifier, decision.reasoning)
                log.info("IVR menu detected but no digit call_sid=%s: %s", call_sid, decision.reasoning)
                _resolve(call_sid)
                return _twiml("<Hangup/>")

            STORE.record_digit(call_sid, decision.digit, decision.classifier,
                               decision.flagged, decision.reasoning,
                               is_emergency_route=decision.is_emergency_route)
            STORE.update(call_sid, phase="navigating")
            # A press that asks for a person -- the emergency line, a representative, the
            # operator -- ends navigation: whatever follows is the answer to it.
            _clause = ivr._option_clause((segment or "").lower(), str(decision.digit))[0]
            # (a "water, mold or fire damage" option is the line we want too: One Team Restoration 2026-10-09 pressed 1 and then,
            # off the leftover of the same menu, 2 = mold remediation)
            if (decision.is_emergency_route or ivr._clause_emergency(_clause) or ivr._clause_damage_service(_clause)
                    or any(w in _clause for w in ivr._CONNECT_WORDS)):
                STORE.update(call_sid, stop_navigating=True)
            # Timing evidence for every press: how long after answer, and how
            # long the far end had been silent. Written to the Calls notes so
            # a press that lands mid-prompt (or one the far end hangs up on)
            # is visible in the Sheet instead of having to be inferred.
            _pbuf = media_stream.peek_buffer(call_sid)
            _q = _pbuf.quiet_for() if _pbuf is not None else None
            _diag = (f"press {decision.digit} @+{STORE.seconds_since_answered(call_sid):.0f}s, "
                     f"far end quiet {'n/a' if _q is None else f'{_q:.1f}s'}"
                     f"{', interim pending' if _pbuf is not None and _pbuf.has_interim() else ''}")
            STORE.update(call_sid, press_diag=(rec.press_diag + "; " if rec.press_diag else "") + _diag)
            _trace(call_sid, rec, "P" + str(decision.digit), _pbuf)
            log.info(
                "IVR press call_sid=%s digit=%s classifier=%s flagged=%s (%s)",
                call_sid, decision.digit, decision.classifier, decision.flagged, decision.reasoning,
            )

            next_level = level + 1
            if next_level >= 2:  # navigated 2 levels -> stop (spec section 3 step 5)
                STORE.update(call_sid, phase="awaiting_tail")
                return _twiml(f'<Play digits="{decision.digit}"/>'
                              + _gather("tail", 0, CFG.ivr_tail_gather_seconds))
            return _twiml(f'<Play digits="{decision.digit}"/>'
                          + _gather("menu", next_level, CFG.ivr_tail_gather_seconds))

        # Haiku vetoed a keyword false-positive (spec section 4 guard). This
        # used to jump straight to _conclude_not_menu(), which resolves
        # immediately if AMD already has a verdict buffered -- ending the
        # call outright on a false structural match (e.g. "8 to 5" business
        # hours matching the digit+"to" menu-option pattern), even when a
        # real menu instruction was still coming later in a longer message
        # (confirmed live 2026-09-16). A veto means "not a menu YET", not
        # "never" -- fall through to the exact same empty/gather-cap/
        # keep-listening logic as a plain non-match, below.
        log.info("IVR menu vetoed call_sid=%s: %s", call_sid, decision.reasoning)

    if gatekeeping and gatekeeping.is_gatekeeping and not gatekeeping.has_digit_option:
        log.info("Gatekeeping detected call_sid=%s: %s", call_sid, gatekeeping.reasoning)
        STORE.mark_gatekeeping(call_sid, gatekeeping.classifier, gatekeeping.reasoning)
        _resolve(call_sid)
        return _twiml("<Hangup/>")

    if alt_contact and alt_contact.is_alt_contact and not alt_contact.has_digit_option:
        log.info("Alt-contact redirect detected call_sid=%s: %s", call_sid, alt_contact.reasoning)
        STORE.mark_alt_contact(call_sid, alt_contact.classifier, alt_contact.reasoning)
        _resolve(call_sid)
        return _twiml("<Hangup/>")

    # Not (yet) a menu (or a vetoed false-positive), not gatekeeping, not alt-contact.
    if empty:
        return _conclude_not_menu(call_sid, rec)
    # Cycle-count guard only -- the time-based budget above is the real cap.
    # Deferred turns and the longer hold budget both add cycles, so the count
    # limit is scaled to the hard cap (2s is the shortest cycle we ever use).
    if rec.gather_count >= max(CFG.ivr_max_gather_cycles, CFG.ivr_hard_cap_seconds // 2):
        log.info("IVR gather cap reached call_sid=%s; concluding not-a-menu", call_sid)
        return _conclude_not_menu(call_sid, rec)

    # Fast hangup for a genuine live pickup: a confident "answered" verdict on
    # real (non-empty) speech resolves now, once at least 2 non-empty turns
    # have been heard. Never trusted on turn 1 alone -- confirmed a scripted
    # voicemail opening exactly like "hi this is Mike... how can I help you"
    # also reads as confidently answered by itself, only revealing itself once
    # "please leave your name and number" arrives on a later turn. A live
    # human reacting to silence with a second prompt is not something a
    # static recording can fake the same way. Never trusts an early
    # voicemail/unknown verdict here either -- same asymmetry as hit_time_cap
    # and _conclude_not_menu: a scripted message can still be leading into a
    # real menu or reveal itself as voicemail later.
    if rec.gather_count >= 2:
        early = ivr.decide_tail(segment, rec.answered_by, rec.company_name)
        # classifier != "amd_fallback" / mentions_incoming_menu guards added
        # 2026-09-28, matching the same guards already applied at every other
        # decide_tail() call site in this file -- this was the one place they
        # were missing. Real incident: Paul Davis Restoration's corporate
        # line was hung up on here mid-menu ("Press two if you are calling
        # regarding existing..." still being read) because Haiku's own
        # structured read said "not a live human, not voicemail, not hold"
        # (this IS an IVR menu) but fell through to trusting AMD's bare
        # 'human' tag as a confident "answered" -- the exact bare-AMD-guess
        # pattern this codebase already stopped trusting at three other
        # resolution sites, just never patched here.
        if (early.outcome == "answered" and early.classifier != "amd_fallback"
                and ivr.ends_cleanly(segment)
                and not ivr.mentions_incoming_menu(segment)):
            log.info("IVR early-resolve call_sid=%s: confident answered on turn %d (%s)",
                      call_sid, rec.gather_count, early.reasoning)
            _resolve(call_sid)
            return _twiml("<Hangup/>")

    # Turn 1 alone already sounds like a confident live pickup (same read as
    # above) but isn't trusted yet -- real incident 2026-09-21: real people
    # picking up were left in dead air for up to the full tail-gather window
    # (20s) waiting on a second turn we won't act on unless it also sounds
    # human, saying "hello... hello" into silence because this dialer never
    # speaks. Listening again is still required (still won't trust turn 1
    # alone -- a voicemail opening can read identically human), but there's
    # no reason to make a live person wait the same window used elsewhere to
    # avoid truncating a long disclosure -- confirm/deny much sooner instead.
    if rec.gather_count == 1:
        early = ivr.decide_tail(segment, rec.answered_by, rec.company_name)
        # Same amd_fallback/mentions_incoming_menu guards as the gather_count
        # >= 2 branch above -- lower stakes here (this only picks a shorter
        # confirm-gather window, it never hangs up), but no reason to trust
        # a bare AMD guess here either when it's not trusted anywhere else.
        if (early.outcome == "answered" and early.classifier != "amd_fallback"
                and ivr.ends_cleanly(segment)
                and not ivr.mentions_incoming_menu(segment)):
            return _twiml(_gather("menu", level, CFG.ivr_confirm_gather_seconds))

    # Keep listening through non-actionable speech (the menu instruction may be
    # at the tail end of a longer disclosure).
    return _twiml(_gather("menu", level, CFG.ivr_tail_gather_seconds))


def _conclude_not_menu(call_sid: str, rec) -> Response:
    if rec.ivr_detected:
        STORE.update(call_sid, phase="awaiting_tail")
        return _ivr_tail(call_sid, rec)

    STORE.update(call_sid, phase="awaiting_amd")
    if rec.answered_by:  # AMD verdict already buffered
        # A bare AMD verdict plus a transcript that only sounds like hold or
        # transfer language ("connecting you now") is not a real resolution --
        # it's mid-flight status, same as the tail-wait fix. Confirmed real
        # incident: Aaa Disaster Recovery got cut off at 18 seconds on exactly
        # this pattern, never given a chance for a live person to join.
        # Only a clear answered/voicemail transcript verdict (or no
        # transcript at all -- then AMD is all we have) resolves early here;
        # anything else keeps listening, bounded by the outer master timer.
        transcript = rec.transcript_accum.strip()
        if transcript:
            decision = ivr.decide_tail(transcript, rec.answered_by, rec.company_name)
            # classifier != "amd_fallback" guard added 2026-09-22: real
            # incident -- Icon Property Rescue resolved 'voicemail' here at
            # 34s from nothing but a generic "this call may be recorded"
            # disclosure, Haiku itself reporting low confidence, purely on
            # AMD's already-established-unreliable fast-mode verdict. Same
            # gap as the hit_time_cap fix just above in this file, one call
            # site over: a real keyword/Haiku read still resolves
            # immediately (that evidence is trustworthy), but a pure
            # AMD-only guess now falls through to keep listening instead of
            # ending the call on a signal this codebase already knows not
            # to trust blindly -- bounded by the outer master timer either
            # way, which itself no longer blindly trusts AMD either.
            # mentions_incoming_menu guard added 2026-09-22: real incident --
            # Epic Restoration's transcript ends at "please choose from 1 of
            # the following options" with nothing after it, Haiku
            # confidently (not amd_fallback) read the surrounding
            # after-hours framing as voicemail, and the call hung up before
            # ever hearing what those options were -- possibly including an
            # emergency line. The business just told us more was coming;
            # trusting a confident-sounding conclusion at that exact moment
            # is the bug, not the confidence itself. Keep listening at
            # least one more cycle instead.
            _thin_machine = (decision.outcome == "answered" and decision.classifier != "rule"
                             and (rec.answered_by or "").startswith("machine") and ivr.thin_answer_reason(transcript)
                             and not ivr.echoed_name_after_opening(transcript, rec.company_name))
            if (decision.outcome in ("answered", "voicemail", "disconnected")
                    and (decision.outcome != "answered" or ivr.ends_cleanly(transcript))
                    and decision.classifier != "amd_fallback"
                    and not _thin_machine
                    and not ivr.mentions_incoming_menu(transcript)):
                _resolve(call_sid)
                return _twiml("<Hangup/>")
            return _twiml(_gather("menu", 0, CFG.ivr_tail_gather_seconds))
        # No text at all. If the audio shows a RINGING cadence the far end has not
        # picked up yet (Longview / Doan / Beacon 2026-10-01: AMD "human", ~800 frames,
        # zero words, we hung up at 18s mid-ring). Shadow mode only notes it.
        # Once ringing has been seen, keep waiting even if the cadence later breaks up (Dry Patrol Akron
        # 2026-10-05: ringing, then a different tone, and we hung up at 23s with the budget unused).
        if CFG.stream_transcription_enabled and (rec.ring_seen or media_stream.ringback_pattern(media_stream.get_buffer(call_sid).energy)):
            _at = STORE.seconds_since_answered(call_sid)
            if not rec.ring_seen:
                STORE.update(call_sid, ring_seen=True,
                             press_diag=(rec.press_diag + "; " if rec.press_diag else "")
                                        + f"ring pattern at +{_at:.0f}s, no text "
                                        + ("-- waiting" if CFG.ring_wait_enabled else "-- shadow: would have kept waiting"))
            if CFG.ring_wait_enabled and not _budget_exhausted(call_sid, rec, margin=3):
                return _twiml(_gather("menu", 0, CFG.ivr_tail_gather_seconds))
        _resolve(call_sid)
        return _twiml("<Hangup/>")
    # Hand off to AMD: hold the line for the remaining budget, then hang up. If
    # AMD posts during the pause, /webhooks/amd resolves and hangs up.
    return _twiml(f'<Pause length="{_amd_pause_seconds(call_sid)}"/><Hangup/>')


def _ivr_tail(call_sid: str, rec) -> Response:
    """Post-navigation wait for pickup/hold resolution. Spec section 3 step 7:
    'No AMD resolution by the 60-second cap -> extended_hold, force hangup' --
    conclusive speech or the master timer are the ONLY things allowed to end
    this early. A prior 'give up after 2 empty tail turns' (~10s) shortcut
    violated that: it hung up long before the documented 60s budget was used,
    mislabeling calls that were still genuinely on hold as extended_hold based
    on 10 seconds of silence, not 60. Removed."""
    tail = _tail_of(rec)
    STORE.bump_tail(call_sid)
    # Only answered/voicemail are genuinely terminal from a keyword match --
    # hearing hold language ("will be with you momentarily") is current
    # status, not a resolution. Treating it as conclusive hangs up the
    # instant hold is detected, never giving the rest of the 60s budget a
    # chance for a human to actually join. Per spec, extended_hold is ONLY
    # reached by exhausting the master timer with no real resolution.
    tail_class = ivr.classify_tail(tail) if tail else "unknown"
    conclusive = tail_class == "voicemail" or (tail_class == "answered" and ivr.ends_cleanly(tail))
    over_budget = _budget_exhausted(call_sid, rec, margin=3)
    if conclusive:
        _resolve(call_sid)
        return _twiml("<Hangup/>")
    if over_budget:
        # Real incident 2026-09-28 (HS Restoration): this check resolves the
        # call directly, same as the top-level master-timer check in
        # ivr_turn(), but -- unlike that one -- never set hit_time_cap. Since
        # _compute_outcome()'s FIRST branch is gated on that flag, a call
        # that correctly pressed a real digit (ivr_detected=True) and then
        # ran out of time HERE fell through to the wrong "ivr_unresolved"
        # ("no digit could be determined" -- false, we determined and
        # pressed one) instead of the correct "extended_hold" (ran out of
        # time after real navigation, no resolution). Setting the flag here
        # too routes it through the same cap_transcript/decide_tail logic
        # the top-level check already uses.
        STORE.update(call_sid, hit_time_cap=True)
        _resolve(call_sid)
        return _twiml("<Hangup/>")
    return _twiml(_gather("tail", 0, CFG.ivr_tail_gather_seconds))


# --------------------------------------------------------------------------- #
# webhooks
# --------------------------------------------------------------------------- #

@app.post("/webhooks/amd")
def webhook_amd() -> Response:
    if not _verify(request):
        return Response("invalid signature", status=403)
    call_sid = request.form.get("CallSid", "")
    _maybe_rehydrate(call_sid)
    answered_by = (request.form.get("AnsweredBy") or "").strip()
    STORE.update(call_sid, answered_by=answered_by, to_number=request.form.get("To") or None)
    rec = STORE.get(call_sid)
    phase = rec.phase if rec else "dialing"
    log.info("AMD result call_sid=%s AnsweredBy=%s phase=%s", call_sid, answered_by, phase)
    # Durable checkpoint, not just the in-memory CallStore -- see
    # app/amd_checkpoint.py's docstring for the real incident this closes.
    # A side-channel write, no live call audio waiting on it.
    amd_checkpoint.checkpoint(call_sid, answered_by)
    if rec:
        call_checkpoint.save(rec)

    if rec and rec.logged:
        # Real incident 2026-09-22: Tri County Cleaning Systems logged
        # 'unknown' with answered_by blank, but SignalWire's own Call
        # resource showed 'machine_start' the whole time -- this webhook
        # just arrived after _resolve() already wrote the row, and
        # returning here with no backfill (as before) silently threw away
        # a real signal we now have. Same pattern as backfill_call_status
        # for twilio_call_status/duration_sec, applied to answered_by.
        # Best-effort only -- never let a Sheets failure break webhook
        # handling, which must always 204 back to SignalWire regardless.
        if answered_by:
            try:
                google_sheets.backfill_answered_by(call_sid, answered_by)
            except Exception as e:  # noqa: BLE001
                log.warning("backfill_answered_by failed for call_sid=%s: %s", call_sid, e)
        return Response("", status=204)

    ab = answered_by.lower()
    decisive = ab in _DECISIVE_AMD or ab.startswith("machine")

    if phase == "awaiting_amd":
        _resolve(call_sid)
        return Response("", status=204)

    if phase in ("listening", "navigating"):
        # Mid-IVR: AMD judged the menu audio, not the final party -- always
        # buffer, never resolve here. This used to resolve immediately on
        # 'human' + no-menu-detected-YET, but AMD often arrives within
        # milliseconds of connect, before any speech has even been gathered
        # -- looks_like_menu("") is trivially "not a menu", so this fired on
        # an empty transcript and ended calls before they'd heard anything
        # (confirmed live: a call resolved 20ms after connect, before its
        # first /ivr/turn). The /ivr/turn flow's own _conclude_not_menu()
        # already resolves correctly using this same answered_by value, but
        # only once a turn genuinely comes back empty or the gather-cycle
        # cap is hit -- that is the right point, not AMD's mere arrival.
        log.info("AMD buffered during IVR navigation (phase=%s)", phase)
        return Response("", status=204)

    if phase == "dialing":
        # Call answered but no /ivr/turn yet. With IVR on, the gather is coming --
        # buffer. With IVR off, resolve on a decisive verdict.
        if not CFG.ivr_enabled and decisive:
            _resolve(call_sid)
        else:
            log.info("AMD arrived before first IVR turn; buffering")
        return Response("", status=204)

    # awaiting_tail / resolved: menu call -- ignore AMD entirely.
    return Response("", status=204)


_TERMINAL_CALL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}


@app.post("/webhooks/status")
def webhook_status() -> Response:
    if not _verify(request):
        return Response("invalid signature", status=403)
    call_sid = request.form.get("CallSid", "")
    # Rehydrate BEFORE computing `recovered` below -- if a checkpoint exists
    # for a genuinely in-flight (not already-logged) call, this restores the
    # full pre-restart state, so `recovered` correctly becomes False (we're
    # no longer working from a blank stub) and _resolve() proceeds with real
    # context instead of the recovered-stub Sheet re-check path. If nothing
    # to rehydrate (already logged, or never checkpointed), this is a no-op
    # and behavior is unchanged from before.
    _maybe_rehydrate(call_sid)
    call_status = (request.form.get("CallStatus") or "").strip()
    duration = request.form.get("CallDuration", "")
    # Captured BEFORE the upsert below creates a record: True only if this
    # process has never seen call_sid in any capacity (not placed by it, no
    # earlier /ivr/turn or /webhooks/amd for it) -- the signal that a restart,
    # not a normal never-answered call, is why nothing is known about it. A
    # normal never-answered call already has a record from place_call's own
    # STORE.register() at dial time, so this stays False for it.
    recovered = STORE.get(call_sid) is None
    STORE.update(call_sid, call_status=call_status, to_number=request.form.get("To") or None)
    if duration:
        STORE.update(call_sid, call_duration=duration)
    rec = STORE.get(call_sid)
    log.info(
        "Status callback call_sid=%s CallStatus=%s phase=%s",
        call_sid, call_status, rec.phase if rec else "?",
    )

    if rec and rec.logged:
        # The row's already written, but the terminal status/duration often
        # arrives after we resolved from a faster IVR/AMD/gatekeeping signal
        # -- backfill it into the already-logged row instead of dropping it.
        if call_status.lower() in _TERMINAL_CALL_STATUSES:
            # rec.logged is set the instant _resolve() CLAIMS the call, before the
            # row is actually written (hang-up + Sheets write take 1-3s). For a call
            # WE hang up, this callback lands in exactly that window: the row is not
            # there yet, the backfill finds nothing and used to give up silently,
            # leaving duration blank and the status stuck at "answered" (4 rows on
            # 2026-10-01: Houzpital, Freedom Services, Icon Property Rescue, National
            # Fire & Water). Retry until the row exists.
            for attempt in range(6):
                try:
                    if google_sheets.backfill_call_status(call_sid, call_status, duration):
                        break
                except Exception as e:  # noqa: BLE001 - best effort only
                    log.warning("Status backfill failed for %s: %s", call_sid, e)
                time.sleep(1.5)
        return Response("", status=204)

    s = call_status.lower()
    if s in ("busy", "no-answer", "failed", "canceled"):
        _resolve(call_sid, hangup=False, recovered=recovered)
    elif s == "completed":
        # Call ended -- resolve with whatever signals we have.
        _resolve(call_sid, hangup=False, recovered=recovered)
    return Response("", status=204)


@app.post("/webhooks/recording")
def webhook_recording() -> Response:
    if not _verify(request):
        return Response("invalid signature", status=403)
    call_sid = request.form.get("CallSid", "")
    recording_url = request.form.get("RecordingUrl", "")
    log.info("Recording ready call_sid=%s url=%s", call_sid, recording_url)
    STORE.update(call_sid, recording_url=recording_url)
    return Response("", status=204)


@app.post("/webhooks/transcription")
def webhook_transcription() -> Response:
    """TEMPORARY feasibility probe (2026-09-20/21): SignalWire only documents
    Transcribe/TranscribeCallback on the <Record> verb, not whole-call REST
    recording -- this logs the raw callback as its own Sheet row (rather than
    trying to backfill the real call row) so it can be inspected directly to
    confirm whether the combination works at all, and what fields it sends,
    before building the real reconciliation pass on top of it."""
    if not _verify(request):
        return Response("invalid signature", status=403)
    fields = request.form.to_dict()
    log.info("Transcription callback: %s", fields)
    now_utc = datetime.now(timezone.utc)
    google_sheets.append_result({
        "company_name": "TEST_TRANSCRIPTION_WEBHOOK_PROBE",
        "outcome": fields.get("TranscriptionStatus", ""),
        "logged_at_date": now_utc.strftime("%b %d, %Y"),
        "logged_at_time": now_utc.strftime("%I:%M %p UTC"),
        "notes": repr(fields),
        "ivr_transcript": fields.get("TranscriptionText", ""),
        "logged_at_iso": now_utc.isoformat(),
        "call_sid": fields.get("CallSid", ""),
        "recording_url": fields.get("RecordingUrl", ""),
    })
    return Response("", status=204)


# --------------------------------------------------------------------------- #
# test-IVR fixture (2026-09-21): a fake "business" we own and fully control,
# for exercising the real live call pipeline (AMD, Gather, IVR navigation,
# alt_contact/gatekeeping detection, the resolution race-condition fix) any
# time of day without waiting for a real nightly batch or spending a real
# company. Dial the number this is configured on via `python -m app.dial`
# exactly like a real outbound call -- it goes through the same /calls ->
# place_call -> AMD/Gather/_resolve pipeline as any real company, it's just
# the audio on the other end that's ours. To switch which scenario the
# number plays, update its VoiceUrl via the SignalWire REST API
# (IncomingPhoneNumbers/{sid}.json, VoiceUrl=.../test_ivr/start?scenario=X);
# not exposed as a query param on this same request because inbound Voice
# webhook requests don't carry any outbound-call context to key off of.
_TEST_IVR_SCENARIOS = {
    # Regression fixture for the personal-cell-voicemail incident
    # (2026-09-21): a real carrier voicemail's OWN message-control menu,
    # reproducing its exact structure (voicemail language, THEN a numbered
    # menu) so the looks_like_menu() fix can be validated live against
    # something that actually exercises it -- neither "voicemail" (no menu
    # at all) nor "menu" (no voicemail language) below does that.
    "voicemail_control_menu": (
        "<Say>Please leave a message after the tone. At the tone, please "
        "record your message. When you have finished recording, press "
        "pound for further options.</Say>"
        '<Pause length="8"/>'
        "<Say>To review, re-record, or add to your message, press pound. "
        "Press 1 to mark your message urgent. Press 2 to mark your message "
        "private. Press 3 to send your message as is.</Say>"
        '<Pause length="20"/>'
    ),
    "voicemail": (
        "<Say>Thank you for calling Test Fake Business. We are unable to "
        "take your call right now. Please leave your name and number after "
        "the tone and we will call you back.</Say>"
        '<Pause length="1"/>'
        "<Say>At the tone, please record your message. When you have "
        "finished recording, you may hang up.</Say>"
        '<Pause length="45"/>'
    ),
    "menu": (
        # No <Gather> on this (callee) side -- confirmed live 2026-09-21 that
        # an active Gather on BOTH legs of a call that lives entirely on our
        # own SignalWire account (this number + our own dialer's own Gather)
        # reproducibly causes total transcript blackout (2/2 failures),
        # while the same setup with no competing Gather succeeded cleanly
        # (2/2). Real businesses run their own phone system's DTMF
        # detection, not SignalWire's Gather, so that contention doesn't
        # exist in production -- but it's not needed here either: our own
        # dialer decides which digit to press and logs it (digits_sent)
        # independent of whether this side "hears" it via its own Gather.
        # The pause gives our dialer time to send the digit; the follow-up
        # Say+Pause gives its post-digit tail-listening Gather real content
        # to classify, regardless of which digit came in.
        "<Say>Thank you for calling Test Fake Business. Press 1 for sales. "
        "Press 2 for support. Press 3 for our emergency line. Press 0 for "
        "the operator.</Say>"
        '<Pause length="8"/>'
        "<Say>Please hold while we connect you to the next available "
        "representative.</Say>"
        '<Pause length="20"/>'
    ),
    "alt_contact": (
        "<Say>Thank you for calling Test Fake Business. If you have an "
        "emergency, please text us at 5 5 5 0 1 2 3 4 5 6. Otherwise, "
        "please leave a message after the tone.</Say>"
        '<Pause length="45"/>'
    ),
    "gatekeeping": (
        # No <Gather> here either, same competing-Gather reasoning as menu
        # above -- and unneeded regardless, since the correct dialer
        # behavior being tested is recognizing there's no way forward and
        # hanging up (gatekeeping_miss), not sending anything back.
        "<Say>Thank you for calling Test Fake Business automated account "
        "system. Please enter your ten digit account number followed by "
        "the pound sign.</Say>"
        '<Pause length="30"/>'
    ),
    "hold": (
        "<Say>Thank you for calling Test Fake Business. Please hold while "
        "we connect you to the next available representative.</Say>"
        '<Pause length="45"/>'
    ),
    "human": (
        "<Say>Hello, this is Jane, how can I help you today?</Say>"
        '<Pause length="5"/>'
        "<Say>Hello? Are you still there?</Say>"
        '<Pause length="20"/>'
    ),
}


@app.post("/test_ivr/start")
@app.get("/test_ivr/start")
def test_ivr_start() -> Response:
    if not _verify(request):
        return Response("invalid signature", status=403)
    scenario = request.values.get("scenario", "voicemail")
    body = _TEST_IVR_SCENARIOS.get(scenario, _TEST_IVR_SCENARIOS["voicemail"])
    return _twiml(body)


# --------------------------------------------------------------------------- #
# inbound / callback pool (spec sections 9-10)
# --------------------------------------------------------------------------- #

# The ONLY response this endpoint can ever produce. <Reject> does not answer the
# call -- no SIP 200, no media, $0, nothing spoken. Every pool number points its
# Voice webhook here (see `python -m app.setup_inbound`), so behaviour is
# identical across the pool and is not configured per-number.
_REJECT_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<Response><Reject reason="{CFG.inbound_reject_reason}"/></Response>'
)


@app.post("/inbound/voice")
def inbound_voice() -> Response:
    verified = _verify(request)
    frm = request.form.get("From", "")
    to = request.form.get("To", "")
    call_sid = request.form.get("CallSid", "")
    city = request.form.get("FromCity", "")
    state = request.form.get("FromState", "")

    # Log first (best-effort), then reject. A logging failure never changes the
    # response. An unverified request is still rejected (fail safe) but noted.
    try:
        from . import inbound_log
        inbound_log.record(
            from_number=frm, from_city=city, from_state=state, to_number=to,
            call_sid=call_sid,
            action="rejected" if verified else "rejected (UNVERIFIED webhook)",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("inbound_log.record raised: %s", e)

    return Response(_REJECT_TWIML, mimetype="text/xml")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CFG.port, threaded=True)

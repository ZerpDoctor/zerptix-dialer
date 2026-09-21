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
from twilio.request_validator import RequestValidator

from .config import CFG
from .call_store import STORE
from . import outcomes
from . import google_sheets
from . import ivr
from .dialer import hang_up, place_call, DialError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("dialer.server")

app = Flask(__name__)

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
    return (
        f'<Gather input="speech" speechTimeout="auto" '
        f'speechModel="{CFG.ivr_speech_model}" language="en-US" '
        f'timeout="{timeout}" actionOnEmptyResult="true" '
        f'method="POST" action="{action}"/>'
    )


def _amd_pause_seconds(call_sid: str) -> int:
    remaining = CFG.ivr_master_timeout_seconds - STORE.seconds_since_answered(call_sid)
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
        return by_status, ""

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
        cap_transcript = (
            rec.transcript_accum[rec.transcript_at_last_digit:] if rec.ivr_detected
            else rec.transcript_accum
        ).strip()
        if cap_transcript:
            decision = ivr.decide_tail(cap_transcript, rec.answered_by)
            if decision.outcome != "unknown":
                note = f"tail: {decision.reasoning}" if decision.reasoning else ""
                return decision.outcome, note
        return "extended_hold", "hit 60s master timer with no resolution"

    if rec.ivr_detected and not rec.digits_sent:
        return "ivr_unresolved", "menu detected but no digit could be determined; check recording"

    transcript = (
        rec.transcript_accum[rec.transcript_at_last_digit:] if rec.ivr_detected
        else rec.transcript_accum
    ).strip()

    if not transcript:
        if rec.ivr_detected:
            return "extended_hold", "silence after menu navigation (likely on hold / in queue)"
        by_amd = outcomes.from_amd(rec.answered_by)
        if by_amd:
            return by_amd, ""
        if cs == "completed":
            return "unknown", "call completed; AMD gave no usable result"
        return "unknown", ""

    decision = ivr.decide_tail(transcript, rec.answered_by)
    note = f"tail: {decision.reasoning}" if decision.reasoning else ""
    if decision.outcome == "unknown":
        if rec.ivr_detected:
            return "ivr_unresolved", note or "post-menu audio inconclusive; check recording"
        return "unknown", note or "call completed; audio inconclusive"
    return decision.outcome, note


def _build_row(rec, outcome: str, note: str) -> dict:
    notes = [n for n in (note, rec.resolve_note) if n]
    if rec.ivr_reasoning:
        notes.append(f"ivr: {rec.ivr_reasoning}")
    if rec.gatekeeping_reasoning:
        notes.append(f"gatekeeping: {rec.gatekeeping_reasoning}")
    if rec.alt_contact_reasoning:
        notes.append(f"alt_contact: {rec.alt_contact_reasoning}")
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
    return {
        "company_name": company_name,
        "logged_at_iso": now_utc.isoformat(),
        "logged_at_date": logged_at_date,
        "logged_at_time": logged_at_time,
        "phone_e164": phone,
        "outcome": outcome,
        "answered_by": rec.answered_by or "",
        "twilio_call_status": rec.call_status or "",
        "call_sid": rec.call_sid,
        "duration_sec": request.form.get("CallDuration", ""),
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
        ),
    }


def _update_queue_row(rec, outcome: str) -> None:
    """If this call's number belongs to a company in the Queue tab, write the
    outcome + quarter status back to that row (spec section 7), under the
    per-phone write lock. Best-effort -- never breaks call resolution."""
    try:
        from .queue_backend import SheetQueue
        from . import queue_writer

        q = SheetQueue(CFG.sched_queue_tab)
        fields = queue_writer.apply_outcome(
            q, rec.to_number, outcome,
            call_sid=rec.call_sid,
            recording_url=rec.recording_url or "",
            ivr_flagged=rec.ivr_fallback_flagged,
        )
        if fields is None:
            return  # not a queued company
        log.info("Queue write-back: %s -> %s", rec.to_number,
                 fields.get("this_quarter_status", "in_progress (unchanged)"))
    except Exception as e:  # noqa: BLE001
        log.warning("Queue write-back failed for %s: %s", rec.call_sid, e)


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
    # snapshot, not get(): _compute_outcome() and _build_row() both read rec's
    # fields, and a concurrent webhook thread mutating the live record in
    # between the two would decide the outcome from one instant and build the
    # notes from another -- see CallStore.snapshot's docstring for the real
    # incident this fixes.
    rec = STORE.snapshot(call_sid)
    outcome, note = _compute_outcome(rec)
    _write_sheet_with_retry(_build_row(rec, outcome, note))
    STORE.update(call_sid, phase="resolved")
    log.info(
        "LOGGED call_sid=%s outcome=%s number=%s ivr=%s digits=%s classifier=%s flagged=%s",
        call_sid, outcome, rec.to_number, rec.ivr_detected,
        ",".join(rec.digits_sent) or "-", rec.classifier, rec.ivr_fallback_flagged,
    )
    _update_queue_row(rec, outcome)


# --------------------------------------------------------------------------- #
# basic endpoints
# --------------------------------------------------------------------------- #

@app.get("/healthz")
def healthz() -> Response:
    return Response("ok", mimetype="text/plain")


@app.post("/calls")
def place_call_endpoint():
    data = request.get_json(silent=True) or request.form
    to_number = (data.get("to_number") or "").strip()
    record_attempt = str(data.get("record_attempt", "")).lower() in ("1", "true", "yes")
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
            call_sid = place_call(to_number, from_number=from_number)
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
                        company_timezone=row.timezone if row is not None else "")

        fields = None
        if row is not None:
            try:
                ld = _date.fromisoformat(local_date_iso) if local_date_iso else _dt.now(_tz.utc).date()
            except ValueError:
                ld = _dt.now(_tz.utc).date()
            fields = queue_writer.record_attempt(
                q, to_number, call_sid=call_sid, window=window,
                local_date=ld, now_utc=_dt.now(_tz.utc),
            )
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
    return _twiml(_gather("menu", 0, CFG.ivr_initial_timeout_seconds))


@app.post("/ivr/turn/<stage>/<int:level>")
def ivr_turn(stage: str, level: int) -> Response:
    if not _verify(request):
        return Response("invalid signature", status=403)

    call_sid = request.form.get("CallSid", "")
    speech = (request.form.get("SpeechResult") or "").strip()
    STORE.update(call_sid, to_number=request.form.get("To") or None)
    STORE.ensure_answered(call_sid)
    rec = STORE.add_turn(call_sid, speech)
    log.info(
        "IVR turn call_sid=%s stage=%s level=%s gather=%d speech=%r",
        call_sid, stage, level, rec.gather_count, speech[:200],
    )

    # 1. Master timer (covers IVR + hold combined, spec section 3).
    if STORE.seconds_since_answered(call_sid) >= CFG.ivr_master_timeout_seconds:
        STORE.update(call_sid, hit_time_cap=True)
        _resolve(call_sid)
        return _twiml("<Hangup/>")

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

    if stage == "tail":
        return _ivr_tail(call_sid, rec)

    # stage == "menu" -- only consider speech heard since the last digit press,
    # so an earlier menu's text doesn't re-trigger navigation at the next level.
    segment = rec.transcript_accum[rec.transcript_at_last_digit:]
    menu_look = ivr.looks_like_menu(segment)

    # 3. Digital-gatekeeping check -- only ever consulted when the menu keyword
    # gate already said this is NOT a menu, so a real digit-press option always
    # takes priority (handled by the menu branch below) before this even runs.
    gatekeeping = None
    if CFG.gatekeeping_detection_enabled and not menu_look.is_menu:
        gk_look = ivr.looks_like_gatekeeping(segment)
        if gk_look.is_gatekeeping:
            gatekeeping = ivr.decide_gatekeeping(segment)
            log.info(
                "Gatekeeping check call_sid=%s is_gatekeeping=%s has_digit=%s (%s)",
                call_sid, gatekeeping.is_gatekeeping, gatekeeping.has_digit_option,
                gatekeeping.reasoning,
            )

    # 4. Alternative-contact-method check (text/email/website redirect) -- same
    # not-a-menu-yet gate as gatekeeping, so a real digit-press option (even
    # one that also mentions a phone number or website) always takes priority.
    alt_contact = None
    if CFG.gatekeeping_detection_enabled and not menu_look.is_menu:
        ac_look = ivr.looks_like_alt_contact(segment)
        if ac_look.is_alt_contact:
            alt_contact = ivr.decide_alt_contact(segment)
            log.info(
                "Alt-contact check call_sid=%s is_alt_contact=%s has_digit=%s (%s)",
                call_sid, alt_contact.is_alt_contact, alt_contact.has_digit_option,
                alt_contact.reasoning,
            )

    if menu_look.is_menu or (gatekeeping and gatekeeping.has_digit_option) \
            or (alt_contact and alt_contact.has_digit_option):
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
    if rec.gather_count >= CFG.ivr_max_gather_cycles:
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
        early = ivr.decide_tail(segment, rec.answered_by)
        if early.outcome == "answered":
            log.info("IVR early-resolve call_sid=%s: confident answered on turn %d (%s)",
                      call_sid, rec.gather_count, early.reasoning)
            _resolve(call_sid)
            return _twiml("<Hangup/>")

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
            decision = ivr.decide_tail(transcript, rec.answered_by)
            if decision.outcome in ("answered", "voicemail"):
                _resolve(call_sid)
                return _twiml("<Hangup/>")
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
    tail = rec.transcript_accum[rec.transcript_at_last_digit:].strip()
    STORE.bump_tail(call_sid)
    # Only answered/voicemail are genuinely terminal from a keyword match --
    # hearing hold language ("will be with you momentarily") is current
    # status, not a resolution. Treating it as conclusive hangs up the
    # instant hold is detected, never giving the rest of the 60s budget a
    # chance for a human to actually join. Per spec, extended_hold is ONLY
    # reached by exhausting the master timer with no real resolution.
    tail_class = ivr.classify_tail(tail) if tail else "unknown"
    conclusive = tail_class in ("answered", "voicemail")
    over_budget = STORE.seconds_since_answered(call_sid) >= CFG.ivr_master_timeout_seconds - 3
    if conclusive or over_budget:
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
    answered_by = (request.form.get("AnsweredBy") or "").strip()
    STORE.update(call_sid, answered_by=answered_by, to_number=request.form.get("To") or None)
    rec = STORE.get(call_sid)
    phase = rec.phase if rec else "dialing"
    log.info("AMD result call_sid=%s AnsweredBy=%s phase=%s", call_sid, answered_by, phase)

    if rec and rec.logged:
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
            try:
                google_sheets.backfill_call_status(call_sid, call_status, duration)
            except Exception as e:  # noqa: BLE001 - best effort only
                log.warning("Status backfill failed for %s: %s", call_sid, e)
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

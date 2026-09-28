"""Thin wrapper around SignalWire's Compatibility (LaML/cXML) REST API for
placing one outbound call.

There is no SDK dependency here on purpose: SignalWire's Compatibility API is
a plain HTTP REST surface at

    https://<SIGNALWIRE_SPACE_URL>/api/laml/2010-04-01/Accounts/<SIGNALWIRE_PROJECT_ID>/Calls.json

authenticated with HTTP Basic Auth (project id as username, API token as
password), form-encoded request body, JSON response -- confirmed against
SignalWire's own REST reference, param-for-param compatible with Twilio's
Calls resource (To, From, Url, Method, StatusCallback, StatusCallbackEvent,
MachineDetection, AsyncAmd, AsyncAmdStatusCallback,
AsyncAmdStatusCallbackMethod, Record, RecordingStatusCallback). The
`signalwire` PyPI packages available at migration time did not provide a
stable REST calls-client for this surface (one is the AI Agents/SWML SDK,
unrelated to placing calls), so plain HTTP avoids depending on the wrong
thing.

Mirrors app/twilio_dialer.py's contract exactly (DialError, place_call,
hang_up) so app/dialer.py can dispatch between the two providers.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .config import CFG

log = logging.getLogger(__name__)

_TRIAL_HINTS = ("trial", "verify", "unverified", "not allowed", "not permitted")


class DialError(RuntimeError):
    """A call could not be placed. Carries a human-readable reason."""


def _base_url() -> str:
    space = CFG.signalwire_space_url.strip().rstrip("/")
    return f"https://{space}/api/laml/2010-04-01/Accounts/{CFG.signalwire_project_id}"


def _auth_header() -> str:
    raw = f"{CFG.signalwire_project_id}:{CFG.signalwire_api_token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _post(path: str, params: list[tuple[str, str]]) -> dict:
    """POST form-encoded params to the compat REST API. Returns parsed JSON.
    Raises DialError on any non-2xx response or transport failure."""
    url = f"{_base_url()}/{path}"
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": _auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.loads(raw)
            msg = detail.get("message") or detail.get("error") or raw
            code = detail.get("code")
        except (json.JSONDecodeError, AttributeError):
            msg, code = raw, None
        low = msg.lower()
        if any(h in low for h in _TRIAL_HINTS):
            raise DialError(
                f"SignalWire trial-mode restriction (HTTP {e.code}"
                + (f", code {code}" if code else "") + f"): {msg}. "
                f"Verify the destination number under Phone Numbers -> Verified, "
                f"or clear trial mode by funding the account, then retry."
            ) from e
        raise DialError(
            f"SignalWire rejected the request (HTTP {e.code}"
            + (f", code {code}" if code else "") + f"): {msg}"
        ) from e
    except urllib.error.URLError as e:
        raise DialError(f"SignalWire call placement failed (network error): {e}") from e


def place_call(to_number: str, *, from_number: str | None = None,
                record: bool | None = None) -> str:
    """Place one outbound call with async AMD enabled. Returns the CallSid.

    `from_number` lets a caller (the scheduler's number-pool distribution)
    choose which of several SignalWire numbers places this call; defaults to
    CFG.signalwire_from_number for manual/single-number use.

    `record` overrides CFG.record_calls for just this one call when explicitly
    passed (True or False) -- None (the default, and the only value any
    scheduler-placed call ever passes) means "use the global CFG.record_calls
    setting" so this never changes a normal Queue call's behavior. Added
    2026-09-28 so a manual /calls test can opt into recording (e.g. to verify
    DTMF actually transmits) without flipping recording on for real calls.

    Raises DialError with a clear message on any failure so the caller can log
    it as a distinct status rather than a real outcome (spec section 11).
    """
    CFG.require_signalwire()
    CFG.require_public_url()
    from_num = from_number or CFG.signalwire_from_number
    if not from_num:
        raise DialError("No SignalWire from-number configured "
                        "(SIGNALWIRE_FROM_NUMBER / SIGNALWIRE_FROM_NUMBERS empty).")
    should_record = CFG.record_calls if record is None else record

    params: list[tuple[str, str]] = [
        ("To", to_number),
        ("From", from_num),
        ("Url", CFG.callback_url("ivr/start")),
        ("Method", "POST"),
        ("StatusCallback", CFG.callback_url("webhooks/status")),
        ("StatusCallbackMethod", "POST"),
        ("MachineDetection", CFG.machine_detection),
        ("AsyncAmd", "true"),
        ("AsyncAmdStatusCallback", CFG.callback_url("webhooks/amd")),
        ("AsyncAmdStatusCallbackMethod", "POST"),
        ("Record", "true" if should_record else "false"),
    ]
    for event in ("initiated", "ringing", "answered", "completed"):
        params.append(("StatusCallbackEvent", event))
    if should_record:
        params.append(("RecordingStatusCallback", CFG.callback_url("webhooks/recording")))
        params.append(("RecordingStatusCallbackMethod", "POST"))
        if CFG.transcribe_calls:
            # SignalWire only documents Transcribe/TranscribeCallback on the
            # <Record> verb, not this REST Calls resource -- being tested live
            # whether it also applies to whole-call recording (2026-09-20/21).
            params.append(("Transcribe", "true"))
            params.append(("TranscribeCallback", CFG.callback_url("webhooks/transcription")))

    data = _post("Calls.json", params)
    sid = data.get("sid")
    if not sid:
        raise DialError(f"SignalWire response had no call sid: {data!r}")
    log.info("Placed call %s -> %s", sid, to_number)
    return sid


def get_answered_by(call_sid: str) -> str:
    """Best-effort direct REST read of SignalWire's own AnsweredBy for this
    call -- never raises, "" on any failure or if blank there too.

    Real incident 2026-09-28: 911 Restoration test call, AMD genuinely
    computed 'machine_start' on SignalWire's side (confirmed via this exact
    endpoint), but the AsyncAmdStatusCallback webhook never reached
    /webhooks/amd at all -- no restart, no signature rejection logged,
    it simply never arrived. A durable checkpoint can't recover a signal
    that was never received by any process in the first place, so this is
    a different, complementary mitigation: ask SignalWire directly for the
    answer it already computed, instead of only ever waiting on it to push
    that answer to us. Called from _compute_outcome as the final fallback,
    after checking rec.answered_by and the checkpoint -- only on the
    already-rare blank-answered_by path, not on every call. A plain
    read-only GET against the Call resource; not audio-minute billed."""
    try:
        url = f"{_base_url()}/Calls/{call_sid}.json"
        req = urllib.request.Request(url, headers={"Authorization": _auth_header()})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return (data.get("answered_by") or "").strip()
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("get_answered_by REST fallback failed for call_sid=%s: %s", call_sid, e)
        return ""


def hang_up(call_sid: str) -> None:
    """Best-effort immediate hangup once an outcome is known."""
    try:
        _post(f"Calls/{call_sid}.json", [("Status", "completed")])
        log.info("Hung up call %s", call_sid)
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("Could not hang up call %s: %s", call_sid, e)

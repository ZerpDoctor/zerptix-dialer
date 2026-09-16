"""Thin wrapper around the Twilio REST client for placing one outbound call."""
from __future__ import annotations

import logging

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

from .config import CFG

log = logging.getLogger(__name__)


class DialError(RuntimeError):
    """A call could not be placed. Carries a human-readable reason."""


def _client() -> Client:
    CFG.require_twilio()
    return Client(CFG.twilio_account_sid, CFG.twilio_auth_token)


def place_call(to_number: str) -> str:
    """Place one outbound call with async AMD enabled. Returns the CallSid.

    Raises DialError with a clear message on any Twilio API failure so the
    caller can log it as a distinct status rather than a real outcome
    (spec section 11).
    """
    CFG.require_public_url()
    client = _client()

    try:
        call = client.calls.create(
            to=to_number,
            from_=CFG.twilio_from_number,
            url=CFG.callback_url("ivr/start"),
            method="POST",
            status_callback=CFG.callback_url("webhooks/status"),
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            machine_detection=CFG.machine_detection,
            async_amd="true",
            async_amd_status_callback=CFG.callback_url("webhooks/amd"),
            async_amd_status_callback_method="POST",
            record=CFG.record_calls,
            recording_status_callback=(
                CFG.callback_url("webhooks/recording") if CFG.record_calls else None
            ),
            recording_status_callback_method="POST",
        )
    except TwilioRestException as e:
        raise DialError(
            f"Twilio rejected the call (code {e.code}): {e.msg}. "
            f"Common causes: insufficient balance, unverified 'to' number on a "
            f"trial account, bad number format, or rate limiting."
        ) from e
    except Exception as e:  # network, auth, etc.
        raise DialError(f"Twilio call placement failed: {e}") from e

    log.info("Placed call %s -> %s", call.sid, to_number)
    return call.sid


def hang_up(call_sid: str) -> None:
    """Best-effort immediate hangup once an outcome is known."""
    try:
        _client().calls(call_sid).update(
            twiml="<Response><Hangup/></Response>"
        )
        log.info("Hung up call %s", call_sid)
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("Could not hang up call %s: %s", call_sid, e)

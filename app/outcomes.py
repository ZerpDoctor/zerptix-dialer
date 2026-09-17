"""Map call signals to a single outcome label.

Taxonomy used so far:
  answered / voicemail / no_answer / busy / disconnected / extended_hold /
  ivr_unresolved / unknown / gatekeeping_miss / alt_miss

`unknown` and `ivr_unresolved` are deliberate: per spec section 4, an honestly
flagged "we couldn't tell" is better than a guessed outcome that silently
corrupts the coverage data.

gatekeeping_miss vs alt_miss: gatekeeping_miss is an automated prompt demanding
the CALLER's own information (zip code, account number, name) with no digit
escape -- a dead end because there's nothing to give it. alt_miss is an
automated message redirecting the caller to a DIFFERENT CONTACT CHANNEL
entirely (text, email, website) instead of connecting them on this call -- a
dead end not because it wants information, but because this call itself was
never going to connect them. Both are decisive misses with no digit path.
"""
from __future__ import annotations

# Terminal Twilio call statuses that resolve an outcome without needing AMD.
STATUS_MAP = {
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "disconnected",
    "canceled": "disconnected",
}

ALL_OUTCOMES = {
    "answered",
    "voicemail",
    "no_answer",
    "busy",
    "disconnected",
    "extended_hold",
    "ivr_unresolved",
    "unknown",
    "gatekeeping_miss",
    "alt_miss",
}


def from_call_status(call_status: str | None) -> str | None:
    return STATUS_MAP.get((call_status or "").strip().lower())


def from_amd(answered_by: str | None) -> str | None:
    ab = (answered_by or "").strip().lower()
    if ab == "human":
        return "answered"
    if ab.startswith("machine") or ab == "fax":
        return "voicemail"
    return None


def resolve(call_status: str | None, answered_by: str | None) -> str:
    """Non-IVR path: status short-circuits, else AMD verdict, else unknown."""
    by_status = from_call_status(call_status)
    if by_status:
        return by_status
    by_amd = from_amd(answered_by)
    if by_amd:
        return by_amd
    return "unknown"


def is_terminal_status(call_status: str | None) -> bool:
    status = (call_status or "").strip().lower()
    return status in STATUS_MAP or status == "completed"

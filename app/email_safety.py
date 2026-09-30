"""email_safe: may this Calls row be used to tell a company "we called and ..."?

Classification and action are different questions. An outcome is our best read
of what happened; an email is a claim made to a real business, and a wrong
claim costs credibility (2026-09-30: Rare Restoration was told nobody answered
their emergency line; their mitigation manager had answered, and we had hung
up). This is a deliberately conservative filter over a Calls row, computed from
what is IN the row only (outcome, digits pressed, transcript, notes), so it
can be re-run on any historical row.

    yes     strong evidence -- safe to use as a miss
    review  plausible, but has a specific risk; a person should read/listen first
    no      do not email from this row (not a miss, or not provable)

The reason says why, so the Sheet can be filtered on it.
"""
from __future__ import annotations

# Outcomes that are never an email target.
_NOT_A_MISS = {
    "answered": "a person answered -- not a miss",
    "unknown": "we don't know what happened",
    "ivr_unresolved": "the call was never resolved (menu unfinished / press unconfirmed)",
    "extended_hold": ("extended_hold is never a confirmed miss: we hung up while still waiting, "
                      "and a person may have answered after"),
    "disconnected": "dead number -- do not email from it (do_not_call)",
    "busy": "line busy -- not proof nobody would answer",
}

# Explicit voicemail wording (a hold or menu never says these).
_VOICEMAIL_PHRASES = (
    "leave a message", "leave us a message", "leave your name", "leave a detailed message",
    "leave a brief message", "at the tone", "after the tone", "after the beep", "record your message",
    "voicemail", "voice mail", "voice mailbox", "mailbox", "unable to take your call",
    "can't take your call", "cannot take your call", "not available to take your call",
    "forwarded to voice",
)

# Notes that mean the evidence behind this row is compromised.
_UNRELIABLE = ("evidence unreliable",)
_OUTAGE = ("anthropic unavailable", "anthropic api error", "credit balance")


def assess(row: dict) -> tuple[str, str]:
    outcome = (row.get("outcome") or "").strip().lower()
    digits = [d for d in (row.get("digits_sent") or "").split(",") if d.strip()]
    transcript = (row.get("ivr_transcript") or "").lower()
    notes = (row.get("notes") or "").lower()

    if outcome in _NOT_A_MISS:
        return "no", f"{outcome}: {_NOT_A_MISS[outcome]}"
    if outcome not in ("voicemail", "alt_miss", "gatekeeping_miss", "no_answer"):
        return "no", f"outcome {outcome or '(blank)'} is not a recognised miss"

    if any(m in notes for m in _UNRELIABLE):
        return "no", "the row itself says its evidence is unreliable"

    # A second digit means the final route is unconfirmed (Rare Restoration
    # pressed 1 then 2 and ended up on the wrong line).
    if len(digits) >= 2:
        if outcome == "voicemail":
            return "no", f"digits {','.join(digits)} pressed: the final route is unconfirmed"
        return "review", f"digits {','.join(digits)} pressed: the final route is unconfirmed"

    outage = any(m in notes for m in _OUTAGE)

    if outcome == "voicemail":
        if not any(p in transcript for p in _VOICEMAIL_PHRASES):
            return "no", "no explicit voicemail wording in the transcript"
        if digits:
            return "review", (f"pressed {digits[0]} first: confirm the voicemail came AFTER the press, "
                              "not from the menu's own 'leave a message' wording")
        if outage:
            return "review", "the classifier was unavailable for part of this call"
        return "yes", "voicemail greeting heard"

    if outcome == "alt_miss":
        if digits:
            return "review", f"pressed {digits[0]} before the redirect was heard"
        if outage:
            return "review", "the classifier was unavailable for part of this call"
        return "yes", "an alternative contact (text/email/number) was given"

    if outcome == "gatekeeping_miss":
        if digits:
            return "review", f"pressed {digits[0]} before the demand was heard"
        return "yes", "the line demands information before connecting"

    # no_answer: the telephony layer itself reported nobody picked up.
    return "yes", "rang out with no answer (reported by the carrier)"

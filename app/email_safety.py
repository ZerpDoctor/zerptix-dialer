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


import difflib
import re

# Words every restoration company has; a greeting that shares only these with the Queue name says nothing about identity.
_GENERIC_NAME_WORDS = {
    "restoration", "restorations", "restore", "restores", "services", "service", "construction", "cleaning", "company",
    "group", "water", "fire", "mold", "mould", "damage", "environmental", "property", "disaster", "recovery", "remediation",
    "contractors", "contracting", "general", "solutions", "systems", "emergency", "repair", "repairs", "building", "home",
    "homes", "inc", "llc", "corp", "the", "and", "of", "pros", "professional", "professionals", "specialists", "experts",
    "mitigation", "reconstruction", "renovation", "remodeling", "national", "american", "advanced", "complete", "first",
}
_NAMING_CUE = re.compile(
    r"(?:thank(?:s| you) for (?:calling|choosing|contacting)|you(?:'ve| have) reached|welcome to)\s+([a-z0-9&'-]+(?: [a-z0-9&'-]+){0,4})")
_NOT_A_NAME = {"us", "our", "the", "a", "an", "my", "this", "your", "please", "office", "one", "leave", "we", "i", "if", "for",
               "voicemail", "voice", "mailbox", "now", "today", "at", "to", "in", "and", "all"}


def greeting_names_other_business(company: str, transcript: str) -> bool:
    """True if the recording introduces itself with a business NAME ("thank you for calling X") and none of the Queue
    company's distinctive name words appear anywhere in what was heard, allowing for speech-to-text garbling and for
    the name being run together. 2026-10-07: Environmental Protective Solutions -> "Home Insights Home Inspections" and
    Storm Damage Services -> "Red Dirt Sanitation" were both logged as proven misses for companies we never reached
    (wrong Clay phone numbers)."""
    t = (transcript or "").lower()
    named = False
    for m in _NAMING_CUE.finditer(t):
        words = m.group(1).split()
        if words and words[0] not in _NOT_A_NAME:
            named = True
            break
    if not named:
        return False
    toks = [w for w in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(w) >= 3 and w not in _GENERIC_NAME_WORDS and w != "new"]
    if not toks:
        return False
    words = re.findall(r"[a-z0-9]+", t)
    squashed = "".join(words)
    for tok in toks:
        if tok in squashed:
            return False
        for w in words:
            if len(tok) >= 4 and len(w) >= 3 and difflib.SequenceMatcher(None, tok, w).ratio() >= 0.6:
                return False
    return True


def assess(row: dict) -> tuple[str, str]:
    outcome = (row.get("outcome") or "").strip().lower()
    digits = [d for d in (row.get("digits_sent") or "").split(",") if d.strip()]
    transcript = (row.get("ivr_transcript") or "").lower()
    notes = (row.get("notes") or "").lower()

    # A Queue prospect's row always carries its company name (it is looked up from
    # the Queue by phone). No name = a manual or test call (2026-09-30: 56 such rows
    # from franchise/own-cell tests, 4 of them would have read as "yes").
    if "company_name" in row and not (row.get("company_name") or "").strip():
        return "no", "no company name: a manual/test call, not a Queue prospect"

    if any(p in transcript for p in ("does not accept solicitation", "doesn't accept solicitation", "do not accept solicitation",
                                     "no solicitation", "no soliciting", "not accept any solicitation")):
        return "no", "do not contact: the number says it does not accept solicitation calls (do_not_call)"

    if outcome in ("voicemail", "alt_miss", "gatekeeping_miss", "extended_hold") and greeting_names_other_business(
            row.get("company_name") or "", row.get("ivr_transcript") or ""):
        return "review", "the greeting names a different business than the Queue row -- the phone number may be wrong"

    # Owner policy 2026-10-06 (AllPro Restoration & Janitorial: pressed the emergency option, then 60s of nothing): a call
    # that used the FULL hold budget and was hung up by us as normal is a proven miss -- 60 seconds is the standard we hold
    # every company to. Not when our own tooling failed (classifier outage, dead stream) or a second press makes the final
    # route unconfirmed (Rare Restoration), and never when the call ended early (that is ivr_unresolved, not a hold).
    if outcome == "extended_hold":
        try:
            _dur = int(float(row.get("duration_sec") or 0))
        except (TypeError, ValueError):
            _dur = 0
        # The budget is used when the master timer fired, or WE ended the call (we only hang up on a hold when the
        # budget is spent), or the call ran the full length. A far end that dropped early is not a full hold.
        if not ("hit 60s master timer" in notes or "ended by: us" in notes or _dur >= 58):
            return "no", "extended_hold: the hold budget was not fully used"
        if any(m in notes for m in _UNRELIABLE + _OUTAGE) or "stream: never connected" in notes or "stream: error" in notes:
            return "no", "extended_hold: our own tooling was impaired during this call"
        if len(digits) >= 2:
            return "review", f"digits {','.join(digits)} pressed: the final route is unconfirmed"
        if digits:
            return "yes", f"pressed {digits[0]}, then waited the full 60s hold budget and nobody answered"
        return "yes", "waited the full 60s hold budget and nobody answered"

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

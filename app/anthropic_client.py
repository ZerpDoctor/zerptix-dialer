"""Single-purpose Claude Haiku call: given an IVR menu transcript, which digit?

Per spec section 4 the response is structured:
    {is_menu, digit, confidence: "high"|"low", clear_choice, reasoning}

Any failure -- missing key, auth error, 429/billing, timeout, bad JSON -- raises
AnthropicUnavailable with a clear message. Callers MUST catch it, log it loudly
(the user's key may have a billing problem, not a code bug), and fall back to the
keyword-priority logic in app/ivr.py.
"""
from __future__ import annotations

import json
import logging
import re

from .config import CFG

log = logging.getLogger(__name__)

_SYSTEM = (
    "You classify a single automated phone (IVR) menu transcript for a dialer. "
    "The transcript may be imperfect speech-to-text and may include a recording "
    "disclosure before the menu. Decide whether it contains an actual menu with "
    "numbered options, and if so which single DTMF key to press to reach a live "
    "person for a business/service call -- preferring an emergency or after-hours "
    "option if one is offered, otherwise the option closest to 'speak to a "
    "representative' / 'current customer'. Never default to 1. "
    "Reply with ONLY a JSON object, no prose, with keys: "
    'is_menu (bool), digit (string like "1", "0", "*", "#", or null), '
    'confidence ("high" or "low"), clear_choice (bool: true only if one option '
    "is clearly correct; false if you had to guess among ambiguous options), "
    "reasoning (short string)."
)

_GATEKEEPING_SYSTEM = (
    "You classify a single automated phone transcript for an outbound dialer. "
    "The transcript may be imperfect speech-to-text and may include a recording "
    "disclosure. Decide whether this is an AUTOMATED system prompt demanding the "
    "caller provide identifying information -- name, zip code, account number, "
    "date of birth, reason for calling, member/policy/order/case number, or "
    "similar -- with NO digit (DTMF) option offered to press instead. This is "
    "'gatekeeping': a dead end for an outbound caller with nothing to enter. "
    "It is NOT gatekeeping if: (a) the prompt offers any digit to press ('press 1 "
    "for...', 'dial 2 to...') -- that is a normal menu, set has_digit_option true "
    "and is_gatekeeping false; (b) this sounds like natural live-human conversation "
    "rather than a scripted/robotic prompt (a real receptionist asking a normal "
    "question is not gatekeeping). When genuinely unsure, prefer confidence low. "
    "Reply with ONLY a JSON object, no prose, with keys: "
    "is_gatekeeping (bool), has_digit_option (bool), "
    'confidence ("high" or "low"), reasoning (short string).'
)

_ALT_CONTACT_SYSTEM = (
    "You classify a single automated phone transcript for an outbound dialer. "
    "The transcript may be imperfect speech-to-text and may include a recording "
    "disclosure. Decide whether this is an AUTOMATED message that will NOT "
    "connect the caller to anyone on THIS call, regardless of what they do "
    "next -- either by redirecting to a DIFFERENT CONTACT CHANNEL entirely "
    "('please text this number', 'email us at...', 'visit our website', 'for "
    "immediate assistance please text/email/visit...'), OR by two other "
    "confirmed dead-end patterns that are the same underlying problem: "
    "(1) Google Voice / personal call-screening language -- 'please state "
    "your name [and reason for calling], after the tone/beep, Google Voice "
    "(or similar) will try to connect you' -- a SEPARATE screening step "
    "decides later whether to connect, not this call, so this counts as "
    "alt_contact even though no text/email/website is mentioned; "
    "(2) an automated company directory that dead-ends on requiring the "
    "caller to already know a specific person's extension ('if you know "
    "your party's extension, please dial it now') with no other option "
    "offered to reach a live person or leave a message -- an unknown caller "
    "has no way to proceed. "
    "This is 'alt_miss': the call itself was never going to connect the caller "
    "to anyone, regardless of what they do next. "
    "It is NOT alt_miss if: (a) the prompt offers any digit to press ('press 1 "
    "for...', 'dial 2 to...') -- that is a normal menu, set has_digit_option "
    "true and is_alt_contact false, even if a phone number, text line, "
    "website, or extension-dialing is ALSO mentioned; (b) this is a request "
    "for the CALLER's own information (zip code, account number, name) -- "
    "that is gatekeeping, a different category, not alt_contact; (c) this "
    "sounds like natural live-human conversation actively engaging with the "
    "caller (e.g. 'let me check if she's available', 'I'll see if he's in') "
    "rather than a scripted/robotic prompt -- a live person present and "
    "about to go check on someone is answered, not alt_contact, even if they "
    "also ask the caller to state their name and reason for calling. When "
    "genuinely unsure, prefer confidence low. Reply with ONLY a JSON object, "
    "no prose, with keys: is_alt_contact (bool), has_digit_option (bool), "
    'confidence ("high" or "low"), reasoning (short string).'
)

_CALL_AUDIO_SYSTEM = (
    "You classify what actually happened on an outbound phone call for a dialer, "
    "based on a transcript captured after the call connected (no digit-menu was "
    "involved, or this is the audio captured after IVR menu navigation finished). "
    "The transcript may be short, garbled by imperfect speech-to-text, or just a "
    "word or two. Decide whether: "
    "(a) a LIVE HUMAN spoke -- a real person answering, greeting the caller, "
    "identifying themselves or their business, asking a question, or reacting to "
    "silence (e.g. repeating 'hello' -- a person doing that IS a live human, not "
    "a machine, since the dialer never speaks back); "
    "(b) this is a MACHINE/RECORDING -- a voicemail greeting, 'leave a message', "
    "an automated business-status or after-hours announcement, or similar scripted "
    "playback; or "
    "(c) this is HOLD language -- 'please hold', queue/wait music description, "
    "'your call is important to us'. "
    "Do not assume length correlates with which is true -- a live person's greeting "
    "can be short, and a machine's message can be short too. A transcript that is "
    "JUST a business name and nothing else (e.g. 'Phoenix Flood and Fire', 'Apex "
    "Restoration') is a LIVE HUMAN answering with HIGH confidence, not an ambiguous "
    "or low-signal case -- real businesses very commonly answer the phone with only "
    "their name. Only drop to low confidence for a bare name/greeting fragment if "
    "there is actual competing signal toward voicemail (e.g. 'leave a message', "
    "'is not available', 'at the tone', 'mailbox') -- the mere absence of a question "
    "or more words is not itself a reason for low confidence. Conversely, a transcript "
    "that is ONLY a generic call-recording/legal disclosure (e.g. 'this call may be "
    "recorded for quality assurance purposes', 'this call may be monitored') with "
    "NOTHING else -- no greeting, no name, no business identification, no question, "
    "and no voicemail-specific language ('leave a message', 'mailbox', 'is not "
    "available') -- is a textbook LOW-confidence case: that exact phrasing is used "
    "equally by live-human-answered businesses (often played automatically right "
    "before a human picks up) and fully automated systems, so it carries no real "
    "directional signal by itself. Do not pick a direction just because the sentence "
    "sounds formal or business-like; report confidence low and let AMD/keyword "
    "fallback decide. When the transcript truly gives no real signal either way, "
    "prefer confidence low rather than guessing. "
    "A phrase telling the caller to state their name/reason and that someone "
    "will check or try to connect them (e.g. 'record your name and reason for "
    "calling, I'll see if this person is available', 'let me check if she's "
    "available') is a LIVE HUMAN screening the call with HIGH confidence, not "
    "voicemail -- 'I'll see', 'let me check' is a present-tense action only a "
    "person physically present right now can take; a static recording cannot "
    "decide to go check on someone. Do not classify this as voicemail just "
    "because it also asks the caller to state their name and reason, which by "
    "itself can sound scripted -- the active, present-tense checking action is "
    "the deciding signal. "
    "Reply with ONLY a JSON object, no prose, with keys: "
    "is_human (bool), is_voicemail (bool), is_hold (bool), "
    'confidence ("high" or "low"), reasoning (short string).'
)


class AnthropicUnavailable(RuntimeError):
    """The Anthropic API call could not be completed or returned unusable data."""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I | re.M).strip()
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AnthropicUnavailable(f"no JSON object in model reply: {text[:200]!r}")
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AnthropicUnavailable(f"model reply was not valid JSON: {e}") from e


def _call_haiku(system: str, transcript: str) -> dict:
    """Shared plumbing: one single-purpose Haiku call, raising AnthropicUnavailable
    on any failure mode (missing key, auth, billing, timeout, bad JSON)."""
    if not CFG.anthropic_api_key:
        raise AnthropicUnavailable("ANTHROPIC_API_KEY is not set")

    try:
        import anthropic
    except ImportError as e:
        raise AnthropicUnavailable("anthropic package not installed") from e

    client = anthropic.Anthropic(api_key=CFG.anthropic_api_key, max_retries=1, timeout=12.0)

    try:
        resp = client.messages.create(
            model=CFG.anthropic_model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": f"Transcript:\n\"\"\"\n{transcript}\n\"\"\""}],
        )
    except Exception as e:  # noqa: BLE001 - normalize every failure mode
        # anthropic.APIStatusError / AuthenticationError / RateLimitError /
        # APIConnectionError / APITimeoutError all land here.
        status = getattr(e, "status_code", None)
        raise AnthropicUnavailable(
            f"{type(e).__name__}"
            + (f" (HTTP {status})" if status else "")
            + f": {getattr(e, 'message', str(e))}"
        ) from e

    text = "".join(
        getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"
    )
    return _extract_json(text)


def classify_ivr_digit(transcript: str) -> dict:
    """Returns {is_menu, digit, confidence, clear_choice, reasoning}.
    Raises AnthropicUnavailable on any problem."""
    data = _call_haiku(_SYSTEM, transcript)

    if "is_menu" not in data:
        raise AnthropicUnavailable(f"reply missing 'is_menu': {data!r}")

    digit = data.get("digit")
    if digit is not None:
        digit = str(digit).strip()
        if digit not in {*(str(n) for n in range(10)), "*", "#"}:
            raise AnthropicUnavailable(f"reply has invalid digit {digit!r}")

    return {
        "is_menu": bool(data.get("is_menu")),
        "digit": digit,
        "confidence": str(data.get("confidence", "low")).lower(),
        "clear_choice": bool(data.get("clear_choice", False)),
        "reasoning": str(data.get("reasoning", ""))[:300],
    }


def classify_gatekeeping(transcript: str) -> dict:
    """Returns {is_gatekeeping, has_digit_option, confidence, reasoning}.
    Raises AnthropicUnavailable on any problem -- callers must NOT fall back to
    a keyword-only verdict here (unlike classify_ivr_digit's keyword-priority
    fallback): a false-positive hangs up and burns the quarter's remaining
    attempts, so an unavailable classifier means "not gatekeeping", not a guess.
    """
    data = _call_haiku(_GATEKEEPING_SYSTEM, transcript)

    if "is_gatekeeping" not in data:
        raise AnthropicUnavailable(f"reply missing 'is_gatekeeping': {data!r}")

    return {
        "is_gatekeeping": bool(data.get("is_gatekeeping")),
        "has_digit_option": bool(data.get("has_digit_option")),
        "confidence": str(data.get("confidence", "low")).lower(),
        "reasoning": str(data.get("reasoning", ""))[:300],
    }


def classify_alt_contact(transcript: str) -> dict:
    """Returns {is_alt_contact, has_digit_option, confidence, reasoning}.
    Raises AnthropicUnavailable on any problem -- same reasoning as
    classify_gatekeeping: a false positive hangs up and burns the quarter's
    remaining attempts, so an unavailable classifier means "not alt_contact",
    not a keyword-only guess.
    """
    data = _call_haiku(_ALT_CONTACT_SYSTEM, transcript)

    if "is_alt_contact" not in data:
        raise AnthropicUnavailable(f"reply missing 'is_alt_contact': {data!r}")

    return {
        "is_alt_contact": bool(data.get("is_alt_contact")),
        "has_digit_option": bool(data.get("has_digit_option")),
        "confidence": str(data.get("confidence", "low")).lower(),
        "reasoning": str(data.get("reasoning", ""))[:300],
    }


def classify_call_audio(transcript: str) -> dict:
    """Returns {is_human, is_voicemail, is_hold, confidence, reasoning}.
    Raises AnthropicUnavailable on any problem. Used to judge what really
    happened on a call from its transcript -- the transcript is the primary
    signal (mirroring how menu calls already trust the transcript over AMD),
    with AMD only consulted as a fallback when this is unavailable or the
    transcript gives no usable signal. See app/ivr.py::decide_tail.
    """
    data = _call_haiku(_CALL_AUDIO_SYSTEM, transcript)

    if not any(k in data for k in ("is_human", "is_voicemail", "is_hold")):
        raise AnthropicUnavailable(f"reply missing expected keys: {data!r}")

    return {
        "is_human": bool(data.get("is_human")),
        "is_voicemail": bool(data.get("is_voicemail")),
        "is_hold": bool(data.get("is_hold")),
        "confidence": str(data.get("confidence", "low")).lower(),
        "reasoning": str(data.get("reasoning", ""))[:300],
    }

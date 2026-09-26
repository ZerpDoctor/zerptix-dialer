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
import threading

from .config import CFG

log = logging.getLogger(__name__)

# Real incident 2026-09-25/26: _call_haiku used to construct a brand-new
# anthropic.Anthropic(...) -- and with it, a brand-new httpx.Client and its
# own connection pool -- on every single classification call, never closed.
# This function fires multiple times per call (menu look, digit choice,
# gatekeeping, alt-contact, tail read), across many concurrent calls, so
# these piled up faster than garbage collection reclaimed them -- traced via
# Railway's own gunicorn arbiter log ("Worker was sent SIGKILL! Perhaps out
# of memory?") recurring even at a concurrent-call count already confirmed
# safe, meaning the real driver was classification-request volume, not call
# count. A single shared client, built once and reused, is what the SDK
# (and the httpx.Client it wraps) is actually designed for -- both are
# documented safe for concurrent use across threads. Lazy singleton instead
# of a module-level eager instantiation so the existing "raise
# AnthropicUnavailable if the key isn't set" behavior below is unaffected.
_client: "anthropic.Anthropic | None" = None  # noqa: F821 - imported lazily below
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import anthropic
                _client = anthropic.Anthropic(
                    api_key=CFG.anthropic_api_key, max_retries=1, timeout=12.0,
                )
    return _client

_SYSTEM = (
    "You classify a single automated phone (IVR) menu transcript for a dialer. "
    "The transcript may be imperfect speech-to-text and may include a recording "
    "disclosure before the menu. Decide whether it contains an actual menu with "
    "numbered options, and if so which single DTMF key to press to reach a live "
    "person for a business/service call -- preferring an emergency or after-hours "
    "option if one is offered, otherwise the option closest to 'speak to a "
    "representative' / 'current customer'. Never default to 1. "
    "Also decide is_emergency_option: true ONLY if the digit you chose is itself "
    "the option explicitly labeled as emergency/urgent/after-hours/24-hour/on-call "
    "(e.g. 'press 1 for emergency service') -- false if you chose a different, "
    "non-emergency option, even if the word 'emergency' appears somewhere else in "
    "the transcript for a DIFFERENT digit than the one you chose, and false "
    "whenever digit is null. "
    "Reply with ONLY a JSON object, no prose, with keys: "
    'is_menu (bool), digit (string like "1", "0", "*", "#", or null), '
    'confidence ("high" or "low"), clear_choice (bool: true only if one option '
    "is clearly correct; false if you had to guess among ambiguous options), "
    "is_emergency_option (bool), reasoning (short string)."
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
    "immediate assistance please text/email/visit...', 'if this is an "
    "emergency please call my cell at...', 'you can reach me directly at "
    "[a different number]...', 'please hang up and call our emergency line "
    "at...' -- personal ('my cell') and organizational ('our emergency "
    "line') framing both count identically: a redirect to ANOTHER PHONE "
    "NUMBER is the same as a text/email/website redirect either way -- the "
    "caller will not reach anyone by continuing on THIS call), OR by two "
    "other confirmed "
    "dead-end patterns that are the same underlying problem: "
    "(1) AUTOMATED call-screening language -- Google Voice or similar "
    "services generate phrasing like 'please state your name [and reason "
    "for calling]', 'after the tone/beep, [Google Voice / this system] will "
    "try to connect you', 'I'll see if this person is available', 'let me "
    "check if she's available' -- these SOUND conversational but are a "
    "scripted, fully-automated screening system, never a live person; there "
    "is no live human on real-time outbound dialer calls saying this. A "
    "SEPARATE screening step decides later whether to connect, not this "
    "call, so this counts as alt_contact even though no text/email/website "
    "is mentioned and even though the phrasing sounds like a present, "
    "active person; "
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
    "for the CALLER's own information (zip code, account number, name) with "
    "no screening/connect-later language attached -- that is gatekeeping, a "
    "different category, not alt_contact. When genuinely unsure, prefer "
    "confidence low. Reply with ONLY a JSON object, no prose, with keys: "
    "is_alt_contact (bool), has_digit_option (bool), "
    'confidence ("high" or "low"), reasoning (short string).'
)

_CALL_AUDIO_SYSTEM = (
    "You classify what actually happened on an outbound phone call for a dialer, "
    "based on a transcript captured after the call connected (no digit-menu was "
    "involved, or this is the audio captured after IVR menu navigation finished). "
    "The transcript may be short, garbled by imperfect speech-to-text, or just a "
    "word or two. Decide whether: "
    "(a) a LIVE HUMAN OR A RESPONSIVE AI VOICE AGENT spoke -- a real person, or an "
    "interactive AI assistant that behaves like a live answer: greeting the caller, "
    "identifying the business, engaging with what would come next, or reacting to "
    "silence/dead air (e.g. repeating 'hello', or 'I think the signal dropped, are "
    "you there?' -- reacting to unexpected silence IS a live/responsive answer, not "
    "a dead-end recording, since the dialer never speaks back -- this applies "
    "equally whether the speaker is a person or an AI agent). The system explicitly "
    "calling itself an 'AI assistant' or 'automated' does NOT by itself make it "
    "MACHINE/RECORDING (b) -- the discriminator is whether it is actually reactive "
    "(responds to the conversation, notices dead air) versus a fixed one-way script "
    "that plays the same way regardless of what the caller does; "
    "(b) this is a MACHINE/RECORDING -- a voicemail greeting, 'leave a message', "
    "an automated business-status or after-hours announcement, or similar scripted "
    "playback that never reacts to the caller; or "
    "(c) this is HOLD language -- 'please hold', queue/wait music description, "
    "'your call is important to us'. "
    "Do not assume length correlates with which is true -- a live person's greeting "
    "can be short, and a machine's message can be short too. A transcript that is "
    "JUST a business name and nothing else (e.g. 'Phoenix Flood and Fire', 'Apex "
    "Restoration') is a LIVE HUMAN answering with HIGH confidence, not an ambiguous "
    "or low-signal case -- real businesses very commonly answer the phone with only "
    "their name. If an expected company name is given below and the transcript is a "
    "short fragment that plausibly matches it (even a partial/garbled match, e.g. "
    "'deep water' for 'Deep Water Emergency Services'), treat that the same as a "
    "bare business name -- a truncated capture of the same live answer, not a reason "
    "for low confidence. Only drop to low confidence for a bare name/greeting fragment "
    "if there is actual competing signal toward voicemail (e.g. 'leave a message', "
    "'is not available', 'at the tone', 'mailbox') -- the mere absence of a question "
    "or more words is not itself a reason for low confidence. A bare 'thank you for "
    "calling' with NOTHING else -- no name, no question, no further content -- is "
    "NOT the same as a bare business name: that exact opener is used equally by a "
    "live pickup ('thank you for calling, this is Dana') and a voicemail/after-hours "
    "greeting ('thank you for calling, we are currently closed...') that just got cut "
    "off before the rest played -- report low confidence, not high, when nothing "
    "after it reveals which one this was. Conversely, a transcript "
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
    "A scripted transfer/hold announcement -- 'please hold while I try to connect "
    "you', 'connecting you to our emergency team', 'please hold while we connect "
    "you' -- is HOLD language (c). What follows it right after decides whether a "
    "live human then took over: a COMPLETE, natural greeting or question (e.g. "
    "'how can I help you today', 'how may I direct your call', a personal name) is "
    "real evidence a person picked up -- classify that as human, same as the "
    "2026-09-21 case this rule must not break. But a bare, truncated fragment "
    "missing its own lead-in -- just '...you help you' or '...you how help' glued "
    "directly onto the hold phrase with no 'how can/may I' of its own -- is speech-"
    "to-text bleed from the same announcement repeating or looping, not a distinct "
    "speaker; keep that as HOLD rather than flipping to human on a fragment alone. "
    "The test is whether the words after the hold phrase form their own complete "
    "sentence, not merely whether the word 'help' appears. "
    "A DIFFERENT case, and NOT the fragment-bleed case above: the hold/transfer "
    "announcement followed by a short reaction word said two or more times on its "
    "own -- 'hello hello hello', 'hello hello' -- e.g. 'your call will be answered "
    "shortly ... hello hello hello'. That repetition is a live human who picked up "
    "once the hold ended and is reacting to silence (the dialer never speaks back), "
    "exactly the same 'repeating hello IS a live human' rule from (a) above -- "
    "classify as human, high confidence, real incident 2026-09-24 (Dire "
    "Restoration) got this wrong as HOLD. The discriminator from the fragment-bleed "
    "case: a REPEATED standalone reaction word is a person reacting, while a SINGLE "
    "garbled continuation of the hold sentence itself ('...connect you help you', "
    "no repetition, reads as one broken sentence) is bleed, not a person. "
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


def _call_haiku(system: str, transcript: str, *, extra_context: str = "") -> dict:
    """Shared plumbing: one single-purpose Haiku call, raising AnthropicUnavailable
    on any failure mode (missing key, auth, billing, timeout, bad JSON). extra_context,
    when given, is a line placed before the transcript block (e.g. the expected
    company name, used by classify_call_audio to recognize a truncated name
    fragment -- see _CALL_AUDIO_SYSTEM)."""
    if not CFG.anthropic_api_key:
        raise AnthropicUnavailable("ANTHROPIC_API_KEY is not set")

    try:
        client = _get_client()
    except ImportError as e:
        raise AnthropicUnavailable("anthropic package not installed") from e

    prefix = f"{extra_context}\n\n" if extra_context else ""
    try:
        resp = client.messages.create(
            model=CFG.anthropic_model,
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": f"{prefix}Transcript:\n\"\"\"\n{transcript}\n\"\"\""}],
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
    """Returns {is_menu, digit, confidence, clear_choice, is_emergency_option,
    reasoning}. Raises AnthropicUnavailable on any problem."""
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
        "is_emergency_option": bool(data.get("is_emergency_option")) and digit is not None,
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


def classify_call_audio(transcript: str, company_name: str = "") -> dict:
    """Returns {is_human, is_voicemail, is_hold, confidence, reasoning}.
    Raises AnthropicUnavailable on any problem. Used to judge what really
    happened on a call from its transcript -- the transcript is the primary
    signal (mirroring how menu calls already trust the transcript over AMD),
    with AMD only consulted as a fallback when this is unavailable or the
    transcript gives no usable signal. See app/ivr.py::decide_tail.

    company_name, when known (from the Queue row we dialed), is passed as
    context so a short fragment that's really a truncated capture of the
    business's own name (e.g. transcript 'deep water' for 'Deep Water
    Emergency Services') can be recognized as a bare-name live answer
    instead of scored low-confidence for looking too generic -- real
    incident 2026-09-24, Deep Water Emergency Services.
    """
    extra = f"Expected company name (from caller lookup; may not exactly match): {company_name}" if company_name else ""
    data = _call_haiku(_CALL_AUDIO_SYSTEM, transcript, extra_context=extra)

    if not any(k in data for k in ("is_human", "is_voicemail", "is_hold")):
        raise AnthropicUnavailable(f"reply missing expected keys: {data!r}")

    return {
        "is_human": bool(data.get("is_human")),
        "is_voicemail": bool(data.get("is_voicemail")),
        "is_hold": bool(data.get("is_hold")),
        "confidence": str(data.get("confidence", "low")).lower(),
        "reasoning": str(data.get("reasoning", ""))[:300],
    }

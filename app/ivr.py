"""IVR menu detection and DTMF-digit selection -- keyword heuristics only.

The Anthropic/Haiku call (app/anthropic_client.py) sits on top of this for digit
extraction; everything here is deterministic and works with no network.

Design points from spec section 4:
  - "Is this a menu?" needs menu *structure* (a press/select verb, or an explicit
    "<digit> for" / "for <digit>" pattern), never a bare digit word -- so a live
    human saying "hi, one sec" does not look like a menu.
  - Digit extraction parses the *instructed* digit; it never defaults to 1.
  - Ambiguous menu (multiple options, no single clear emergency/after-hours
    instruction) -> keyword priority: emergency/after-hours wins anywhere in the
    menu, else the option closest to "reach a person", else the lowest digit.
    Whenever that priority logic decides the digit, the row is flagged.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import outcomes
from .anthropic_client import (
    AnthropicUnavailable,
    classify_alt_contact,
    classify_call_audio,
    classify_gatekeeping,
    classify_ivr_digit,
)

log = logging.getLogger(__name__)

# --- word / phrase lists ---------------------------------------------------

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "pound": "#", "hash": "#", "star": "*", "asterisk": "*",
}
_DIGIT_TOKEN = r"(?:[0-9]|zero|one|two|three|four|five|six|seven|eight|nine|pound|hash|star)"
_PRESS_VERB = r"(?:press|select|dial|enter|push|choose|hit|key in)"

# Structural patterns: a press-verb + digit, or a digit tied to "for"/"to".
_STRUCT_PATTERNS = [
    re.compile(_PRESS_VERB + r"\s+(?:the\s+)?(?:number\s+)?" + _DIGIT_TOKEN + r"\b", re.I),
    re.compile(r"\b" + _DIGIT_TOKEN + r"\s+(?:for|to)\s", re.I),
    re.compile(r"\bfor\b[\w\s,'-]{0,40}?" + _PRESS_VERB + r"\s+" + _DIGIT_TOKEN + r"\b", re.I),
]

_MENU_PHRASES = [
    "for emergency", "emergency service", "after hours", "after-hours", "afterhours",
    "to speak with", "to speak to", "speak with a", "speak to a", "speak to someone",
    "to reach", "representative", "customer service", "current customer",
    "existing customer", "new customer", "main menu", "return to the main menu",
    "if you know your party", "party's extension", "party’s extension",
    "para espanol", "para español", "for english", "for spanish", "for sales",
    "for billing", "for support", "for service", "for dispatch", "for scheduling",
    "for the operator", "reach the operator", "touch tone", "touch-tone",
    "remain on the line", "listen carefully as our menu", "menu options have changed",
]

_EMERGENCY_WORDS = [
    "emergency", "urgent", "after hours", "after-hours", "afterhours", "24 hour",
    "24-hour", "24/7", "on call", "on-call", "immediate assistance", "no heat",
    "no water", "gas leak", "no cooling", "outage",
]

_REACH_PERSON_WORDS = [
    "representative", "operator", "receptionist", "front desk", "reception",
    "dispatch", "dispatcher", "speak with", "speak to", "customer service",
    "current customer", "existing customer", "schedule", "scheduling",
    "make an appointment", "book an appointment", "service department",
]

# A voicemail box's OWN recording-control menu ("press 2 to erase and
# re-record...") structurally matches press-verb+digit exactly like a real
# business call-routing IVR, but pressing those digits controls someone's
# answering machine (erase, re-record, send), not a live-person route.
# Confirmed real incident 2026-09-15 (E F Yates Construction): the system
# pressed "erase and re-record" on a real company's voicemail. Never treat
# this language as a navigable menu.
_VOICEMAIL_CONTROL_PHRASES = [
    "erase and re-record", "to erase and re-record", "append this recording",
    "to append this recording", "send this message now", "to replay press",
    "special delivery options", "general mailbox is not available",
    "mailbox is not available", "to review your recording",
    "to listen to your message", "re-record your message",
]

# A transcript that explicitly announces a menu is about to be read out, but
# cuts off before any actual option is heard -- real incident 2026-09-21:
# Epic Restoration's transcript ends at "please choose from 1 of the
# following options" with nothing after it. Haiku confidently read the
# surrounding "offices are currently closed" framing as voicemail and the
# call resolved immediately -- a real (not amd_fallback) classifier, so the
# separate amd_fallback-trust fix doesn't catch this. The bug isn't
# confidence, it's timing: the business just told us more content -- an
# actual menu, possibly with an emergency option -- was coming, and we
# stopped listening right before it arrived. This gates any early
# conclusion in _conclude_not_menu (server.py) so it keeps listening at
# least one more cycle instead of confidently guessing what those options
# might have been.
_MENU_INCOMING_PHRASES = [
    "choose from", "choose one of the following", "select one of the following",
    "following options", "listen carefully as our menu", "here are your options",
    "please listen to the following", "the following menu", "these options",
]


def mentions_incoming_menu(transcript: str) -> bool:
    t = (transcript or "").lower()
    return any(p in t for p in _MENU_INCOMING_PHRASES)


# Post-navigation ("tail") classification.
_TAIL_VOICEMAIL = [
    "leave a message", "leave a detailed message", "leave your name",
    "at the tone", "after the tone", "after the beep", "at the beep",
    "record your message", "please leave", "not available to take your call",
    "unable to take your call", "you have reached the voicemail", "voice mailbox",
    "voicemail box", "mailbox", "is not available", "are not available",
    "our office is closed", "we are currently closed", "please call back during",
    "you've reached", "you have reached the office of",
]
# Subset of _TAIL_VOICEMAIL that unambiguously identifies a personal/generic
# mailbox no matter what follows -- used only to gate looks_like_menu()'s
# struct_hits bypass below (added 2026-09-23). The rest of _TAIL_VOICEMAIL
# ("leave a message", "please leave", etc.) is too generic for that: it
# matches equally well as a standalone voicemail greeting or as a real
# business menu's own option description ("press 2 and leave a message"),
# so it can't safely override genuine menu structure the way these
# stronger, identity-establishing phrases can.
_TAIL_VOICEMAIL_STRONG_IDENTITY = [
    "you have reached the voicemail",
    # NOT "mailbox"/"voicemail box" (moved back out 2026-09-24, real incident:
    # Gerloff Company's real after-hours menu -- "if you need immediate
    # emergency services please press 1 ... press 2 to leave a message in our
    # general voicemail box" -- named its OWN option 2 destination with
    # "voicemail box", which unconditionally short-circuited the whole menu
    # (including the real emergency option 1) before struct_hits was even
    # weighed, same failure shape as the Damage Control "leave a message"
    # incident this file already documents below. Unlike "record your
    # message" (which only ever means the call is ALREADY recording),
    # "mailbox"/"voicemail box" are commonly just the NAME of where option 2
    # goes, so they can't safely win unconditionally -- they still catch a
    # real personal mailbox via the general _TAIL_VOICEMAIL list below, which
    # IS gated on the absence of real menu structure.
    # Added 2026-09-23, same night as the split above: a same-night sweep of
    # every Sep-22 non-menu call against the just-fixed struct_hits gate
    # found two more real transcripts (Flood Pros, Cera Restoration) whose
    # OWN recording-management controls ("record your message ... press 1
    # for more options" / "record your message ... for delivery options
    # press the pound sign") now passed the struct_hits bypass too -- and
    # since looks_like_menu() runs before the alt_contact check in the live
    # turn handler, that would have hijacked Flood Pros' real alt_contact
    # redirect into pressing the voicemail's own "more options" instead.
    # "record your message" (unlike "leave a message", which Damage
    # Control's real menu also uses to describe its OWN option 2) means the
    # call has already entered a recording state -- everything said next is
    # about managing that recording, never a way to reach anyone.
    "record your message",
    # Added 2026-09-25, real incident found via live testing: Biorestoration's
    # voicemail box loops "...you may hang up or press 1 for more options"
    # BEFORE the phrase "record your message" appears (that only shows up in
    # a later loop iteration) -- so at the exact turn the digit decision gets
    # made, struct_hits alone won and pressed 1 into the mailbox's own
    # controls. "hang up or press N for more options" is itself an
    # unambiguous recording-management idiom no real business menu uses (a
    # real menu's options reach departments/people, never "more options"
    # about a recording) -- doesn't need to wait for "record your message"
    # to show up in a later turn to be recognized.
    "hang up or press",
]
_TAIL_ANSWERED = [
    "hello", "hi there", "this is", "speaking", "how can i help",
    "how may i help", "thanks for calling", "thank you for calling",
    "good morning", "good afternoon", "good evening", "how can i direct",
    "what can i do for you", "who am i speaking", "how may i direct",
    "what's your location", "what is your location", "what's the address",
    "what is the address", "what's the emergency", "name and number",
    "go ahead", "you're through to", "how can i assist",
]
# "Digital gatekeeping": an automated prompt demanding identifying information
# with NO digit-press path forward -- distinct from a menu (spec addendum).
# These are deliberately narrow/specific phrases (unlike _MENU_PHRASES, which
# needs 2+ corroborating hits) since each one is a fairly unambiguous signal on
# its own -- a single hit is enough to warrant the Haiku confirmation call.
_GATEKEEPING_PHRASES = [
    "your zip code", "your postal code", "your account number", "your account #",
    "your member id", "your member number", "your customer id", "your customer number",
    "your policy number", "your order number", "your confirmation number",
    "your date of birth", "your social security", "your case number",
    "your ticket number", "your claim number", "your pin number",
    "your verification code", "your full name and", "the reason for your call",
    "the reason you're calling", "reason for calling", "state your name",
    "say your name", "spell your last name", "spell your name",
]

# An automated message redirecting the caller to a DIFFERENT contact channel
# entirely (text, email, website, ANOTHER PHONE NUMBER) instead of connecting
# them on this call -- distinct from gatekeeping (which demands the caller's
# own info) and from a normal menu (which offers a digit to press within this
# same call). Same "single hit is enough to warrant Haiku confirmation"
# reasoning as gatekeeping.
# Also covers two confirmed real dead-end patterns that don't fit the
# text/email/website/phone mold but are the same underlying problem -- this
# call will not connect the caller to anyone, regardless of what they do next:
# (a) automated call-screening (Google Voice or similar) -- "state your
# name, ... will try to connect you" / "I'll see if this person is
# available" -- confirmed 2026-09-18 there is no live screener on these
# calls, it is always a scripted system despite sounding conversational; a
# SEPARATE screening step decides whether to connect later, not this call;
# (b) a company directory that dead-ends on requiring an extension the
# caller has no way of knowing, with no fallback option.
# Phone-redirect phrases added 2026-09-21: missed entirely until a real audit
# caught Ashley's Restoration's voicemail ("if you have an emergency please
# call my cell at ...") logging as plain voicemail -- the pre-filter below
# gated Haiku out before it ever got a chance to judge it, same failure shape
# as a keyword blocklist that doesn't generalize. A call-my-cell redirect is
# structurally identical to a text/email redirect: the caller will not reach
# anyone on THIS call regardless of what they do next.
_ALT_CONTACT_PHRASES = [
    "please text", "text this number", "text us at", "text the word",
    "send us a text", "you can text", "reach us by text",
    # Added 2026-09-25, found via live testing: Ambar Mold's voicemail
    # ("for a quicker response text me your information") uses first-person
    # "text me", the same personal-framing gap the phone-redirect list below
    # already had to fix once ("call my cell" vs. "call our") -- same lesson,
    # applied to text before a real incident needed it to matter.
    "text me",
    # Broadened again 2026-09-22: a THIRD real incident, third phrasing --
    # Water Pro's voicemail ("if this is an emergency send a text message to
    # the same number...") logged as plain voicemail. "send us a text" above
    # doesn't match "send A text message" (no "us"). Same lesson as the
    # phone-redirect broadening two nights ago: stop chasing exact wordings,
    # widen to the shorter fragment that actually generalizes.
    "send a text", "text message to", "to the same number",
    "email us at", "send us an email", "you can email", "email address is",
    "reach us at our email", "for a faster response please email",
    "visit our website", "go to our website", "check out our website",
    "visit us online", "visit us at", "check us out at", "find us online",
    "for immediate assistance please visit", "for immediate assistance please text",
    "for immediate assistance please email",
    "will try to connect you", "try to connect you", "trying to connect you",
    "know your party's extension", "know the extension", "your party's extension",
    "i'll see if", "let me check if", "i will see if",
    "call my cell", "call my mobile", "call my personal", "call my direct",
    "call me at", "please call me", "reach me at", "reach me directly",
    "you can call me", "you can reach me", "call or text me at",
    # Broadened 2026-09-21, same night: a *second* real incident (Gold Star
    # Restoration -- "please hang up and call our emergency line at...")
    # showed the phrase-list-of-exact-wordings approach still doesn't
    # generalize even after the first fix above -- "call OUR emergency line"
    # is organizational framing, not "call MY cell", so none of the phrases
    # above matched it. Widened to shorter, more general fragments that
    # catch the whole redirect-to-another-number family regardless of
    # personal/organizational phrasing, instead of chasing wordings one at a
    # time. Haiku's own is_alt_contact judgment (plus the has_digit_option
    # safety rule) is still the real decision -- these fragments only gate
    # whether it gets consulted, so a broader net here is low-risk.
    "call our", "dial our", "hang up and call", "please hang up and call",
    "call another number", "call a different number", "our emergency line",
    "our direct line", "another line", "a different line",
    # Broadened again 2026-09-23: a FOURTH real incident, fourth phrasing --
    # Flood Pros' voicemail ("...if this is any emergency please give our
    # office a call at 608-756-9300...") logged as plain voicemail. None of
    # the "call our"/"call my"/"call me at" fragments above match "give our
    # office A CALL at" -- the verb+object order is reversed from every
    # phrasing caught so far. decide_tail's own reasoning even named the
    # emergency number as a marker it saw, then filed it under voicemail
    # anyway, because the pre-filter never let is_alt_contact get asked at
    # all. Same shorter-fragment lesson again: "a call at" generalizes over
    # "give our office a call at", "give us a call at", "give me a call at",
    # etc. regardless of who's asking.
    "a call at",
    # Broadened again 2026-09-25: a FIFTH real incident, fifth phrasing, found
    # via live testing -- Cooks Proclean And Restoration's voicemail ("if this
    # is an emergency you can also reach Becky cook at 706-988-2664") named a
    # PERSON in third person, not "me"/"my"/"our" -- none of the phrases above
    # match "reach [a name] at [number]" since they all assume first-person
    # or organizational framing. This call happened to still resolve to
    # voicemail correctly anyway (real voicemail language followed), but the
    # same lesson applies before it doesn't matter on some other call: the
    # actual decision is still Haiku's, gated by has_digit_option, so widen
    # the pre-filter rather than chase this exact wording too. NOT a bare
    # "reach" -- too generic, would gate-in unrelated content like "reach a
    # representative" menu offers on every call.
    "can also reach", "you can reach",
]

_TAIL_HOLD = [
    "please hold", "please continue to hold", "continue to hold",
    "all of our representatives", "all our representatives", "all of our agents",
    "all our agents", "currently assisting other", "your call is important",
    "estimated wait", "next available", "remain on the line", "thank you for holding",
    "your call will be answered", "please stay on the line",
    # Added 2026-09-21, found via a full-history classification sweep: AAA
    # Disaster Recovery's "this is triple a disaster recovery ... connecting
    # you now" is a scripted automated-routing announcement -- Haiku's own
    # primary read correctly said so ("not a human greeting"), but that
    # reasoning gets discarded once its structured verdict lands on
    # "unknown", and classify_tail()'s fallback then matched the generic
    # "this is" phrase in _TAIL_ANSWERED (checked below _TAIL_HOLD) plus a
    # <=12-word guard that this exact transcript happened to satisfy,
    # overriding Haiku's correct read with the wrong one. These phrases are
    # checked first, same as the rest of this list, so a real live "connecting
    # you now, how can I help" still resolves correctly once genuine
    # conversational content follows (that case already goes through Haiku's
    # confident primary path, never reaches this fallback at all).
    "connecting you now", "connecting you", "transferring your call",
    "please hold while we connect", "one moment while we connect",
    # Added 2026-09-23/24, Lake Effect Restoration real incident: Haiku's
    # primary (high-confidence) read wrongly called this a live human because
    # of a garbled trailing "...connect you help you" fragment tacked onto a
    # scripted after-hours transfer announcement (see the matching prompt
    # rule in anthropic_client._CALL_AUDIO_SYSTEM). This keyword-fallback
    # entry is defense-in-depth for the same phrasing when Haiku is
    # unavailable/low-confidence instead of confidently wrong.
    "try to connect you",
]


@dataclass
class MenuLook:
    is_menu: bool
    score: int
    matched: list[str]


@dataclass
class DigitPick:
    digit: str | None
    reason: str
    flagged: bool  # True whenever priority-fallback logic chose the digit
    is_emergency: bool = False  # True only if THIS digit's own label is the
    # emergency/after-hours option -- not just that emergency language
    # appears somewhere else in the transcript for a different digit.


# --- menu detection ------------------------------------------------------------

def looks_like_menu(transcript: str) -> MenuLook:
    t = (transcript or "").lower()
    if not t.strip():
        return MenuLook(False, 0, [])

    # Never treat a voicemail box's own recording-control menu as a
    # navigable business IVR, even though it structurally matches
    # press-verb+digit -- checked before anything else.
    if any(p in t for p in _VOICEMAIL_CONTROL_PHRASES):
        return MenuLook(False, 0, ["voicemail-control-menu-excluded"])

    # _VOICEMAIL_CONTROL_PHRASES above is a narrow, specific-wording
    # blocklist that has now missed 5 separate real incidents with 5
    # different phrasings (most recently: a personal cell's own carrier
    # voicemail -- "press 1 to mark your message urgent" -- got navigated
    # as if it were a business menu, on a REAL PERSON'S PHONE, not a test
    # line; confirmed 2026-09-21). Phrase-blocklisting a voicemail box's
    # near-infinite specific control wordings doesn't generalize and never
    # will. This is the structural fix: an UNAMBIGUOUS "this is a mailbox"
    # identity phrase (_TAIL_VOICEMAIL_STRONG_IDENTITY) always wins over any
    # nearby "press N" -- a personal mailbox's own controls are never a way
    # to reach a person/department, regardless of wording, so this check
    # doesn't need struct_hits to decide anything.
    if any(p in t for p in _TAIL_VOICEMAIL_STRONG_IDENTITY):
        return MenuLook(False, 0, ["voicemail-greeting-already-established"])

    # The REST of _TAIL_VOICEMAIL ("leave a message", "please leave", "our
    # office is closed", etc.) is too generic to apply the same way: real
    # incident 2026-09-23, Damage Control -- "you have reached damage
    # control if this is an emergency press 1 to be connected to our on
    # call service... press 2 and leave a message" is a genuine after-hours
    # business menu WITH a real emergency option, but its own option-2
    # description contains "leave a message" -- the whole menu, emergency
    # option included, got silently suppressed as if the box had already
    # been established as a voicemail from an earlier turn. That assumption
    # only holds when there's no competing menu structure in the very same
    # text; when there is, the generic phrase is far more likely describing
    # what a menu option does than confirming we've already reached a
    # recording. Only bypass on real "press N" structure, not on the
    # unambiguous identity phrases above -- those still always win.
    struct_hits = sum(1 for p in _STRUCT_PATTERNS if p.search(t))
    if not struct_hits and any(p in t for p in _TAIL_VOICEMAIL):
        return MenuLook(False, 0, ["voicemail-greeting-already-established"])

    matched: list[str] = []
    score = 0

    if struct_hits:
        score += 2 * struct_hits
        matched.append(f"menu-structure x{struct_hits}")

    phrase_hits = [p for p in _MENU_PHRASES if p in t]
    score += len(phrase_hits)
    matched.extend(phrase_hits)

    # A menu needs real structure OR several corroborating menu phrases.
    is_menu = struct_hits >= 1 or len(phrase_hits) >= 2
    return MenuLook(is_menu, score, matched)


# --- digital gatekeeping detection (distinct from menu detection) ------------

@dataclass
class GatekeepingLook:
    is_gatekeeping: bool
    matched: list[str]


@dataclass
class GatekeepingDecision:
    is_gatekeeping: bool
    has_digit_option: bool
    classifier: str  # "haiku" | "none" (never "keyword_fallback" -- see decide_gatekeeping)
    reasoning: str


def looks_like_gatekeeping(transcript: str) -> GatekeepingLook:
    """Cheap keyword pre-filter, only ever consulted by the caller AFTER
    looks_like_menu() has already said this is NOT a menu -- so a real digit-
    press option always takes priority before this is even considered."""
    t = (transcript or "").lower()
    if not t.strip():
        return GatekeepingLook(False, [])
    matched = [p for p in _GATEKEEPING_PHRASES if p in t]
    return GatekeepingLook(bool(matched), matched)


def decide_gatekeeping(transcript: str) -> GatekeepingDecision:
    """Haiku-confirmed only -- deliberately NO keyword-only fallback path.

    Unlike decide_digit(), which falls back to keyword-priority digit
    selection on any Anthropic failure, gatekeeping has an asymmetric risk
    profile: a false positive hangs up and burns the company's remaining
    attempts for the quarter (confirmed_miss), while a false negative just
    falls through to the existing (already-safe) non-menu/AMD handling. So on
    any Anthropic failure, or low confidence, this returns is_gatekeeping=False
    rather than guessing from keywords alone.
    """
    try:
        h = classify_gatekeeping(transcript)
    except AnthropicUnavailable as e:
        log.error("ANTHROPIC API ERROR (check billing / key): %s", e)
        return GatekeepingDecision(False, False, "none",
                                   f"anthropic unavailable ({e}); not treated as gatekeeping")

    if h["confidence"] != "high":
        return GatekeepingDecision(False, h["has_digit_option"], "haiku",
                                   f"haiku: low confidence ({h['reasoning']})")

    return GatekeepingDecision(h["is_gatekeeping"], h["has_digit_option"], "haiku", h["reasoning"])


# --- alternative-contact-method detection (distinct from menu/gatekeeping) --

@dataclass
class AltContactLook:
    is_alt_contact: bool
    matched: list[str]


@dataclass
class AltContactDecision:
    is_alt_contact: bool
    has_digit_option: bool
    classifier: str  # "haiku" | "none" (never "keyword_fallback" -- see decide_alt_contact)
    reasoning: str


def looks_like_alt_contact(transcript: str) -> AltContactLook:
    """Cheap keyword pre-filter, only ever consulted by the caller AFTER
    looks_like_menu() has already said this is NOT a menu -- so a real digit-
    press option always takes priority before this is even considered."""
    t = (transcript or "").lower()
    if not t.strip():
        return AltContactLook(False, [])
    matched = [p for p in _ALT_CONTACT_PHRASES if p in t]
    return AltContactLook(bool(matched), matched)


def decide_alt_contact(transcript: str) -> AltContactDecision:
    """Haiku-confirmed only -- deliberately NO keyword-only fallback path,
    same asymmetric-risk reasoning as decide_gatekeeping: a false positive
    hangs up and burns the company's remaining attempts for the quarter
    (confirmed_miss), while a false negative just falls through to the
    existing (already-safe) non-menu/AMD handling.
    """
    try:
        h = classify_alt_contact(transcript)
    except AnthropicUnavailable as e:
        log.error("ANTHROPIC API ERROR (check billing / key): %s", e)
        return AltContactDecision(False, False, "none",
                                  f"anthropic unavailable ({e}); not treated as alt_miss")

    if h["confidence"] != "high":
        return AltContactDecision(False, h["has_digit_option"], "haiku",
                                  f"haiku: low confidence ({h['reasoning']})")

    return AltContactDecision(h["is_alt_contact"], h["has_digit_option"], "haiku", h["reasoning"])


# --- digit options parsing ---------------------------------------------------

# An option "anchor" is either "<press-verb> <digit>" or "<digit> for/to".
_OPT_ANCHOR = re.compile(
    r"(?:" + _PRESS_VERB + r"\s+(?:the\s+)?(?:number\s+)?(?P<d1>" + _DIGIT_TOKEN + r")\b"
    r"|\b(?P<d2>" + _DIGIT_TOKEN + r")\s+(?:for|to)\b)",
    re.I,
)


def _norm_digit(tok: str) -> str:
    return _DIGIT_WORDS.get(tok.strip().lower(), tok.strip().lower())


def parse_options(transcript: str) -> list[tuple[str, str]]:
    """Return [(digit, label), ...] in the order heard, de-duped by digit.

    The label is the text from this option's anchor up to the next option's
    anchor (or +60 chars), so labels don't bleed across options.
    """
    t = (transcript or "").lower()
    anchors = list(_OPT_ANCHOR.finditer(t))
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, m in enumerate(anchors):
        digit = _norm_digit(m.group("d1") or m.group("d2") or "")
        if not digit or digit in seen:
            continue
        seen.add(digit)
        end = anchors[i + 1].start() if i + 1 < len(anchors) else min(len(t), m.end() + 60)
        label = t[m.end():end].strip(" ,.;:-")
        found.append((digit, label))
    return found


# --- keyword-priority digit selection --------------------------------------

def choose_digit_by_priority(transcript: str) -> DigitPick:
    """Spec section 4 fallback. Always `flagged=True` -- this path only runs when
    Haiku is unavailable or the menu was ambiguous."""
    options = parse_options(transcript)
    if not options:
        return DigitPick(None, "no numbered options could be parsed from the menu", True)

    t = (transcript or "").lower()

    # 1. Emergency / after-hours anywhere in the menu.
    for digit, label in options:
        if any(w in label for w in _EMERGENCY_WORDS):
            return DigitPick(digit, f"emergency/after-hours option ('{label.strip()}')", True,
                              is_emergency=True)
    if any(w in t for w in _EMERGENCY_WORDS):
        # Emergency language present but not clearly tied to one option -> lowest.
        # is_emergency stays False -- the digit actually pressed is NOT confirmed
        # to be the emergency option, just an arbitrary lowest-numbered one.
        digit = min(options, key=lambda o: _digit_sort_key(o[0]))[0]
        return DigitPick(digit, "emergency language present, not tied to one option; took lowest", True)

    # 2. Closest to reaching a person.
    for digit, label in options:
        if any(w in label for w in _REACH_PERSON_WORDS):
            return DigitPick(digit, f"reach-a-person option ('{label.strip()}')", True)

    # 3. Lowest offered digit.
    digit = min(options, key=lambda o: _digit_sort_key(o[0]))[0]
    return DigitPick(digit, "no emergency/representative option; took lowest offered digit", True)


def _digit_sort_key(d: str) -> tuple[int, str]:
    # numeric first (0-9), then * and # last
    return (0, d) if d.isdigit() else (1, d)


# --- top-level decision: Haiku first, keyword-priority fallback ---------------

@dataclass
class Decision:
    is_menu: bool
    digit: str | None
    classifier: str  # "haiku" | "keyword_fallback"
    flagged: bool
    reasoning: str
    is_emergency_route: bool = False  # True only if THIS specific digit was
    # chosen because it is the emergency/after-hours option -- not just that
    # emergency language appears anywhere in the transcript.


def decide_digit(transcript: str) -> Decision:
    """Called only after `looks_like_menu` has passed. Confirms with Haiku and
    picks the digit; on any Anthropic failure, logs loudly and uses keyword
    priority (spec sections 4 and 11).

    The row is flagged whenever the ambiguous-fallback logic fires: multiple
    numbered options with no clear emergency/after-hours instruction, or any
    time keyword priority (not Haiku) chose the digit.
    """
    keyword_look = looks_like_menu(transcript)
    options = parse_options(transcript)
    has_emergency = any(w in transcript.lower() for w in _EMERGENCY_WORDS)
    ambiguous = len(options) >= 2 and not has_emergency

    try:
        h = classify_ivr_digit(transcript)
    except AnthropicUnavailable as e:
        log.error("ANTHROPIC API ERROR (check billing / key): %s", e)
        if not keyword_look.is_menu:
            return Decision(False, None, "keyword_fallback", False,
                            f"anthropic unavailable ({e}); keywords: not a menu")
        pick = choose_digit_by_priority(transcript)
        return Decision(True, pick.digit, "keyword_fallback", True,
                        f"anthropic unavailable ({e}); {pick.reason}",
                        is_emergency_route=pick.is_emergency)

    # False-positive guard: Haiku not confident it's a menu -> treat as not a menu.
    if not h["is_menu"] or h["confidence"] != "high":
        return Decision(False, None, "haiku", False,
                        f"haiku: not a confident menu ({h['reasoning']})")

    if h["digit"] is None:
        pick = choose_digit_by_priority(transcript)
        if pick.digit is None or pick.reason == "no emergency/representative option; took lowest offered digit":
            # Both Haiku and keyword-priority came up with nothing but "just
            # press something" -- the exact scenario that pressed a voicemail
            # system's OWN recording-review controls in two confirmed real
            # incidents with different wording each time (Cooks Proclean And
            # Restoration; Blumer Restoration: pressed "1" into "to disconnect
            # press 1 to record your message press 2", landing on
            # extended_hold instead of the true voicemail outcome).
            # Phrase-blocklisting _VOICEMAIL_CONTROL_PHRASES doesn't
            # generalize to new wording (confirmed twice), but this signal
            # does: when NEITHER a semantic read (Haiku) NOR a keyword
            # priority scan (emergency/reach-a-person) can find any
            # business-relevant reason to press a specific digit, guessing
            # the lowest one anyway is exactly what goes wrong. Don't press
            # blind -- fall through to keep-listening, same as a Haiku veto;
            # a real menu with a genuine emergency/reach-a-person option
            # never reaches this branch (Lanier's "press 4 for emergency"
            # menu is caught above, before this point).
            return Decision(False, None, "haiku", False,
                            f"haiku saw menu structure but found no safely-groundable digit, "
                            f"and keyword-priority found no emergency/representative signal "
                            f"either; not pressing blind ({h['reasoning']})")
        return Decision(True, pick.digit, "keyword_fallback", True,
                        f"haiku saw a menu but no digit; {pick.reason}",
                        is_emergency_route=pick.is_emergency)

    # Grounding guard: Haiku's chosen digit must actually be one of the
    # structurally-parsed menu options. Without this, Haiku can select a
    # digit that isn't grounded in the transcript at all (hallucination) --
    # fall back to keyword priority and flag the row instead of trusting it.
    valid_digits = {d for d, _ in options}
    if valid_digits and h["digit"] not in valid_digits:
        pick = choose_digit_by_priority(transcript)
        return Decision(True, pick.digit, "keyword_fallback", True,
                        f"haiku chose digit {h['digit']!r} not grounded in any parsed menu "
                        f"option (parsed: {sorted(valid_digits)}); {pick.reason}",
                        is_emergency_route=pick.is_emergency)

    return Decision(True, h["digit"], "haiku", ambiguous, f"haiku: {h['reasoning']}",
                    is_emergency_route=h["is_emergency_option"])


# --- tail classification ----------------------------------------------------

def classify_tail(tail_transcript: str) -> str:
    """Post-navigation outcome from the transcript captured after the last digit.
    Returns answered / voicemail / extended_hold / unknown."""
    t = (tail_transcript or "").lower().strip()
    if not t:
        return "unknown"

    if any(p in t for p in _TAIL_VOICEMAIL):
        return "voicemail"
    if any(p in t for p in _TAIL_HOLD):
        return "extended_hold"
    if any(p in t for p in _TAIL_ANSWERED):
        # Guard: a long utterance that merely contains "hello" is probably still
        # a recording; a short one is a person.
        if len(t.split()) <= 12:
            return "answered"
        return "unknown"
    return "unknown"


@dataclass
class TailDecision:
    outcome: str        # "answered" | "voicemail" | "extended_hold" | "unknown"
    classifier: str     # "haiku" | "keyword" | "amd_fallback"
    reasoning: str
    conflicts_with_amd: bool  # True when we overrode a real AMD signal


def decide_tail(transcript: str, answered_by: str | None, company_name: str = "") -> TailDecision:
    """Single source of truth for 'what actually happened on this call', used
    for BOTH the post-menu-navigation tail AND plain non-menu calls.

    The transcript is the PRIMARY signal, not AMD -- this generalizes the
    existing menu-call design (AMD judges the menu audio, not the outcome; the
    post-navigation transcript is authoritative) to every call, closing a real
    gap where non-menu calls used to trust AMD alone and never looked at the
    transcript they'd already captured. AMD is only a tie-breaker/fallback when
    Haiku is unavailable or genuinely unconfident, and even then only after the
    keyword list finds nothing -- never silently overridden without a reason
    logged in `reasoning`.

    company_name (the Queue row's own name, if known) is passed through to
    classify_call_audio so a truncated name fragment can be recognized as a
    bare-name live answer -- see that function's docstring.
    """
    amd_guess = outcomes.from_amd(answered_by)  # "answered" | "voicemail" | None

    def _from_keyword_or_amd(prefix: str) -> TailDecision:
        kw = classify_tail(transcript)
        if kw != "unknown":
            conflicts = amd_guess is not None and kw != amd_guess
            return TailDecision(kw, "keyword", f"{prefix}; keyword match", conflicts)
        if amd_guess:
            return TailDecision(amd_guess, "amd_fallback", f"{prefix}; no keyword match, trusting AMD", False)
        return TailDecision("unknown", "keyword", f"{prefix}; inconclusive", False)

    try:
        h = classify_call_audio(transcript, company_name)
    except AnthropicUnavailable as e:
        log.error("ANTHROPIC API ERROR (check billing / key): %s", e)
        return _from_keyword_or_amd(f"anthropic unavailable ({e})")

    if h["confidence"] != "high":
        return _from_keyword_or_amd(f"haiku low confidence ({h['reasoning']})")

    outcome = (
        "answered" if h["is_human"] else
        "voicemail" if h["is_voicemail"] else
        "extended_hold" if h["is_hold"] else
        "unknown"
    )
    if outcome == "unknown":
        return _from_keyword_or_amd(f"haiku inconclusive ({h['reasoning']})")

    conflicts = amd_guess is not None and outcome != amd_guess
    reasoning = h["reasoning"]
    if conflicts:
        reasoning = f"AMD said {answered_by!r} but transcript indicates {outcome}: {reasoning}"
    return TailDecision(outcome, "haiku", reasoning, conflicts)

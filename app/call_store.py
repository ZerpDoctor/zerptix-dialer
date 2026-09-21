"""In-memory tracking of calls this process has placed.

Scope note: this is a single-process store. It is enough for the core-loop test
(one dialer process, low volume). The full build (spec sections 6 & 8) will need
this promoted to something durable so idempotency and in-flight locks survive a
restart and work across processes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class CallRecord:
    call_sid: str
    to_number: str
    from_number: str = ""
    company_name: str = ""              # from the Queue row at dial time, if this
    company_timezone: str = ""          # was a scheduler-placed call -- avoids a
    # second, redundant Sheets read at resolution time just to look these up
    # again by phone number (see google_sheets read-quota incident 2026-09-20).
    placed_at: float = field(default_factory=time.time)
    answered_by: str | None = None      # buffered AMD AnsweredBy
    call_status: str | None = None
    recording_url: str | None = None
    logged: bool = False

    # --- IVR navigation state ---
    phase: str = "dialing"              # dialing|listening|navigating|awaiting_tail|awaiting_amd|resolved
    answered_at: float | None = None    # set on the first /ivr/turn (call connected)
    gather_count: int = 0
    transcript_accum: str = ""
    transcript_at_last_digit: int = 0   # index into transcript_accum where the tail begins
    ivr_detected: bool = False
    menu_levels: int = 0                # how many menus we navigated (digits sent)
    digits_sent: list[str] = field(default_factory=list)
    ivr_fallback_flagged: bool = False
    classifier: str = "none"           # none|haiku|keyword_fallback
    ivr_reasoning: str = ""
    hit_time_cap: bool = False
    tail_turns: int = 0
    resolve_note: str = ""
    gatekeeping_detected: bool = False
    gatekeeping_reasoning: str = ""
    alt_contact_detected: bool = False
    alt_contact_reasoning: str = ""
    emergency_route: bool = False  # True if ANY digit pressed in the whole
    # navigation path (across multi-level menus) was specifically the
    # emergency/after-hours option -- sticky once set, never cleared by a
    # later non-emergency press.


class CallStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_sid: dict[str, CallRecord] = {}

    def register(self, call_sid: str, to_number: str, from_number: str = "",
                 company_name: str = "", company_timezone: str = "") -> CallRecord:
        with self._lock:
            rec = CallRecord(call_sid=call_sid, to_number=to_number, from_number=from_number,
                              company_name=company_name, company_timezone=company_timezone)
            self._by_sid[call_sid] = rec
            return rec

    def get(self, call_sid: str) -> CallRecord | None:
        with self._lock:
            return self._by_sid.get(call_sid)

    def has_inflight_to(self, to_number: str) -> bool:
        """True if a call to this number was placed and not yet logged."""
        with self._lock:
            return any(
                r.to_number == to_number and not r.logged
                for r in self._by_sid.values()
            )

    def update(self, call_sid: str, *, to_number: str | None = None, **fields) -> CallRecord:
        """Upsert: create the record if this is the first we've heard of the SID.

        Webhooks can arrive for a call this process did not place -- after a
        restart, or (in the core loop) because AMD and status callbacks are
        separate HTTP requests that must be stitched together by SID. Creating
        on first sight keeps the signals from those requests from being lost.
        """
        with self._lock:
            rec = self._by_sid.get(call_sid)
            if rec is None:
                rec = CallRecord(call_sid=call_sid, to_number=to_number or "")
                self._by_sid[call_sid] = rec
            elif to_number and not rec.to_number:
                rec.to_number = to_number
            for k, v in fields.items():
                if v is not None:
                    setattr(rec, k, v)
            return rec

    def ensure_answered(self, call_sid: str) -> CallRecord:
        """Mark the call connected (first /ivr/turn) and stamp answered_at once."""
        with self._lock:
            rec = self._by_sid.get(call_sid)
            if rec is None:
                rec = CallRecord(call_sid=call_sid, to_number="")
                self._by_sid[call_sid] = rec
            if rec.answered_at is None:
                rec.answered_at = time.time()
                rec.phase = "listening"
            return rec

    def add_turn(self, call_sid: str, speech: str) -> CallRecord:
        """Append one gather's speech to the accumulator and bump the count."""
        with self._lock:
            rec = self._by_sid[call_sid]
            rec.gather_count += 1
            if speech:
                rec.transcript_accum = (rec.transcript_accum + " " + speech).strip()
            return rec

    def record_digit(self, call_sid: str, digit: str, classifier: str,
                     flagged: bool, reasoning: str, is_emergency_route: bool = False) -> CallRecord:
        with self._lock:
            rec = self._by_sid[call_sid]
            rec.digits_sent.append(digit)
            rec.menu_levels = len(rec.digits_sent)
            rec.ivr_detected = True
            rec.classifier = classifier
            rec.ivr_fallback_flagged = rec.ivr_fallback_flagged or flagged
            rec.ivr_reasoning = reasoning
            rec.transcript_at_last_digit = len(rec.transcript_accum)
            rec.emergency_route = rec.emergency_route or is_emergency_route
            return rec

    def mark_menu_no_digit(self, call_sid: str, classifier: str, reasoning: str) -> CallRecord:
        with self._lock:
            rec = self._by_sid[call_sid]
            rec.ivr_detected = True
            rec.ivr_fallback_flagged = True
            rec.classifier = classifier
            rec.ivr_reasoning = reasoning
            rec.transcript_at_last_digit = len(rec.transcript_accum)
            return rec

    def mark_gatekeeping(self, call_sid: str, classifier: str, reasoning: str) -> CallRecord:
        """Digital-gatekeeping prompt detected (no digit path, no menu) -- distinct
        from ivr_detected, which specifically means a menu was navigated."""
        with self._lock:
            rec = self._by_sid[call_sid]
            rec.gatekeeping_detected = True
            rec.gatekeeping_reasoning = reasoning
            rec.classifier = classifier
            return rec

    def mark_alt_contact(self, call_sid: str, classifier: str, reasoning: str) -> CallRecord:
        """Automated redirect-to-a-different-contact-channel detected (text/
        email/website, no digit path, no menu) -- distinct from gatekeeping
        (which demands the caller's own info) and from ivr_detected."""
        with self._lock:
            rec = self._by_sid[call_sid]
            rec.alt_contact_detected = True
            rec.alt_contact_reasoning = reasoning
            rec.classifier = classifier
            return rec

    def bump_tail(self, call_sid: str) -> int:
        with self._lock:
            rec = self._by_sid[call_sid]
            rec.tail_turns += 1
            return rec.tail_turns

    def seconds_since_answered(self, call_sid: str) -> float:
        with self._lock:
            rec = self._by_sid.get(call_sid)
            if rec is None or rec.answered_at is None:
                return 0.0
            return time.time() - rec.answered_at

    def mark_logged(self, call_sid: str) -> bool:
        """Atomically claim the right to log this call. Returns False if another
        handler already claimed it (duplicate webhook)."""
        with self._lock:
            rec = self._by_sid.get(call_sid)
            if rec is None:
                # Unknown SID (e.g. process restarted). Create a stub and claim it.
                rec = CallRecord(call_sid=call_sid, to_number="")
                self._by_sid[call_sid] = rec
            if rec.logged:
                return False
            rec.logged = True
            return True


STORE = CallStore()

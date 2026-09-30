"""In-memory tracking of calls this process has placed.

Scope note: this is a single-process store. It is enough for the core-loop test
(one dialer process, low volume). The full build (spec sections 6 & 8) will need
this promoted to something durable so idempotency and in-flight locks survive a
restart and work across processes.
"""
from __future__ import annotations

import copy
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
    is_scheduled_attempt: bool = False  # True only for a real scheduler-placed
    # dial (record_attempt=true at /calls). Gates queue_writer.apply_outcome's
    # record_attempt self-heal (see that function's docstring) -- a manual/test
    # call to a real Queue number must never touch attempts/last_call_date/
    # next_eligible_date just because it happened to resolve.
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
    segment_len_at_last_check: int = 0  # len(segment) as of the last menu-stage
    # evaluation -- lets ivr_turn detect "still growing" (far end still
    # talking) vs. "stable" (a real pause) across short poll cycles. Reset to
    # 0 alongside transcript_at_last_digit since it tracks the same window.
    consecutive_growth_turns: int = 0   # how many turns in a row came back
    # "still growing" -- real incident 2026-09-28 (911 Restoration): a menu
    # that LOOPS (repeats its own announcement when nothing registers, with
    # only a brief gap between loops) never gave the stability gate a real
    # pause to evaluate on -- it deferred for all 8 turns/66s of the call,
    # so decide_digit() never even ran once, despite hearing a clear "press
    # one for..." option four separate times. Capped in ivr_turn so a
    # looping menu can't defer forever; reset alongside the fields above.
    speaking_deferrals: int = 0         # consecutive turns skipped because the far
    # end was still mid-speech (see CFG.listen_gate_enabled); reset whenever a
    # turn actually gets to decide
    last_digit_at: float | None = None  # wall time of the most recent digit
    # press -- the hold budget is counted from here (see server._deadline_seconds)
    press_diag: str = ""                # per-press timing evidence, appended to the Calls notes
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
                 company_name: str = "", company_timezone: str = "",
                 is_scheduled_attempt: bool = False) -> CallRecord:
        with self._lock:
            rec = CallRecord(call_sid=call_sid, to_number=to_number, from_number=from_number,
                              company_name=company_name, company_timezone=company_timezone,
                              is_scheduled_attempt=is_scheduled_attempt)
            self._by_sid[call_sid] = rec
            self._evict_old_resolved_locked()
            return rec

    def _evict_old_resolved_locked(self, max_age_seconds: float = 3600) -> None:
        """Drop long-resolved records so a long-running process's memory
        doesn't grow for the rest of its uptime (every call ever handled
        otherwise stays in _by_sid forever -- a real, if slow, leak). Only
        phase=='resolved' records are eligible regardless of age, and the
        1-hour margin is well past how long a real call plus its terminal
        status callback ever takes (the master timer alone caps a call at
        60s) -- generous safety room for SignalWire's own late 'completed'
        callback (see /webhooks/status's backfill_call_status) before this
        ever removes a record that still needs it. Any later webhook for an
        evicted call_sid is already handled safely: every caller treats an
        unknown call_sid as a normal case (stub-create in mark_logged, the
        Sheet-based duplicate check in _resolve's `recovered` path) -- the
        same handling already proven correct for a real process restart.
        Called opportunistically from register() (once per new call) rather
        than on a timer/background thread -- no extra machinery, and it
        naturally scales with real call volume. Caller must hold self._lock."""
        cutoff = time.time() - max_age_seconds
        stale = [sid for sid, r in self._by_sid.items()
                 if r.phase == "resolved" and r.placed_at < cutoff]
        for sid in stale:
            del self._by_sid[sid]

    def get(self, call_sid: str) -> CallRecord | None:
        with self._lock:
            return self._by_sid.get(call_sid)

    def snapshot(self, call_sid: str) -> CallRecord | None:
        """Atomic point-in-time copy, for resolution specifically.

        `get()` returns a reference to the SAME mutable record -- fine for
        code that reads one field, wrong for `_resolve()`, which reads
        several fields (across `_compute_outcome` then `_build_row`) that
        must all reflect the same instant. A real incident found via a
        full-history classification sweep 2026-09-21: a late-arriving
        `/ivr/turn` webhook for the same call landed on another thread in the
        gap between those two calls and ran `mark_alt_contact()`, so the
        outcome was decided from the pre-mutation state while the logged
        notes were built from the post-mutation state -- outcome='voicemail'
        with an 'alt_contact: ...' note contradicting it. Raising gunicorn
        threads earlier the same night made this race window get hit more
        often, not less. `digits_sent` is copied explicitly since a shallow
        copy would otherwise still share the same mutable list."""
        with self._lock:
            rec = self._by_sid.get(call_sid)
            if rec is None:
                return None
            snap = copy.copy(rec)
            snap.digits_sent = list(rec.digits_sent)
            return snap

    def has_inflight_to(self, to_number: str) -> bool:
        """True if a call to this number was placed and not yet logged."""
        with self._lock:
            return any(
                r.to_number == to_number and not r.logged
                for r in self._by_sid.values()
            )

    def inflight_count(self) -> int:
        """Total calls placed and not yet logged, across ALL destinations --
        the real, global, single-source-of-truth concurrency count for the
        scheduler's dial pool (2026-09-21). Deliberately NOT tracked as a
        local list in the worker process: a real incident showed 10 calls
        truly concurrent despite a pool cap of 5, traced to the worker's
        own in-memory tracking resetting to empty on a process restart --
        a fresh worker process assumed 0 in flight and dialed 5 more while
        the previous process's 5 were still resolving here. Querying this
        count directly instead of tracking it locally is immune to that,
        since a new worker process just asks for the current real number
        instead of assuming zero."""
        with self._lock:
            return sum(1 for r in self._by_sid.values() if not r.logged)

    def rehydrate(self, call_sid: str, data: dict[str, str]) -> CallRecord:
        """Reconstruct a CallRecord from a call_checkpoint.load() dict for a
        call_sid this process has never seen (a restart-orphaned in-flight
        call). Caller MUST already have confirmed via
        google_sheets.call_sid_already_logged() that this call hasn't
        already been resolved -- `logged` is never trusted from a
        checkpoint, only from the real Calls tab, so it's deliberately not
        one of the restored fields (always starts False here).

        Deliberately overwrites any existing in-memory record (rehydration
        is only ever called when STORE.get() already returned None) rather
        than merging, since a checkpoint row is a complete snapshot, not a
        partial update. Malformed/missing fields fall back to CallRecord's
        own defaults rather than raising -- a corrupt checkpoint must not
        break live call handling, just lose some of the recovery benefit."""
        def _f(name: str, default: float) -> float:
            try:
                return float(data.get(name) or default)
            except ValueError:
                return default

        def _i(name: str, default: int) -> int:
            try:
                return int(data.get(name) or default)
            except ValueError:
                return default

        def _b(name: str) -> bool:
            return (data.get(name) or "").strip().lower() == "true"

        with self._lock:
            rec = CallRecord(
                call_sid=call_sid,
                to_number=data.get("to_number", ""),
                from_number=data.get("from_number", ""),
                company_name=data.get("company_name", ""),
                company_timezone=data.get("company_timezone", ""),
                is_scheduled_attempt=_b("is_scheduled_attempt"),
                answered_by=data.get("answered_by") or None,
                phase=data.get("phase") or "listening",
                answered_at=_f("answered_at", time.time()),
                gather_count=_i("gather_count", 0),
                transcript_accum=data.get("transcript_accum", ""),
                transcript_at_last_digit=_i("transcript_at_last_digit", 0),
                segment_len_at_last_check=_i("segment_len_at_last_check", 0),
                ivr_detected=_b("ivr_detected"),
                digits_sent=[d for d in (data.get("digits_sent") or "").split("|") if d],
                ivr_fallback_flagged=_b("ivr_fallback_flagged"),
                classifier=data.get("classifier") or "none",
                ivr_reasoning=data.get("ivr_reasoning", ""),
                hit_time_cap=_b("hit_time_cap"),
                gatekeeping_detected=_b("gatekeeping_detected"),
                gatekeeping_reasoning=data.get("gatekeeping_reasoning", ""),
                alt_contact_detected=_b("alt_contact_detected"),
                alt_contact_reasoning=data.get("alt_contact_reasoning", ""),
                emergency_route=_b("emergency_route"),
            )
            rec.menu_levels = len(rec.digits_sent)
            self._by_sid[call_sid] = rec
            return rec

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
            rec.last_digit_at = time.time()
            rec.menu_levels = len(rec.digits_sent)
            rec.ivr_detected = True
            rec.classifier = classifier
            rec.ivr_fallback_flagged = rec.ivr_fallback_flagged or flagged
            rec.ivr_reasoning = reasoning
            rec.transcript_at_last_digit = len(rec.transcript_accum)
            rec.segment_len_at_last_check = 0
            rec.consecutive_growth_turns = 0
            # Describes the route we ended up on, i.e. the LAST digit pressed.
            # It used to stay true once any press was the emergency option,
            # so a call that pressed the emergency digit and then a second,
            # non-emergency one (Rare Restoration 1,2 -> "mitigation customer
            # service") still claimed to have routed to the emergency line --
            # and the business replied that it had not.
            rec.emergency_route = is_emergency_route
            return rec

    def mark_menu_no_digit(self, call_sid: str, classifier: str, reasoning: str) -> CallRecord:
        with self._lock:
            rec = self._by_sid[call_sid]
            rec.ivr_detected = True
            rec.ivr_fallback_flagged = True
            rec.classifier = classifier
            rec.ivr_reasoning = reasoning
            rec.transcript_at_last_digit = len(rec.transcript_accum)
            rec.segment_len_at_last_check = 0
            rec.consecutive_growth_turns = 0
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

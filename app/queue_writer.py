"""The single place Queue-tab rows are mutated.

Every write to a company's row -- recording a placed call, applying a resolved
outcome, a quarter reset -- goes through here, serialized by a per-phone
threading.RLock. Because all callers run in the one `--workers 1` server process
(the scheduler only *reads* the Queue), this is sufficient mutual exclusion and
has NO persistent lock state: a crash drops every lock with the process.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .config import CFG
from .queue_model import (
    ANSWERS_TO_COVER,
    COVERED_OUTCOMES,
    MISS_OUTCOMES,
    STATUS_CONFIRMED_COVERED,
    STATUS_CONFIRMED_MISS,
    append_csv,
    next_window_for,
)
from .timezones import parse_tz

log = logging.getLogger("dialer.queue_writer")

_locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
_locks_guard = threading.Lock()


def lock_for(phone: str) -> threading.RLock:
    with _locks_guard:
        return _locks[phone or "-"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def record_attempt(queue, phone: str, *, call_sid: str, window: str,
                   local_date: date, now_utc: datetime) -> dict | None:
    """Record one placed call against the company's row. Returns the written
    fields, or None if the row is gone or was already attempted today (a lost
    race -- the caller should treat that as 'skipped')."""
    with lock_for(phone):
        row = queue.find_by_phone(phone)
        if row is None:
            return None
        if row.last_call_date == local_date.isoformat():
            log.info("record_attempt: %s already attempted %s", phone, local_date)
            return None
        n = row.attempts + 1
        fields = {
            "current_quarter_attempts": n,
            "last_call_date": local_date.isoformat(),
            "last_call_window": window,
            "next_eligible_date": (local_date + timedelta(days=CFG.sched_min_days_between_attempts)).isoformat(),
            "call_sid_history": append_csv(row.call_sid_history, call_sid),
            "last_updated_at": _iso(now_utc),
        }
        queue.update_fields(row.row_number, fields)
        return fields


def apply_outcome(queue, phone: str, outcome: str, *, call_sid: str = "",
                  recording_url: str = "", ivr_flagged: bool = False,
                  heal_missed_attempt: bool = False,
                  when_utc: datetime | None = None) -> dict | None:
    """Write a resolved call outcome + quarter status back to the company's row
    (spec section 7). Returns the written fields, or None if no matching row."""
    when_utc = when_utc or datetime.now(timezone.utc)
    with lock_for(phone):
        row = queue.find_by_phone(phone)
        if row is None:
            return None
        fields: dict = {"last_outcome": outcome, "last_updated_at": _iso(when_utc)}
        if call_sid:
            fields["call_sid_history"] = append_csv(row.call_sid_history, call_sid)
        if recording_url:
            fields["recording_url"] = recording_url
        if ivr_flagged:
            fields["ivr_fallback_flagged"] = "yes"

        # Self-heal a record_attempt() that never wrote through. Real incident
        # 2026-09-24: Restoration Done was dialed twice 8 minutes apart -- its
        # first call's record_attempt exhausted all 4 retries and gave up
        # (exactly the residual risk that retry's own error log names), leaving
        # last_call_date unset even though the call itself connected and
        # resolved normally moments later. record_attempt() only runs at DIAL
        # time and has no reconciliation if it fails; apply_outcome() runs
        # reliably at RESOLUTION time for every call that connects, so it's
        # the natural place to catch and fill the gap before the next tick can
        # see this row as still-eligible and redial a real business. Gated on
        # heal_missed_attempt (only true for a real scheduler-placed dial,
        # never a manual/test call) so a one-off test against a real Queue
        # number can't accidentally bump its attempts/cadence fields.
        if heal_missed_attempt:
            tz = parse_tz(row.timezone)
            local_now = when_utc.astimezone(tz) if tz else when_utc
            local_today = local_now.date().isoformat()
            if row.last_call_date != local_today:
                log.warning(
                    "apply_outcome healing a missed record_attempt for %s: "
                    "last_call_date was %r, not today (%s) -- the dial-time "
                    "write must have failed",
                    phone, row.last_call_date, local_today,
                )
                fields["current_quarter_attempts"] = row.attempts + 1
                fields["last_call_date"] = local_today
                fields["last_call_window"] = next_window_for(row)
                fields["next_eligible_date"] = (
                    local_now.date() + timedelta(days=CFG.sched_min_days_between_attempts)
                ).isoformat()

        if outcome in MISS_OUTCOMES:
            # A single miss is always decisive, regardless of any prior answers
            # -- overrides and resets the answered-streak immediately.
            fields["this_quarter_status"] = STATUS_CONFIRMED_MISS
            fields["miss_timestamp"] = _iso(when_utc)
            fields["consecutive_answered_count"] = 0
            if outcome == "disconnected":
                fields["do_not_call"] = "true"  # spec section 5
        elif outcome in COVERED_OUTCOMES:
            # A single answer is NOT decisive -- only ANSWERS_TO_COVER
            # consecutive answers (no miss in between) confirm coverage.
            streak = row.consecutive_answered + 1
            fields["consecutive_answered_count"] = streak
            if streak >= ANSWERS_TO_COVER:
                fields["this_quarter_status"] = STATUS_CONFIRMED_COVERED
            # else: stays in_progress -- one answer isn't enough evidence yet
        # ivr_unresolved / unknown -> inconclusive, stays in_progress, streak untouched

        queue.update_fields(row.row_number, fields)
        return fields


def apply_reset(queue, phone: str, reset_fields: dict) -> None:
    with lock_for(phone):
        row = queue.find_by_phone(phone)
        if row is not None:
            queue.update_fields(row.row_number, reset_fields)

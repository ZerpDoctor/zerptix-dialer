"""Durable checkpoint of a call's FULL in-progress IVR state, in the same
Google Sheet already in use -- no new datastore, superseding the narrower
amd_checkpoint.py (which only durably recorded AnsweredBy).

Real incident chain, 2026-09-28: a full-history audit found 97/168 (58%) of
'unknown' rows carry "AMD gave no usable result" -- a mid-call dialer-web
restart wiping the in-memory CallStore between a signal arriving and the call
resolving (the durable-state gap this project's own history already flagged
as deliberately deferred). amd_checkpoint.py durably recorded AnsweredBy
specifically, but a restart just as often wipes the accumulated transcript,
digits already pressed, and navigation state too -- recovering AnsweredBy
alone still left most of those calls resolving as a slightly-more-informative
'unknown' rather than a real classification, since decide_tail/decide_digit
need the transcript, not just AnsweredBy, to do anything useful.

This checkpoints the CallRecord fields needed to fully resume a call after a
restart -- not just read them back at resolution time, but REHYDRATE the live
CallStore record so ivr_turn/webhook_amd/webhook_status can keep navigating
and eventually resolve with the SAME context the pre-restart process had,
not a blank stub. See call_store.py's rehydrate() and each webhook's
_maybe_rehydrate() call in server.py.

Append-only (matching amd_checkpoint.py's proven pattern) -- a call
checkpoints on essentially every turn, so append-then-take-latest avoids a
read-before-write round trip on the hot path; load() takes the newest row.
Best-effort throughout: a checkpoint failure must never break real call
handling, only lose the recovery benefit for that one call.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .config import CFG

log = logging.getLogger("dialer.call_checkpoint")

# Deliberately excludes: placed_at (not needed to resume), call_status/
# recording_url (backfilled independently, not resume-critical), logged
# (NEVER trust a checkpointed "already logged" -- google_sheets.
# call_sid_already_logged() against the real Calls tab is the only safe
# source of truth for that, checked before any rehydration is attempted),
# resolve_note/tail_turns/menu_levels (re-derived or not resume-critical).
FIELDS = [
    "call_sid", "to_number", "from_number", "company_name", "company_timezone",
    "is_scheduled_attempt", "answered_by", "phase", "answered_at",
    "gather_count", "transcript_accum", "transcript_at_last_digit",
    "segment_len_at_last_check", "ivr_detected", "digits_sent",
    "ivr_fallback_flagged", "classifier", "ivr_reasoning", "hit_time_cap",
    "gatekeeping_detected", "gatekeeping_reasoning", "alt_contact_detected",
    "alt_contact_reasoning", "emergency_route",
]
HEADER = FIELDS + ["checkpointed_at_iso"]

_DIGITS_SEP = "|"

_ensured = False
_ensure_lock = threading.Lock()


def _svc():
    from . import google_sheets
    return google_sheets._service()


def _tab() -> str:
    return CFG.call_checkpoint_tab


def _ensure_tab() -> None:
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        svc = _svc()
        meta = svc.spreadsheets().get(spreadsheetId=CFG.sheet_id).execute()
        tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}
        tab = _tab()
        if tab not in tabs:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=CFG.sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()
            log.info("Created missing tab %r", tab)
        existing = (
            svc.spreadsheets().values()
            .get(spreadsheetId=CFG.sheet_id, range=f"{tab}!A1:Z1")
            .execute().get("values", [[]])
        )
        if (existing[0] if existing else [])[: len(HEADER)] != HEADER:
            svc.spreadsheets().values().update(
                spreadsheetId=CFG.sheet_id, range=f"{tab}!A1",
                valueInputOption="RAW", body={"values": [HEADER]},
            ).execute()
        _ensured = True


def _serialize(rec) -> list[str]:
    row = []
    for f in FIELDS:
        v = getattr(rec, f)
        if f == "digits_sent":
            row.append(_DIGITS_SEP.join(v))
        elif v is None:
            row.append("")
        else:
            row.append(str(v))
    return row


def save(rec) -> None:
    """Best-effort: append the current full state. Called on essentially
    every turn from server.py -- never raises, a failure here only means
    this one turn's progress isn't durable, not that the live call breaks."""
    try:
        _ensure_tab()
        row = _serialize(rec) + [datetime.now(timezone.utc).isoformat()]
        _svc().spreadsheets().values().append(
            spreadsheetId=CFG.sheet_id,
            range=f"{_tab()}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("Call checkpoint write failed (call_sid=%s): %s", rec.call_sid, e)


def load(call_sid: str) -> dict[str, str] | None:
    """Best-effort recovery read -- only called when a process sees a
    call_sid it has no in-memory record for, which should be rare. Returns
    the newest matching row as {field: raw_string}, or None on no match or
    any failure -- never raises."""
    try:
        _ensure_tab()
        idx = {name: i for i, name in enumerate(HEADER)}
        existing = (
            _svc().spreadsheets().values()
            .get(spreadsheetId=CFG.sheet_id, range=f"{_tab()}!A2:{chr(ord('A') + len(HEADER) - 1)}100000")
            .execute().get("values", [])
        )
        matches = [row for row in existing if row and row[0] == call_sid]
        if not matches:
            return None
        latest = matches[-1]
        return {f: (latest[idx[f]] if idx[f] < len(latest) else "") for f in HEADER}
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("Call checkpoint lookup failed (call_sid=%s): %s", call_sid, e)
        return None

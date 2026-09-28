"""Durable checkpoint of AMD (AnsweredBy) verdicts, in the same Google Sheet
the app already uses -- no new datastore.

Real incident found 2026-09-28: a full-history audit showed 97 of 168 (58%)
'unknown' rows carry the note "AMD gave no usable result", and sampling 10 of
them against SignalWire's own Call resource directly showed 9 actually had a
real AnsweredBy verdict (machine_start/human) that this process simply never
captured -- confirmed via server logs to be a mid-call web-process restart
wiping the in-memory CallStore between AMD arriving and the call resolving
(the same class of gap the project's own history already flagged as "a call
genuinely in-flight at the moment of a restart still loses its real data,
deliberately not fixed yet" -- this closes the AMD-specific slice of that gap
without the larger durable-state project Redis/a volume would need).

Best-effort only, matching inbound_log.py's pattern: a checkpoint write/read
failure must never break real call handling, only lose the recovery benefit
for that one call.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .config import CFG

log = logging.getLogger("dialer.amd_checkpoint")

HEADER = ["call_sid", "answered_by", "checkpointed_at_iso"]

_ensured = False
_ensure_lock = threading.Lock()


def _svc():
    from . import google_sheets
    return google_sheets._service()


def _tab() -> str:
    return CFG.amd_checkpoint_tab


def _ensure_tab() -> None:
    """Create the checkpoint tab + header if missing. Runs at most once per
    process (a module-level flag, not a call_sid-keyed check) since this
    never changes after the first successful call."""
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


def checkpoint(call_sid: str, answered_by: str) -> None:
    """Durably record a real AMD verdict the moment it arrives, before this
    process's in-memory copy can be lost to a restart. Append-only -- AMD
    normally fires once per call, and a rare duplicate append is harmless
    (lookup() just takes the newest match). Called from the AMD webhook
    itself, which is a side-channel request with no live call audio waiting
    on it, so this never adds latency to an actual in-progress call."""
    if not answered_by:
        return
    try:
        _ensure_tab()
        _svc().spreadsheets().values().append(
            spreadsheetId=CFG.sheet_id,
            range=f"{_tab()}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[call_sid, answered_by, datetime.now(timezone.utc).isoformat()]]},
        ).execute()
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("AMD checkpoint write failed (call_sid=%s): %s", call_sid, e)


def lookup(call_sid: str) -> str:
    """Best-effort recovery read -- only called when a call is about to
    resolve with a blank answered_by, which should be rare (the whole point
    of this module), so this doesn't run on the hot path of every call.
    Returns "" on any failure or no match, never raises."""
    try:
        _ensure_tab()
        existing = (
            _svc().spreadsheets().values()
            .get(spreadsheetId=CFG.sheet_id, range=f"{_tab()}!A2:C100000")
            .execute().get("values", [])
        )
        matches = [row[1] for row in existing if len(row) > 1 and row[0] == call_sid]
        return matches[-1] if matches else ""
    except Exception as e:  # noqa: BLE001 - best effort only
        log.warning("AMD checkpoint lookup failed (call_sid=%s): %s", call_sid, e)
        return ""

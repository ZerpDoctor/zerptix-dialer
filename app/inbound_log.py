"""Append-only log of inbound calls to the dialer number pool (spec section 9).

Every inbound call to a pool number is <Reject>ed (never answered). This just
records that it happened so you can see if/when callbacks are landing once real
numbers are live. Best-effort: a logging failure must never change the reject.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .config import CFG

log = logging.getLogger("dialer.inbound")

HEADER = [
    "received_at_iso",
    "from_number",
    "from_city",
    "from_state",
    "to_number",
    "in_pool",
    "action",
    "call_sid",
]


def _svc():
    from . import google_sheets
    return google_sheets._service()


def ensure_inbound_tab() -> None:
    svc = _svc()
    meta = svc.spreadsheets().get(spreadsheetId=CFG.sheet_id).execute()
    tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}
    tab = CFG.inbound_log_tab
    if tab not in tabs:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=CFG.sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
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


def record(*, from_number: str, from_city: str, from_state: str,
           to_number: str, call_sid: str, action: str = "rejected") -> None:
    in_pool = "yes" if to_number in CFG.inbound_pool_numbers else "no"
    log.info("INBOUND call_sid=%s from=%s to=%s in_pool=%s -> %s",
             call_sid, from_number, to_number, in_pool, action)
    try:
        row = [
            datetime.now(timezone.utc).isoformat(),
            from_number, from_city, from_state, to_number, in_pool, action, call_sid,
        ]
        _svc().spreadsheets().values().append(
            spreadsheetId=CFG.sheet_id,
            range=f"{CFG.inbound_log_tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[str(c) for c in row]]},
        ).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("inbound log append failed (call_sid=%s): %s", call_sid, e)

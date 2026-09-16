"""Google Sheets access using keyless OAuth (user refresh token).

No service-account JSON key. We hold an OAuth client id/secret plus a long-lived
refresh token (obtained once via scripts/get_google_refresh_token.py) and mint
short-lived access tokens on demand. Authenticates as the Google user who
granted consent -- that user must be an editor of the target Sheet.
"""
from __future__ import annotations

import logging

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import CFG
from .queue_model import col_letter

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Column order for the results tab, reordered 2026-09-15 for readability (was
# previously append-only-at-the-end; a one-time migration moved the 35
# existing rows into this layout -- see scratchpad migration script history).
# Most-useful-first, technical/rarely-needed fields last (and hidden by
# default in the actual Sheet, via column-hide, not deleted).
HEADER = [
    "company_name",       # looked up from the Queue tab by phone -- not stored independently
    "phone_e164",
    "outcome",
    "logged_at_date",     # split from logged_at_time 2026-09-17 so email mail-merge can
    "logged_at_time",     # reference just the time; both in the CALLED COMPANY's own local timezone
    "ivr_fallback_flagged",
    "notes",
    "ivr_transcript",
    "from_number",
    "duration_sec",
    # --- hidden-by-default technical columns below ---
    "logged_at_iso",      # UTC/ISO, kept for any future machine use
    "answered_by",
    "twilio_call_status",
    "call_sid",
    "recording_url",
    "ivr_detected",
    "ivr_levels",
    "digits_sent",
    "classifier",
]


def _credentials() -> Credentials:
    CFG.require_google()
    return Credentials(
        token=None,
        refresh_token=CFG.google_refresh_token,
        client_id=CFG.google_client_id,
        client_secret=CFG.google_client_secret,
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )


def _service():
    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)


def ensure_tab_and_header() -> None:
    """Create the results tab if missing and write the header row if the tab is
    empty. Safe to call repeatedly."""
    svc = _service()
    meta = svc.spreadsheets().get(spreadsheetId=CFG.sheet_id).execute()
    tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}

    if CFG.sheet_tab not in tabs:
        log.info("Creating missing tab %r", CFG.sheet_tab)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=CFG.sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": CFG.sheet_tab}}}]},
        ).execute()

    existing = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=CFG.sheet_id, range=f"{CFG.sheet_tab}!A1:Z1")
        .execute()
        .get("values", [[]])
    )
    current = existing[0] if existing else []
    if current[: len(HEADER)] != HEADER:
        svc.spreadsheets().values().update(
            spreadsheetId=CFG.sheet_id,
            range=f"{CFG.sheet_tab}!A1",
            valueInputOption="RAW",
            body={"values": [HEADER]},
        ).execute()
        log.info("Wrote/updated header row on %r (%d columns)", CFG.sheet_tab, len(HEADER))


def append_result(row: dict) -> None:
    """Append one call result. `row` keys should match HEADER; missing keys are
    written as empty cells."""
    values = [[str(row.get(col, "")) for col in HEADER]]
    svc = _service()
    svc.spreadsheets().values().append(
        spreadsheetId=CFG.sheet_id,
        range=f"{CFG.sheet_tab}!A1",
        # RAW, not USER_ENTERED: otherwise Sheets parses "+18437939226" as a
        # number and drops the leading "+", corrupting the phone key.
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
    log.info("Appended result row for call_sid=%s outcome=%s", row.get("call_sid"), row.get("outcome"))


def backfill_call_status(call_sid: str, call_status: str, duration_sec: str = "") -> bool:
    """Patch twilio_call_status (and duration_sec, if given) on an ALREADY-
    LOGGED row. SignalWire/Twilio's terminal status callback ('completed',
    'busy', etc.) commonly arrives after we've already resolved the outcome
    from a faster IVR/AMD/gatekeeping signal and written the row -- without
    this, that later callback is silently dropped and the row keeps whatever
    interim status ('answered', 'in-progress') happened to be current at
    resolution time. Best-effort: returns False (does not raise) if the row
    can't be found, so a lookup miss never breaks webhook handling.
    """
    svc = _service()
    existing = (
        svc.spreadsheets()
        .values()
        .get(spreadsheetId=CFG.sheet_id, range=f"{CFG.sheet_tab}!A2:{col_letter(len(HEADER) - 1)}100000")
        .execute()
        .get("values", [])
    )
    sid_idx = HEADER.index("call_sid")
    status_idx = HEADER.index("twilio_call_status")
    dur_idx = HEADER.index("duration_sec")

    for i, raw in enumerate(existing):
        if sid_idx < len(raw) and raw[sid_idx] == call_sid:
            row_number = i + 2  # header is row 1
            data = [{"range": f"{CFG.sheet_tab}!{col_letter(status_idx)}{row_number}",
                     "values": [[call_status]]}]
            if duration_sec:
                data.append({"range": f"{CFG.sheet_tab}!{col_letter(dur_idx)}{row_number}",
                            "values": [[duration_sec]]})
            svc.spreadsheets().values().batchUpdate(
                spreadsheetId=CFG.sheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()
            log.info("Backfilled final status for call_sid=%s: %s (duration=%s)",
                     call_sid, call_status, duration_sec or "-")
            return True
    return False


def check_access() -> str:
    """Round-trip the Sheets API to confirm auth + access. Returns the sheet title."""
    svc = _service()
    meta = svc.spreadsheets().get(spreadsheetId=CFG.sheet_id).execute()
    return meta.get("properties", {}).get("title", "(untitled)")

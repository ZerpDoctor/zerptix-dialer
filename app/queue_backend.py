"""Queue backends: SheetQueue (production) and MemoryQueue (simulation).

Both expose the same tiny interface the scheduler needs:
    read_rows() -> list[QueueRow]
    update_fields(row_number, {col_name: value})
    find_by_phone(phone) -> QueueRow | None
"""
from __future__ import annotations

import logging

from .config import CFG
from .queue_model import HEADER, QueueRow, col_letter, row_from_dict

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Google Sheets backend
# --------------------------------------------------------------------------- #

def _svc():
    from . import google_sheets
    return google_sheets._service()


def ensure_queue_tab(tab: str) -> None:
    """Create the tab if missing and (re)write the header row if it doesn't match."""
    svc = _svc()
    meta = svc.spreadsheets().get(spreadsheetId=CFG.sheet_id).execute()
    tabs = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab not in tabs:
        log.info("Creating Queue tab %r", tab)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=CFG.sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
    existing = (
        svc.spreadsheets().values()
        .get(spreadsheetId=CFG.sheet_id, range=f"{tab}!A1:AZ1")
        .execute().get("values", [[]])
    )
    current = existing[0] if existing else []
    if current[: len(HEADER)] != HEADER:
        svc.spreadsheets().values().update(
            spreadsheetId=CFG.sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [HEADER]},
        ).execute()
        log.info("Wrote Queue header on %r (%d columns)", tab, len(HEADER))


class SheetQueue:
    def __init__(self, tab: str | None = None) -> None:
        self.tab = tab or CFG.sched_queue_tab

    def read_rows(self) -> list[QueueRow]:
        svc = _svc()
        # Range must cover every HEADER column -- hardcoding a fixed letter
        # here silently truncates any column appended after it (this bit us
        # for consecutive_answered_count, appended at the end of HEADER: reads
        # always saw "" for that column even though writes landed correctly).
        last_col = col_letter(len(HEADER) - 1)
        vals = (
            svc.spreadsheets().values()
            .get(spreadsheetId=CFG.sheet_id, range=f"{self.tab}!A2:{last_col}")
            .execute().get("values", [])
        )
        rows: list[QueueRow] = []
        for i, raw in enumerate(vals):
            if not any(c.strip() for c in raw):
                continue
            rows.append(QueueRow(row_number=i + 2, raw=[str(c) for c in raw]))
        return rows

    def find_by_phone(self, phone: str) -> QueueRow | None:
        phone = (phone or "").strip()
        return next((r for r in self.read_rows() if r.phone_e164 == phone), None)

    def update_fields(self, row_number: int, fields: dict) -> None:
        data = []
        for name, value in fields.items():
            c = col_letter(HEADER.index(name))
            data.append({"range": f"{self.tab}!{c}{row_number}", "values": [[str(value)]]})
        if not data:
            return
        _svc().spreadsheets().values().batchUpdate(
            spreadsheetId=CFG.sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()

    def append_rows(self, dicts: list[dict]) -> None:
        values = [[str(d.get(h, "")) for h in HEADER] for d in dicts]
        _svc().spreadsheets().values().append(
            spreadsheetId=CFG.sheet_id,
            range=f"{self.tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()

    def clear_data_rows(self) -> None:
        last_col = col_letter(len(HEADER) - 1)
        _svc().spreadsheets().values().clear(
            spreadsheetId=CFG.sheet_id, range=f"{self.tab}!A2:{last_col}100000"
        ).execute()


# --------------------------------------------------------------------------- #
# In-memory backend (simulation)
# --------------------------------------------------------------------------- #

class MemoryQueue:
    def __init__(self, dicts: list[dict]) -> None:
        # row_number is 1-based with a header at 1, matching SheetQueue.
        self._rows = [row_from_dict(d, row_number=i + 2) for i, d in enumerate(dicts)]

    def read_rows(self) -> list[QueueRow]:
        return list(self._rows)

    def find_by_phone(self, phone: str) -> QueueRow | None:
        return next((r for r in self._rows if r.phone_e164 == (phone or "").strip()), None)

    def update_fields(self, row_number: int, fields: dict) -> None:
        for r in self._rows:
            if r.row_number == row_number:
                for name, value in fields.items():
                    r.raw[HEADER.index(name)] = str(value)
                return

    def as_dicts(self) -> list[dict]:
        return [{h: r.raw[i] for i, h in enumerate(HEADER)} for r in self._rows]

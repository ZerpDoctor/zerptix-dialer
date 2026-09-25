"""The `Queue` tab: per-company state store (spec section 2).

The full spec-section-2 header is written so the tab is future-proof; the
scheduler only reads/writes the columns this phase needs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import CFG

# Column order == spec section 2. Index in this list == 0-based column index.
HEADER = [
    "company_name",              # 0  A
    "phone_e164",                # 1  B   -- join key to the Calls log + in-flight lock
    "domain",                    # 2  C
    "city",                      # 3  D
    "state",                     # 4  E
    "timezone",                  # 5  F   -- IANA name; blank/invalid => row skipped + flagged
    "source",                    # 6  G
    "current_quarter_attempts",  # 7  H
    "last_call_date",            # 8  I   -- YYYY-MM-DD, company-local
    "last_call_window",          # 9  J   -- evening | deep_night
    "last_outcome",              # 10 K
    "this_quarter_status",       # 11 L   -- in_progress | confirmed_miss | confirmed_covered
    "next_eligible_date",        # 12 M   -- YYYY-MM-DD, company-local
    "miss_timestamp",            # 13 N   -- ISO 8601
    "recording_url",             # 14 O
    "ivr_fallback_flagged",      # 15 P
    "email_track",               # 16 Q   -- untouched this phase
    "replied_or_closed",         # 17 R
    "do_not_call",               # 18 S
    "call_sid_history",          # 19 T   -- comma-separated, idempotency (spec section 6)
    "last_updated_at",           # 20 U
    # Appended at the END, not inserted mid-row, so the 21 already-populated
    # columns on real Queue rows don't shift meaning.
    "consecutive_answered_count", # 21 V -- resets on any miss; quarter reset also zeroes it
]

STATUS_IN_PROGRESS = "in_progress"
STATUS_CONFIRMED_MISS = "confirmed_miss"
STATUS_CONFIRMED_COVERED = "confirmed_covered"

# Outcomes that count as a "usable miss" for the quarter (spec section 7): stop
# attempting this company once any of these is logged. Note ivr_unresolved and
# unknown are deliberately NOT here -- an inconclusive call is not evidence.
MISS_OUTCOMES = {"voicemail", "extended_hold", "no_answer", "busy", "disconnected",
                  "gatekeeping_miss", "alt_miss"}
COVERED_OUTCOMES = {"answered"}
# A single answered call is not decisive (unlike a single miss) -- only after
# this many CONSECUTIVE answered outcomes in the same quarter, with no miss in
# between, does this_quarter_status flip to confirmed_covered. Any miss resets
# the streak to 0 immediately and always overrides, regardless of prior answers.
ANSWERS_TO_COVER = 4

_TRUE = {"true", "yes", "y", "1", "x", "t"}


def _b(v: str) -> bool:
    return str(v).strip().lower() in _TRUE


_DIGITS_RE = re.compile(r"\d+")


def normalize_phone(raw: str) -> str:
    """Best-effort E.164 normalization: strips spaces/dashes/parens/dots,
    keeps a leading '+' if present, and assumes US (+1) for a bare 10-digit
    number. Never guesses past what it's confident about -- an unrecognizable
    shape is returned unchanged, so a genuinely broken number still fails
    loudly downstream (a clear SignalWire rejection) rather than being
    silently mangled here. Sheet cells are never rewritten; this only affects
    what the app sees when it reads phone_e164.
    """
    raw = (raw or "").strip()
    if not raw:
        return raw
    had_plus = raw.startswith("+")
    digits = "".join(_DIGITS_RE.findall(raw))
    if not digits:
        return raw
    if had_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return raw


def col_letter(idx0: int) -> str:
    """0-based column index -> spreadsheet letter (0->A)."""
    s = ""
    n = idx0
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


@dataclass
class QueueRow:
    row_number: int  # 1-based sheet row; 2 == first data row
    raw: list[str]

    def _g(self, name: str) -> str:
        i = HEADER.index(name)
        return self.raw[i].strip() if i < len(self.raw) else ""

    def set(self, name: str, value) -> None:
        """Mutate the in-memory row (pads short rows). Does NOT persist."""
        i = HEADER.index(name)
        if i >= len(self.raw):
            self.raw.extend([""] * (i + 1 - len(self.raw)))
        self.raw[i] = str(value)

    # identity
    @property
    def company_name(self) -> str: return self._g("company_name")
    @property
    def phone_e164(self) -> str: return normalize_phone(self._g("phone_e164"))
    @property
    def timezone(self) -> str: return self._g("timezone")
    @property
    def state(self) -> str: return self._g("state")

    # gating
    @property
    def is_dnc(self) -> bool: return _b(self._g("do_not_call"))
    @property
    def is_closed(self) -> bool: return _b(self._g("replied_or_closed"))
    @property
    def attempts(self) -> int:
        try:
            return int(self._g("current_quarter_attempts") or "0")
        except ValueError:
            return 0

    @property
    def consecutive_answered(self) -> int:
        try:
            return int(self._g("consecutive_answered_count") or "0")
        except ValueError:
            return 0

    @property
    def last_call_date(self) -> str: return self._g("last_call_date")
    @property
    def last_call_window(self) -> str: return self._g("last_call_window")
    @property
    def this_quarter_status(self) -> str:
        return self._g("this_quarter_status") or STATUS_IN_PROGRESS
    @property
    def next_eligible_date(self) -> str: return self._g("next_eligible_date")
    @property
    def last_outcome(self) -> str: return self._g("last_outcome")
    @property
    def call_sid_history(self) -> str: return self._g("call_sid_history")
    @property
    def recording_url(self) -> str: return self._g("recording_url")
    @property
    def ivr_fallback_flagged(self) -> str: return self._g("ivr_fallback_flagged")


def append_csv(existing: str, value: str) -> str:
    parts = [p for p in (existing or "").split(",") if p.strip()]
    if value and value not in parts:
        parts.append(value)
    return ",".join(parts)


def row_from_dict(d: dict, row_number: int = 0) -> QueueRow:
    return QueueRow(row_number=row_number, raw=[str(d.get(h, "")) for h in HEADER])


def next_window_for(row: QueueRow) -> str:
    """Which window (evening/deep_night) this row's NEXT attempt should use,
    alternating from its last one. Shared by scheduler.py (deciding whether a
    row is due right now) and queue_writer.py (self-healing a record_attempt
    that never wrote through -- see apply_outcome's fallback)."""
    if not row.last_call_window:
        return CFG.sched_first_window
    return "deep_night" if row.last_call_window == "evening" else "evening"

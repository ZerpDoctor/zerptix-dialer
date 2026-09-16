"""Timezone validation, and best-effort *proposals* for the fill_timezones helper.

The scheduler only ever trusts an explicit, valid IANA name in the Queue's
`timezone` column (see parse_tz). The state / area-code maps below are used ONLY
by `python -m app.fill_timezones` to suggest values a human then reviews.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo, available_timezones

_AVAILABLE = available_timezones()


def parse_tz(name: str) -> ZoneInfo | None:
    name = (name or "").strip()
    if not name or name not in _AVAILABLE:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


# US state -> the single most-populous IANA zone in that state.
STATE_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
}

# States that span >1 zone -- a state-only guess is unsafe; flag for review.
SPLIT_STATES = {
    "AK", "FL", "ID", "IN", "KS", "KY", "MI", "NE", "ND", "OR", "SD", "TN", "TX",
}

# Area codes that pin a split-state row to the non-dominant zone. Small on
# purpose -- only the clear cases. Everything else falls back to STATE_TZ.
AREACODE_TZ = {
    # Texas -- El Paso / far west = Mountain
    "915": "America/Denver",
    # Florida panhandle (west of the Apalachicola) = Central
    "850": "America/Chicago",
    # Kansas -- far west = Mountain
    "620": "America/Chicago", "785": "America/Chicago",
    # Nebraska -- panhandle = Mountain
    "308": "America/Denver",
    # North Dakota -- southwest = Mountain
    "701": "America/Chicago",
    # Oregon -- most of the state is Pacific; Malheur County (part of 541/458) is Mountain
    # Idaho -- north (208/986 Panhandle) is Pacific; south is Mountain
    # Tennessee -- east (423, part of 865) = Eastern
    "423": "America/New_York", "865": "America/New_York",
    # Kentucky -- west (270/364) = Central
    "270": "America/Chicago", "364": "America/Chicago",
    # Michigan -- 4 UP counties (906) partly Central; keep Detroit default
    # Indiana -- NW (219) + SW (812 partly) = Central
    "219": "America/Chicago",
}


def propose(state: str, phone_e164: str) -> tuple[str | None, str]:
    """Return (proposed_iana_or_None, note). Never authoritative."""
    st = (state or "").strip().upper()
    ac = ""
    p = (phone_e164 or "").strip()
    if p.startswith("+1") and len(p) >= 5:
        ac = p[2:5]

    if st not in STATE_TZ:
        return None, f"unknown state {state!r}; set timezone manually"

    if st in SPLIT_STATES and ac in AREACODE_TZ:
        return AREACODE_TZ[ac], f"split state {st}, area code {ac} -> pinned; VERIFY"
    if st in SPLIT_STATES:
        return STATE_TZ[st], f"split state {st}: guessed dominant zone; VERIFY against city/area code"
    return STATE_TZ[st], f"from state {st}"

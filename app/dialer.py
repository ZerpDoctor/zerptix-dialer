"""Provider dispatch: routes place_call/hang_up to Twilio or SignalWire based
on CFG.telephony_provider, so callers (app/server.py) never import a specific
provider module directly. Both provider modules keep their own DialError
class (untouched); this module normalizes to one DialError type.
"""
from __future__ import annotations

from .config import CFG


class DialError(RuntimeError):
    """A call could not be placed. Carries a human-readable reason."""


def place_call(to_number: str, *, from_number: str | None = None) -> str:
    """`from_number` (SignalWire number-pool distribution) is only meaningful
    on the SignalWire path; the Twilio path has no pool concept and ignores it."""
    if CFG.telephony_provider == "twilio":
        from . import twilio_dialer
        try:
            return twilio_dialer.place_call(to_number)
        except twilio_dialer.DialError as e:
            raise DialError(str(e)) from e
    from . import signalwire_dialer
    try:
        return signalwire_dialer.place_call(to_number, from_number=from_number)
    except signalwire_dialer.DialError as e:
        raise DialError(str(e)) from e


def hang_up(call_sid: str) -> None:
    if CFG.telephony_provider == "twilio":
        from . import twilio_dialer
        twilio_dialer.hang_up(call_sid)
        return
    from . import signalwire_dialer
    signalwire_dialer.hang_up(call_sid)

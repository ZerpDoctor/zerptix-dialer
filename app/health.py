"""Pre-dial health gate and error circuit breaker.

A call placed while a dependency is down cannot be redone. 2026-09-29: the
Anthropic balance ran out mid-batch, 56 of 100 calls pressed menu digits by
keyword fallback (some wrong: Johnston pressed 0, not emergency) or were
mislabeled, and those companies lost an attempt. preflight.py only *reported*
that; this stops the dialing.

Two independent protections, both consulted by scheduler.tick before it places
a call:

1. Live checks -- Anthropic, Deepgram, Google Sheets, SignalWire, dialer-web --
   re-run every gate_recheck_ok_seconds while healthy and every
   gate_recheck_fail_seconds while blocked. Any failure blocks dialing and
   raises an alert; recovery raises another.

2. Circuit breaker -- if several of the most recent calls carry dependency
   errors in their notes (Anthropic unavailable/credit balance/Sheet write
   failed), dialing stops even though the live checks may still pass (a check
   can succeed while real traffic fails). It stays open for a cooldown AND
   until the live checks pass again, and only errors logged after it tripped
   count toward tripping it again.
"""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import alerts
from .config import CFG

log = logging.getLogger("dialer.health")

ERROR_MARKERS = ("anthropic unavailable", "anthropic api error", "credit balance", "sheet write failed")
BREAKER_COOLDOWN_SECONDS = 300


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


# --------------------------------------------------------------------------- #
# individual live checks -- each returns a Check and never raises
# --------------------------------------------------------------------------- #

def check_anthropic() -> Check:
    if not CFG.anthropic_api_key:
        return Check("anthropic", False, "ANTHROPIC_API_KEY not set")
    from .anthropic_client import AnthropicUnavailable, classify_ivr_digit
    last = ""
    for _ in range(2):  # one retry: a malformed model reply is noise, a billing error is not
        try:
            classify_ivr_digit("press 1 for sales, press 2 for support")
            return Check("anthropic", True)
        except AnthropicUnavailable as e:
            last = str(e)
        except Exception as e:  # noqa: BLE001
            last = repr(e)
    return Check("anthropic", False, last[:160])


def check_deepgram() -> Check:
    """Open (and immediately close) the same streaming connection production
    uses. Deliberately NOT an account/project API call: a key scoped only for
    streaming can be forbidden from those, which would wrongly block a whole
    night. No audio is sent, so nothing is billed."""
    if not CFG.stream_transcription_enabled:
        return Check("deepgram", True, "streaming transcription is off")
    if not CFG.deepgram_api_key:
        return Check("deepgram", False, "DEEPGRAM_API_KEY not set")
    import simple_websocket
    from . import media_stream
    last = ""
    for _ in range(2):
        try:
            ws = simple_websocket.Client(media_stream._DEEPGRAM_WS_URL,
                                         headers={"Authorization": f"Token {CFG.deepgram_api_key}"})
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
            return Check("deepgram", True, "streaming connection opens")
        except Exception as e:  # noqa: BLE001
            last = repr(e)
    return Check("deepgram", False, last[:160])


def check_sheets() -> Check:
    try:
        from . import google_sheets
        google_sheets.check_access()
        return Check("sheets", True)
    except Exception as e:  # noqa: BLE001
        return Check("sheets", False, repr(e)[:160])


def check_signalwire() -> Check:
    if CFG.telephony_provider != "signalwire":
        return Check("signalwire", True, f"provider is {CFG.telephony_provider}")
    try:
        url = (f"https://{CFG.signalwire_space_url}/api/laml/2010-04-01/"
               f"Accounts/{CFG.signalwire_project_id}.json")
        auth = base64.b64encode(f"{CFG.signalwire_project_id}:{CFG.signalwire_api_token}".encode()).decode()
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"}),
                                    timeout=15) as r:
            data = json.loads(r.read().decode())
        status = str(data.get("status", "")).lower()
        return Check("signalwire", status in ("active", ""), f"status {status or '?'}")
    except Exception as e:  # noqa: BLE001
        return Check("signalwire", False, repr(e)[:160])


def check_dialer_web() -> Check:
    try:
        with urllib.request.urlopen(CFG.callback_url("healthz"), timeout=10) as r:
            return Check("dialer-web", r.status == 200, f"HTTP {r.status}")
    except Exception as e:  # noqa: BLE001
        return Check("dialer-web", False, repr(e)[:160])


def run_checks() -> list[Check]:
    return [check_anthropic(), check_deepgram(), check_sheets(), check_signalwire(), check_dialer_web()]


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

class DialGate:
    """`allow(recent_rows)` -> (ok, reason). `recent_rows` are today's Calls
    rows as returned by google_sheets.calls_activity (the scheduler already
    reads them for the nightly cap, so the breaker costs no extra read)."""

    def __init__(self, checks_fn: Callable[[], list[Check]] = run_checks,
                 notes_fn: Callable[[list[int]], dict[int, str]] | None = None,
                 clock: Callable[[], float] = time.time):
        self._checks_fn = checks_fn
        self._notes_fn = notes_fn
        self._clock = clock
        self.ok: bool | None = None          # None = never checked
        self.reason = ""
        self.next_check_at = 0.0
        self.tripped_until = 0.0
        self.ignore_before_iso = ""          # only errors logged after the last trip count

    # -- public ------------------------------------------------------------
    def allow(self, recent_rows: list[dict] | None = None) -> tuple[bool, str]:
        if not CFG.gate_enabled:
            return True, ""
        now = self._clock()

        trip = self._breaker(now, recent_rows or [])
        if trip:
            self.ok = False
            self.reason = trip
            self.tripped_until = now + BREAKER_COOLDOWN_SECONDS
            self.next_check_at = self.tripped_until      # live checks must pass after the cooldown
            self.ignore_before_iso = self._now_iso(now)
            alerts.send("Dialing STOPPED: error circuit breaker tripped", trip, key="breaker")
            return False, self.reason

        if now < self.tripped_until:
            return False, self.reason

        if now >= self.next_check_at:
            failed = [c for c in self._checks_fn() if not c.ok]
            if failed:
                self.reason = "health check failed: " + "; ".join(f"{c.name} ({c.detail})" for c in failed)
                if self.ok is not False:
                    alerts.send("Dialing STOPPED: health check failed", self.reason, key="gate-fail")
                self.ok = False
                self.next_check_at = now + CFG.gate_recheck_fail_seconds
            else:
                if self.ok is False:
                    alerts.send("Dialing RESUMED: health checks pass again", "", level="WARN", key="gate-ok")
                self.ok = True
                self.reason = ""
                self.next_check_at = now + CFG.gate_recheck_ok_seconds
        return bool(self.ok), self.reason

    # -- breaker -------------------------------------------------------------
    def _breaker(self, now: float, rows: list[dict]) -> str:
        if not rows:
            return ""
        cutoff = self._now_iso(now - CFG.breaker_window_minutes * 60)
        floor = max(cutoff, self.ignore_before_iso)
        recent = sorted((r for r in rows if r["logged_at_iso"] > floor), key=lambda r: r["logged_at_iso"])
        recent = recent[-CFG.breaker_window_calls:]
        if len(recent) < CFG.breaker_min_errors or self._notes_fn is None:
            return ""
        notes = self._notes_fn([r["row"] for r in recent])
        errs = [r for r in recent if any(m in notes.get(r["row"], "").lower() for m in ERROR_MARKERS)]
        if len(errs) >= CFG.breaker_min_errors:
            sample = notes.get(errs[-1]["row"], "")[:140]
            return (f"{len(errs)} of the last {len(recent)} calls carry dependency errors "
                    f"(latest: {sample!r})")
        return ""

    @staticmethod
    def _now_iso(ts: float) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

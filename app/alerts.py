"""Alerts: make a dialing stop impossible to miss.

Every alert is (1) logged at ERROR so it is in Railway's logs, (2) appended to
an "Alerts" tab in the same Google Sheet (durable, and readable without
Railway access), and (3) POSTed to ALERT_WEBHOOK_URL if one is set (a
Slack/Discord-style webhook; both `text` and `content` are sent). Everything
is best-effort -- an alert failure must never take the scheduler down -- and
the same alert is not repeated within ALERT_REPEAT_SECONDS.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone

from .config import CFG

log = logging.getLogger("dialer.alerts")

ALERT_REPEAT_SECONDS = 1800
_last_sent: dict[str, float] = {}
ALERTS_TAB = "Alerts"


def send(title: str, detail: str = "", *, level: str = "ERROR", key: str | None = None) -> bool:
    """Returns True if the alert was emitted, False if suppressed as a repeat."""
    key = key or title
    now = time.time()
    if now - _last_sent.get(key, 0.0) < ALERT_REPEAT_SECONDS:
        return False
    _last_sent[key] = now
    text = f"[Zerptix Dialer] {title}" + (f" -- {detail}" if detail else "")
    (log.error if level == "ERROR" else log.warning)("ALERT: %s", text)
    _to_sheet(title, detail, level)
    _to_webhook(text)
    return True


def _to_sheet(title: str, detail: str, level: str) -> None:
    try:
        from . import google_sheets
        google_sheets.append_alert(ALERTS_TAB, datetime.now(timezone.utc).isoformat(), level, title, detail)
    except Exception as e:  # noqa: BLE001
        log.warning("alert not written to the Sheet: %s", e)


def _to_webhook(text: str) -> None:
    url = CFG.alert_webhook_url
    if not url:
        return
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text, "content": text}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001
        log.warning("alert webhook failed: %s", e)

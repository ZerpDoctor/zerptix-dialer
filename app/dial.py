"""CLI: place one outbound test call by asking the running server to place it.

Usage:
    python -m app.dial +15551234567

The web server (python -m app.server) must already be running locally, and its
PUBLIC_BASE_URL must be reachable by Twilio (ngrok locally / Railway domain in
prod) or no outcome will be logged.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request

from .config import CFG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dialer.dial")


def main() -> None:
    parser = argparse.ArgumentParser(description="Place one outbound test call.")
    parser.add_argument("to_number", help="Destination number in E.164 format, e.g. +15551234567")
    parser.add_argument(
        "--server",
        default=f"http://127.0.0.1:{CFG.port}",
        help="Base URL of the locally running dialer server.",
    )
    args = parser.parse_args()
    to_number = args.to_number.strip()

    if not to_number.startswith("+"):
        log.error("Number must be E.164 format (start with +). Got: %s", to_number)
        sys.exit(2)

    payload = json.dumps({"to_number": to_number}).encode()
    req = urllib.request.Request(
        f"{args.server.rstrip('/')}/calls",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            log.info("Call placed. CallSid=%s to=%s", body.get("call_sid"), body.get("to"))
            log.info("Watch the server logs for AMD + status callbacks and the Sheet append.")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.loads(detail).get("error", detail)
        except Exception:  # noqa: BLE001
            pass
        log.error("Server refused the call (HTTP %s): %s", e.code, detail)
        sys.exit(1)
    except urllib.error.URLError as e:
        log.error(
            "Could not reach the dialer server at %s (%s). "
            "Is `python -m app.server` running?",
            args.server, e,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

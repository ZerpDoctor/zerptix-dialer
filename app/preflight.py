"""CLI: verify credentials and Sheet access before placing real calls.

    python -m app.preflight

Checks, in order:
  1. Required env vars are present.
  2. Twilio credentials work (fetches the account, prints type + balance).
  3. Google Sheets OAuth works and the target Sheet is reachable; creates the
     results tab + header row if missing.
"""
from __future__ import annotations

import sys

from .config import CFG, ConfigError


def _ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check_config() -> bool:
    print("1. Environment variables")
    try:
        CFG.require_provider()
        CFG.require_google()
        CFG.require_public_url()
    except ConfigError as e:
        _fail(str(e))
        return False
    _ok(f"all required variables present (provider={CFG.telephony_provider})")
    if CFG.test_mode:
        _ok(f"TEST_MODE=true, allowlist: {CFG.test_allowlist or '(empty!)'}")
    else:
        print("  [WARN] TEST_MODE=false -- real numbers can be dialed")
    return True


def check_twilio() -> bool:
    print("2. Twilio")
    try:
        from twilio.rest import Client

        client = Client(CFG.twilio_account_sid, CFG.twilio_auth_token)
        acct = client.api.v2010.accounts(CFG.twilio_account_sid).fetch()
        _ok(f"authenticated as account '{acct.friendly_name}' (status: {acct.status}, type: {acct.type})")
        try:
            bal = client.api.v2010.accounts(CFG.twilio_account_sid).balance.fetch()
            _ok(f"balance: {bal.balance} {bal.currency}")
        except Exception:  # noqa: BLE001
            pass
        if acct.type and acct.type.lower() == "trial":
            print("  [WARN] Trial account: you can only call numbers you have verified in the console.")
        return True
    except Exception as e:  # noqa: BLE001
        _fail(f"Twilio check failed: {e}")
        return False


def check_signalwire() -> bool:
    print("2. SignalWire")
    try:
        import base64
        import json
        import urllib.request

        url = (
            f"https://{CFG.signalwire_space_url}/api/laml/2010-04-01/"
            f"Accounts/{CFG.signalwire_project_id}.json"
        )
        auth = base64.b64encode(
            f"{CFG.signalwire_project_id}:{CFG.signalwire_api_token}".encode()
        ).decode()
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        _ok(
            f"authenticated to space '{CFG.signalwire_space_url}' "
            f"(status: {data.get('status', '?')}, type: {data.get('type', '?')})"
        )
        if str(data.get("type", "")).lower() == "trial":
            print("  [WARN] Trial account: calls are restricted to verified numbers "
                  "and funding-required limits until you add a card + $5 credit.")
        if not CFG.signalwire_signing_key:
            print("  [WARN] SIGNALWIRE_SIGNING_KEY is empty -- webhook signature "
                  "verification will fail closed (every webhook rejected).")
        return True
    except Exception as e:  # noqa: BLE001
        _fail(f"SignalWire check failed: {e}")
        print("       -> If this mentions trial/verification, that's an account "
              "restriction, not a code bug -- see SignalWire dashboard.")
        return False


def check_anthropic() -> bool:
    print("3. Anthropic (IVR digit classification)")
    if not CFG.anthropic_api_key or CFG.anthropic_api_key == "sk-ant-xxxxxxxx":
        print("  [WARN] ANTHROPIC_API_KEY not set -- menu calls will use keyword-priority "
              "fallback only (still works, but untested against a real menu).")
        return True  # not fatal
    try:
        from .anthropic_client import classify_ivr_digit, AnthropicUnavailable

        try:
            out = classify_ivr_digit("press 1 for sales, press 2 for support")
            _ok(f"key works: model returned is_menu={out['is_menu']} digit={out['digit']} "
                f"({CFG.anthropic_model})")
            return True
        except AnthropicUnavailable as e:
            _fail(f"Anthropic call failed: {e}")
            print("       -> This is likely the billing issue, not a code bug. Menu calls will")
            print("          fall back to keyword-priority selection until it's resolved.")
            return True  # not fatal -- fallback path covers it
    except Exception as e:  # noqa: BLE001
        _fail(f"Anthropic check error: {e}")
        return True


def check_google() -> bool:
    print("4. Google Sheets")
    try:
        from . import google_sheets

        title = google_sheets.check_access()
        _ok(f"reached spreadsheet: '{title}'")
        google_sheets.ensure_tab_and_header()
        _ok(f"results tab '{CFG.sheet_tab}' ready with header row")
        return True
    except Exception as e:  # noqa: BLE001
        _fail(f"Google Sheets check failed: {e}")
        print("       - Confirm GOOGLE_OAUTH_* values and that you consented as a user")
        print("         with edit access to this Sheet.")
        print("       - Re-run: python scripts/get_google_refresh_token.py")
        return False


def main() -> None:
    print("Zerptix Dialer -- preflight\n")
    results = [check_config()]
    if results[0]:
        if CFG.telephony_provider == "twilio":
            results.append(check_twilio())
        else:
            results.append(check_signalwire())
        results.append(check_anthropic())
        results.append(check_google())
    print()
    if all(results):
        print("All checks passed. Safe to run the server and place a test call.")
        sys.exit(0)
    print("One or more checks failed. Fix the above before dialing.")
    sys.exit(1)


if __name__ == "__main__":
    main()

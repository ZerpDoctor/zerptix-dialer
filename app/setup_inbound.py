"""Point every dialer-pool number's Voice webhook at /inbound/voice.

    python -m app.setup_inbound             # show current Voice config of each pool number
    python -m app.setup_inbound --apply     # set them all identically

"Every number in the pool must behave identically" (spec section 10) is enforced
here, not per-number in the console: --apply writes the SAME voice_url /
voice_method to every number in INBOUND_POOL_NUMBERS and clears anything (a TwiML
App, a fallback URL) that could override it.

Needs PUBLIC_BASE_URL set. Dispatches on CFG.telephony_provider. The SignalWire
branch talks to the same Compatibility REST surface as app/signalwire_dialer.py
(IncomingPhoneNumbers resource) using the same field names Twilio's REST API
uses (voice_url, voice_method, voice_application_sid, voice_fallback_url,
status_callback) -- SignalWire's compat layer mirrors Twilio's JSON shape for
Calls.json, so this is expected to hold, but INBOUND_POOL_NUMBERS is empty as
of this migration so this path is UNVERIFIED against a real account. Run it
and check the printed listing before trusting --apply.
"""
from __future__ import annotations

import argparse
import sys

from .config import CFG


def _twilio_list_and_apply(pool: list[str], target_url: str, apply: bool) -> list[str]:
    from twilio.rest import Client

    client = Client(CFG.twilio_account_sid, CFG.twilio_auth_token)
    owned = {n.phone_number: n for n in client.incoming_phone_numbers.list(limit=1000)}

    missing = []
    for number in pool:
        n = owned.get(number)
        if n is None:
            print(f"  {number:16} NOT FOUND on this Twilio account")
            missing.append(number)
            continue
        aligned = (
            n.voice_url == target_url
            and (n.voice_method or "").upper() == "POST"
            and not n.voice_application_sid
        )
        print(f"  {number:16} voice_url={n.voice_url or '(none)'} "
              f"method={n.voice_method or '-'} app_sid={n.voice_application_sid or '-'} "
              f"{'OK' if aligned else 'NEEDS UPDATE'}")
        if apply and not aligned:
            client.incoming_phone_numbers(n.sid).update(
                voice_url=target_url,
                voice_method="POST",
                voice_application_sid="",
                voice_fallback_url="",
                status_callback="",
            )
    return missing


def _signalwire_list_and_apply(pool: list[str], target_url: str, apply: bool) -> list[str]:
    import base64
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    base = f"https://{CFG.signalwire_space_url}/api/laml/2010-04-01/Accounts/{CFG.signalwire_project_id}"
    auth = "Basic " + base64.b64encode(
        f"{CFG.signalwire_project_id}:{CFG.signalwire_api_token}".encode()
    ).decode()

    def get(path: str) -> dict:
        req = urllib.request.Request(f"{base}/{path}", headers={"Authorization": auth})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def post(path: str, params: dict) -> dict:
        body = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"{base}/{path}", data=body, method="POST",
            headers={"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    try:
        listing = get("IncomingPhoneNumbers.json")
    except urllib.error.HTTPError as e:
        print(f"  could not list SignalWire numbers (HTTP {e.code}): {e.read().decode(errors='replace')}")
        sys.exit(1)

    owned = {n.get("phone_number"): n for n in listing.get("incoming_phone_numbers", [])}

    missing = []
    for number in pool:
        n = owned.get(number)
        if n is None:
            print(f"  {number:16} NOT FOUND on this SignalWire project")
            missing.append(number)
            continue
        aligned = (
            n.get("voice_url") == target_url
            and (n.get("voice_method") or "").upper() == "POST"
            and not n.get("voice_application_sid")
        )
        print(f"  {number:16} voice_url={n.get('voice_url') or '(none)'} "
              f"method={n.get('voice_method') or '-'} "
              f"app_sid={n.get('voice_application_sid') or '-'} "
              f"{'OK' if aligned else 'NEEDS UPDATE'}")
        if apply and not aligned:
            post(f"IncomingPhoneNumbers/{n['sid']}.json", {
                "VoiceUrl": target_url,
                "VoiceMethod": "POST",
                "VoiceApplicationSid": "",
                "VoiceFallbackUrl": "",
                "StatusCallback": "",
            })
    return missing


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the config (default: just list)")
    args = ap.parse_args()

    CFG.require_provider()
    CFG.require_public_url()
    target_url = CFG.callback_url("inbound/voice")

    pool = CFG.inbound_pool_numbers
    if not pool:
        print("INBOUND_POOL_NUMBERS is empty -- nothing to configure.")
        print(f"When you add pool numbers, they should point Voice -> {target_url}")
        return

    print(f"provider = {CFG.telephony_provider}")
    print(f"target voice_url = {target_url}\n")
    if CFG.telephony_provider == "twilio":
        missing = _twilio_list_and_apply(pool, target_url, args.apply)
    else:
        missing = _signalwire_list_and_apply(pool, target_url, args.apply)

    print()
    if missing:
        print(f"  {len(missing)} pool number(s) not found: {missing}")
    if args.apply:
        print("  --apply run complete; re-run without --apply to confirm.")
    else:
        print("  run again with --apply to write the config.")
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()

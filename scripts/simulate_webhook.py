"""Send signed, simulated Twilio traffic at the running server.

Exercises the full pipeline -- signature verification, IVR menu detection + DTMF
selection, idempotency, outcome resolution, Sheet append -- WITHOUT placing a
real call. Payloads carry a real X-Twilio-Signature computed from
TWILIO_AUTH_TOKEN (against PUBLIC_BASE_URL + path, matching how the server
validates), so verification runs for real.

Two scenario kinds:
  * "status"   -- a fixed list of webhook POSTs (calls that never connect)
  * "call"     -- follows the TwiML like Twilio: POST /ivr/start, read the
                  <Gather>, post the next scripted SpeechResult to its action,
                  record any <Play digits="...">, loop; then fire AMD + status.

Usage:
    python scripts/simulate_webhook.py all
    python scripts/simulate_webhook.py ivr_single
    python scripts/simulate_webhook.py answered voicemail no_answer
"""
from __future__ import annotations

import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twilio.request_validator import RequestValidator  # noqa: E402

from app.config import CFG  # noqa: E402

# Both providers verify with the identical HMAC-SHA1(secret, url+sorted-params)
# scheme -- only the secret and header name differ (see app/server.py::_verify).
_IS_TWILIO = CFG.telephony_provider == "twilio"
_validator = RequestValidator(CFG.twilio_auth_token if _IS_TWILIO else CFG.signalwire_signing_key)
_SIGNATURE_HEADER = "X-Twilio-Signature" if _IS_TWILIO else "X-SignalWire-Signature"

# --- scenarios --------------------------------------------------------------

# "status" scenarios: calls that never connect.
STATUS_SCENARIOS: dict[str, list[tuple[str, dict]]] = {
    "no_answer": [
        ("/webhooks/status", {"CallStatus": "initiated"}),
        ("/webhooks/status", {"CallStatus": "ringing"}),
        ("/webhooks/status", {"CallStatus": "no-answer", "CallDuration": "0"}),
    ],
    "busy": [
        ("/webhooks/status", {"CallStatus": "initiated"}),
        ("/webhooks/status", {"CallStatus": "busy", "CallDuration": "0"}),
    ],
    "disconnected": [
        ("/webhooks/status", {"CallStatus": "initiated"}),
        ("/webhooks/status", {"CallStatus": "failed", "CallDuration": "0"}),
    ],
}

# "call" scenarios: connected calls. `speech` = successive <Gather> results
# ("" == silence / end of speech). `expect_*` are assertions printed at the end.
CALL_SCENARIOS: dict[str, dict] = {
    # --- non-IVR (regression cover for the original core loop) ---
    "answered": {
        "speech": ["hello, this is Dana", ""],
        "amd": "human",
        "expect_outcome": "answered",
        "expect_digits": [],
    },
    "voicemail": {
        "speech": ["", ""],
        "amd": "machine_end_beep",
        "expect_outcome": "voicemail",
        "expect_digits": [],
    },
    # --- IVR: DoD cases (spec section 13) ---
    "ivr_single": {
        "speech": [
            "thank you for calling. press 1 for emergency service, press 2 for billing questions",
            "emergency dispatch, this is Ray, what is your location",
        ],
        "expect_digits": ["1"],
        "expect_flagged": False,
        "expect_outcome": "answered",
    },
    "ivr_two_level": {
        "speech": [
            "thanks for calling Acme. press 2 for the service department, press 3 for new sales",
            "service department. press 1 for an after hours emergency, press 2 for general service",
            "after hours dispatch, this is Ray, what's the emergency",
        ],
        "expect_digits": ["2", "1"],
        "expect_outcome": "answered",
    },
    "ivr_ambiguous": {
        "speech": [
            "press 1 for sales, press 2 if you are a current customer, press 3 for billing",
            "customer care, you're speaking with Alex",
        ],
        "expect_digits": ["2"],
        "expect_flagged": True,
        "expect_outcome": "answered",
    },
    "ivr_false_positive_human": {
        "speech": ["hi, sorry, one sec", "okay hello, this is Dana speaking"],
        "amd": "human",
        "expect_digits": [],
        "expect_outcome": "answered",
    },
    "ivr_disclosure_only": {
        "speech": ["this call may be recorded for quality assurance purposes", ""],
        "amd": "human",
        "expect_digits": [],
        "expect_outcome": "answered",
    },
    "ivr_disclosure_then_menu_split": {
        "speech": [
            "this call may be recorded for quality and training purposes",
            "press 1 for our emergency line, press 2 for our main office",
            "emergency dispatch, this is Ray, what's your location",
        ],
        "expect_digits": ["1"],
        "expect_outcome": "answered",
    },
    "ivr_disclosure_then_menu_oneshot": {
        "speech": [
            "this call may be recorded for quality assurance purposes press 1 for our emergency line",
            "emergency dispatch, this is Ray speaking, go ahead",
        ],
        "expect_digits": ["1"],
        "expect_outcome": "answered",
    },
    # Menu navigated but the party we reach can't be auto-classified -> flagged
    # for manual recording review (spec section 4 anti-corruption intent).
    "ivr_unresolved_tail": {
        "speech": [
            "press 1 to speak with a representative, press 2 to leave a message",
            "yeah hang on",
            "",
        ],
        "expect_digits": ["1"],
        "expect_outcome": "ivr_unresolved",
    },
    # --- digital gatekeeping (distinct from IVR menu navigation) ---
    "gatekeeping_zip_code": {
        "speech": [
            "to better assist you today, please enter your zip code followed by the pound sign",
        ],
        "expect_digits": [],
        "expect_outcome": "gatekeeping_miss",
    },
    # Same "account number" phrase as above, but WITH a real digit-press option
    # this time -- must go through normal IVR navigation, NOT gatekeeping.
    "ivr_account_info_with_digit": {
        "speech": [
            "please have your account number ready. press 1 for emergency service, press 2 for billing questions",
            "emergency dispatch, this is Ray, what is your location",
        ],
        "expect_digits": ["1"],
        "expect_flagged": False,
        "expect_outcome": "answered",
    },
    # --- transcript-vs-AMD reconciliation (2026-09-14 incident regression) ---
    # AMD's fast Enable-mode "machine_start" is not reliable -- confirmed live
    # tonight, it misclassified real human pickups as voicemail. The transcript
    # must override a wrong AMD verdict when Haiku is confident.
    "false_machine_live_human": {
        "speech": ["Capital Fire and Water, this is Demetrius, is this an emergency", ""],
        "amd": "machine_start",
        "expect_digits": [],
        "expect_outcome": "answered",
    },
    # Regression: a genuine voicemail with AMD correctly agreeing must stay
    # voicemail -- the fix must not flip correct cases just because it now
    # looks at the transcript.
    "genuine_voicemail_amd_agrees": {
        "speech": ["the person you're trying to reach is not available at the tone please record your message", ""],
        "amd": "machine_start",
        "expect_digits": [],
        "expect_outcome": "voicemail",
    },
    # Reverse direction, proving the fix is symmetric: AMD wrongly claims
    # human, but the transcript is unambiguous voicemail -- transcript wins.
    "false_human_actual_voicemail": {
        "speech": ["the mailbox you have reached is full and cannot accept any messages at this time please try your call again later", ""],
        "amd": "human",
        "expect_digits": [],
        "expect_outcome": "voicemail",
    },
}

_ACTION_RE = re.compile(r'action="([^"]+)"')
_PLAY_RE = re.compile(r'<Play digits="([^"]+)"')


def _fake_sid() -> str:
    return "CA" + secrets.token_hex(16)


def _post(path: str, params: dict) -> tuple[int, str]:
    public_url = CFG.public_base_url.rstrip("/") + path
    local_url = f"http://127.0.0.1:{CFG.port}{path}"
    signature = _validator.compute_signature(public_url, params)
    req = urllib.request.Request(
        local_url,
        data=urllib.parse.urlencode(params).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            _SIGNATURE_HEADER: signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _base_params(sid: str, to: str) -> dict:
    return {
        "CallSid": sid,
        "AccountSid": CFG.twilio_account_sid if _IS_TWILIO else CFG.signalwire_project_id,
        "From": CFG.twilio_from_number if _IS_TWILIO else CFG.signalwire_from_number,
        "To": to,
    }


def run_status_scenario(name: str, steps: list[tuple[str, dict]]) -> None:
    sid = _fake_sid()
    to = CFG.test_allowlist[0] if CFG.test_allowlist else "+15555550123"
    print(f"\n=== {name}  (CallSid={sid}) ===")
    for path, extra in steps:
        status, text = _post(path, {**_base_params(sid, to), **extra})
        label = extra.get("CallStatus", "?")
        print(f"  {path:24} {label:16} -> {'ok' if status in (200, 204) else f'HTTP {status}'}")
        time.sleep(0.3)


def run_call_scenario(name: str, sc: dict) -> None:
    sid = _fake_sid()
    to = CFG.test_allowlist[0] if CFG.test_allowlist else "+15555550123"
    print(f"\n=== {name}  (CallSid={sid}, To={to}) ===")

    status, twiml = _post("/ivr/start", _base_params(sid, to))
    print(f"  /ivr/start               -> HTTP {status}")

    speeches = list(sc.get("speech", []))
    digits_pressed: list[str] = _PLAY_RE.findall(twiml)
    idx = 0
    for _ in range(12):  # hard loop guard
        m = _ACTION_RE.search(twiml)
        if not m or "<Gather" not in twiml:
            break
        action_path = urllib.parse.urlparse(m.group(1)).path
        speech = speeches[idx] if idx < len(speeches) else ""
        idx += 1
        status, twiml = _post(
            action_path, {**_base_params(sid, to), "SpeechResult": speech, "Confidence": "0.9"}
        )
        digits_pressed += _PLAY_RE.findall(twiml)
        print(f"  {action_path:24} speech={speech[:40]!r:46} -> HTTP {status}")
        time.sleep(0.3)

    if sc.get("amd"):
        st, _ = _post("/webhooks/amd", {**_base_params(sid, to), "AnsweredBy": sc["amd"]})
        print(f"  /webhooks/amd             AnsweredBy={sc['amd']:16} -> HTTP {st}")
    st, _ = _post("/webhooks/status", {**_base_params(sid, to), "CallStatus": "completed", "CallDuration": "22"})
    print(f"  /webhooks/status          completed        -> HTTP {st}")

    exp_d = sc.get("expect_digits")
    if exp_d is not None:
        ok = digits_pressed == exp_d
        print(f"  digits pressed: {digits_pressed or '[]'}  (expected {exp_d}) {'OK' if ok else 'MISMATCH'}")
    print("  -> check server log 'LOGGED ...' and the Sheet for outcome"
          + (f" (expected {sc['expect_outcome']})" if sc.get("expect_outcome") else ""))
    if "expect_flagged" in sc:
        print(f"  -> expected ivr_fallback_flagged = {'yes' if sc['expect_flagged'] else 'no'}")


_FORBIDDEN_INBOUND = ("<Say", "<Play", "<Dial", "<Record", "<Gather", "<Pause", "<Number", "<Sip")


def run_inbound_scenario() -> None:
    sid = _fake_sid()
    pool = CFG.inbound_pool_numbers
    to = pool[0] if pool else "+15550999999"  # rejected either way; in_pool just logged
    print(f"\n=== inbound  (CallSid={sid}, To={to}, in_pool={'yes' if pool else 'no'}) ===")
    params = {
        "CallSid": sid,
        "AccountSid": CFG.twilio_account_sid if _IS_TWILIO else CFG.signalwire_project_id,
        "From": "+15551234567", "To": to, "FromCity": "AUSTIN", "FromState": "TX",
        "Direction": "inbound", "CallStatus": "ringing",
    }
    status, body = _post("/inbound/voice", params)
    body = body.strip()
    print(f"  /inbound/voice  -> HTTP {status}")
    print(f"  response: {body}")

    ok = True
    if status != 200:
        ok = False
        print(f"  MISMATCH: expected HTTP 200, got {status}")
    if "<Reject" not in body:
        ok = False
        print("  MISMATCH: response has no <Reject>")
    hit = [v for v in _FORBIDDEN_INBOUND if v in body]
    if hit:
        ok = False
        print(f"  MISMATCH: response contains forbidden verb(s): {hit}")
    print(f"  {'OK -- call is rejected, nothing spoken/recorded' if ok else 'FAILED'}")
    print("  -> check the Inbound tab for a new row")


def main() -> None:
    names = sys.argv[1:]
    all_names = ["inbound", *STATUS_SCENARIOS, *CALL_SCENARIOS]
    if not names or names == ["all"]:
        names = all_names
    bad = [n for n in names if n not in all_names]
    if bad:
        print(f"unknown scenario(s): {bad}\navailable: {all_names}")
        sys.exit(2)

    CFG.require_public_url()
    for name in names:
        if name == "inbound":
            run_inbound_scenario()
        elif name in STATUS_SCENARIOS:
            run_status_scenario(name, STATUS_SCENARIOS[name])
        else:
            run_call_scenario(name, CALL_SCENARIOS[name])
        time.sleep(0.5)


if __name__ == "__main__":
    main()

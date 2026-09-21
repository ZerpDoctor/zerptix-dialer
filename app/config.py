"""Central environment / configuration loading.

Everything the app needs from the environment is read here once, validated, and
exposed as a single `CFG` object so the rest of the code never touches os.environ
directly.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or malformed."""


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and (val is None or val.strip() == ""):
        raise ConfigError(
            f"Missing required environment variable: {name}. "
            f"See .env.example for what it should contain."
        )
    return val if val is not None else ""


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _spreadsheet_id(url_or_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_id)
    if m:
        return m.group(1)
    # Assume the caller passed a bare ID.
    return url_or_id.strip()


def _e164_list(raw: str) -> list[str]:
    return [n.strip() for n in raw.split(",") if n.strip()]


def _num_cap_map(raw: str) -> dict[str, int]:
    """Parse 'number:cap,number:cap,...' into a dict. Malformed entries are
    skipped rather than raising -- a bad cap value should not block startup."""
    out: dict[str, int] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        num, _, cap = pair.partition(":")
        num, cap = num.strip(), cap.strip()
        if num and cap.isdigit():
            out[num] = int(cap)
    return out


@dataclass
class Config:
    # Twilio
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    twilio_validate_signature: bool

    # Public URL
    public_base_url: str

    # Google
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    sheet_id: str
    sheet_tab: str

    # Anthropic (used for IVR digit classification)
    anthropic_api_key: str
    anthropic_model: str = "claude-haiku-4-5"

    # Dialer behaviour
    test_mode: bool = True
    test_allowlist: list[str] = field(default_factory=list)
    machine_detection: str = "Enable"
    amd_wait_seconds: int = 6
    record_calls: bool = False
    transcribe_calls: bool = False  # SignalWire Transcribe/TranscribeCallback on the
    # whole-call REST recording -- undocumented whether this combination works
    # (SignalWire only documents transcribe on the <Record> verb, not the Calls
    # resource's Record=true); being tested live 2026-09-20/21. No effect unless
    # record_calls is also true.
    port: int = 8080

    # IVR navigation
    ivr_enabled: bool = True
    ivr_master_timeout_seconds: int = 60
    ivr_max_gather_cycles: int = 5
    ivr_speech_model: str = "phone_call"
    ivr_initial_timeout_seconds: int = 5
    ivr_tail_gather_seconds: int = 5
    ivr_confirm_gather_seconds: int = 7  # short window used only when turn 1
    # already sounds like a confident live pickup, waiting on turn 2 to
    # confirm -- deliberately much shorter than ivr_tail_gather_seconds so a
    # real person isn't left in dead air for that long (see ivr_turn's
    # gather_count==1 fast-confirm branch in server.py).
    gatekeeping_detection_enabled: bool = True  # kill switch, independent of IVR_ENABLED

    # Scheduler / Sheet-as-queue (spec section 7)
    sched_queue_tab: str = "Queue"
    sched_sim_queue_tab: str = "Queue_SIM"
    sched_evening_window: str = "21:30-22:30"
    sched_deep_night_window: str = "02:00-04:00"
    sched_calling_days: str = "Sun,Mon,Tue,Wed,Thu"
    sched_min_days_between_attempts: int = 10
    sched_max_attempts_per_quarter: int = 4
    sched_first_window: str = "evening"
    sched_tick_seconds: int = 60
    sched_nightly_cap: int = 0  # 0 = unlimited. Process-lifetime count, resets daily (UTC).
    sched_dial_pacing_seconds: float = 6.0  # delay between dials within one tick, so a
    # full batch doesn't all go live within the same few seconds (was overwhelming
    # downstream capacity and the Sheets read quota -- see incident 2026-09-20).
    # Raised 2s->6s same night: 2s cut the unknown-outcome rate roughly in half
    # (47%->25-33%) but didn't close it, and raising OUR gunicorn threads had
    # NOT helped at all -- pointing at a concurrent-call constraint on
    # SignalWire's side, not ours, so pushing pacing further is the next cheap
    # (free) lever to pull before building anything new.

    # Inbound / callback number pool (spec sections 9-10)
    inbound_pool_numbers: list[str] = field(default_factory=list)
    inbound_log_tab: str = "Inbound"
    inbound_reject_reason: str = "rejected"  # "rejected" (SIT/fast-busy) or "busy"

    # Telephony provider selection (spec: SignalWire migration) -----------------
    # Which provider actually places calls / owns the active webhook contract.
    # The Twilio path is left fully intact behind this switch in case Twilio
    # ever clears compliance review.
    telephony_provider: str = "signalwire"  # "signalwire" | "twilio"

    # SignalWire (Compatibility/cXML REST API -- Twilio-shaped, different host +
    # credentials). Plain HTTP REST, no SDK: see app/signalwire_dialer.py.
    signalwire_space_url: str = ""        # e.g. your-space.signalwire.com (no scheme)
    signalwire_project_id: str = ""       # == "AccountSid" on the compat REST surface
    signalwire_api_token: str = ""
    signalwire_from_number: str = ""
    signalwire_signing_key: str = ""      # separate from api_token -- webhook signing secret
    signalwire_validate_signature: bool = True

    # Number pool (extends the single signalwire_from_number above). Falls back
    # to [signalwire_from_number] when SIGNALWIRE_FROM_NUMBERS isn't set, so a
    # single-number setup behaves exactly as before.
    signalwire_from_numbers: list[str] = field(default_factory=list)
    # Per-number nightly cap override, e.g. {"+1555...": 15}. A pool number
    # without an entry here falls back to sched_nightly_cap as its default.
    signalwire_nightly_caps: dict[str, int] = field(default_factory=dict)

    def require_twilio(self) -> None:
        for k in ("twilio_account_sid", "twilio_auth_token", "twilio_from_number"):
            if not getattr(self, k):
                raise ConfigError(f"Twilio not configured: {k} is empty (see .env.example).")

    def require_signalwire(self) -> None:
        for k in ("signalwire_space_url", "signalwire_project_id",
                  "signalwire_api_token", "signalwire_from_number"):
            if not getattr(self, k):
                raise ConfigError(f"SignalWire not configured: {k} is empty (see .env.example).")

    def require_provider(self) -> None:
        if self.telephony_provider == "twilio":
            self.require_twilio()
        else:
            self.require_signalwire()

    @property
    def from_number(self) -> str:
        return self.twilio_from_number if self.telephony_provider == "twilio" else self.signalwire_from_number

    def require_google(self) -> None:
        for k in ("google_client_id", "google_client_secret", "google_refresh_token", "sheet_id"):
            if not getattr(self, k):
                raise ConfigError(f"Google Sheets not configured: {k} is empty (see .env.example).")

    def require_public_url(self) -> None:
        if not self.public_base_url or not self.public_base_url.startswith("http"):
            raise ConfigError(
                "PUBLIC_BASE_URL must be set to the https URL Twilio can reach this "
                "service at (ngrok URL locally, Railway domain in prod)."
            )

    def callback_url(self, path: str) -> str:
        return self.public_base_url.rstrip("/") + "/" + path.lstrip("/")


def load() -> Config:
    _sw_from_single = _get("SIGNALWIRE_FROM_NUMBER")
    _sw_pool = _e164_list(_get("SIGNALWIRE_FROM_NUMBERS", ""))
    return Config(
        twilio_account_sid=_get("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_get("TWILIO_AUTH_TOKEN"),
        twilio_from_number=_get("TWILIO_FROM_NUMBER"),
        twilio_validate_signature=_bool("TWILIO_VALIDATE_SIGNATURE", True),
        public_base_url=_get("PUBLIC_BASE_URL"),
        google_client_id=_get("GOOGLE_OAUTH_CLIENT_ID"),
        google_client_secret=_get("GOOGLE_OAUTH_CLIENT_SECRET"),
        google_refresh_token=_get("GOOGLE_OAUTH_REFRESH_TOKEN"),
        sheet_id=_spreadsheet_id(_get("GOOGLE_SHEET_URL")),
        sheet_tab=_get("GOOGLE_SHEET_TAB", "Calls"),
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        anthropic_model=_get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        test_mode=_bool("TEST_MODE", True),
        test_allowlist=_e164_list(_get("TEST_ALLOWLIST", "")),
        machine_detection=_get("MACHINE_DETECTION", "Enable"),
        amd_wait_seconds=int(_get("AMD_WAIT_SECONDS", "6") or "6"),
        record_calls=_bool("RECORD_CALLS", False),
        transcribe_calls=_bool("TRANSCRIBE_CALLS", False),
        port=int(_get("PORT", "8080") or "8080"),
        ivr_enabled=_bool("IVR_ENABLED", True),
        ivr_master_timeout_seconds=int(_get("IVR_MASTER_TIMEOUT_SECONDS", "60") or "60"),
        ivr_max_gather_cycles=int(_get("IVR_MAX_GATHER_CYCLES", "5") or "5"),
        ivr_speech_model=_get("IVR_SPEECH_MODEL", "phone_call"),
        ivr_initial_timeout_seconds=int(_get("IVR_INITIAL_TIMEOUT_SECONDS", "5") or "5"),
        ivr_tail_gather_seconds=int(_get("IVR_TAIL_GATHER_SECONDS", "5") or "5"),
        ivr_confirm_gather_seconds=int(_get("IVR_CONFIRM_GATHER_SECONDS", "7") or "7"),
        gatekeeping_detection_enabled=_bool("GATEKEEPING_DETECTION_ENABLED", True),
        sched_queue_tab=_get("SCHED_QUEUE_TAB", "Queue"),
        sched_sim_queue_tab=_get("SCHED_SIM_QUEUE_TAB", "Queue_SIM"),
        sched_evening_window=_get("SCHED_EVENING_WINDOW", "21:30-22:30"),
        sched_deep_night_window=_get("SCHED_DEEP_NIGHT_WINDOW", "02:00-04:00"),
        sched_calling_days=_get("SCHED_CALLING_DAYS", "Sun,Mon,Tue,Wed,Thu"),
        sched_min_days_between_attempts=int(_get("SCHED_MIN_DAYS_BETWEEN_ATTEMPTS", "10") or "10"),
        sched_max_attempts_per_quarter=int(_get("SCHED_MAX_ATTEMPTS_PER_QUARTER", "4") or "4"),
        sched_first_window=_get("SCHED_FIRST_WINDOW", "evening"),
        sched_tick_seconds=int(_get("SCHED_TICK_SECONDS", "60") or "60"),
        sched_nightly_cap=int(_get("SCHED_NIGHTLY_CAP", "0") or "0"),
        sched_dial_pacing_seconds=float(_get("SCHED_DIAL_PACING_SECONDS", "6.0") or "6.0"),
        inbound_pool_numbers=_e164_list(_get("INBOUND_POOL_NUMBERS", "")),
        inbound_log_tab=_get("INBOUND_LOG_TAB", "Inbound"),
        inbound_reject_reason=_get("INBOUND_REJECT_REASON", "rejected"),
        telephony_provider=_get("TELEPHONY_PROVIDER", "signalwire").strip().lower(),
        signalwire_space_url=_get("SIGNALWIRE_SPACE_URL"),
        signalwire_project_id=_get("SIGNALWIRE_PROJECT_ID"),
        signalwire_api_token=_get("SIGNALWIRE_API_TOKEN"),
        signalwire_from_number=_sw_from_single,
        signalwire_signing_key=_get("SIGNALWIRE_SIGNING_KEY"),
        signalwire_validate_signature=_bool("SIGNALWIRE_VALIDATE_SIGNATURE", True),
        signalwire_from_numbers=_sw_pool if _sw_pool else ([_sw_from_single] if _sw_from_single else []),
        signalwire_nightly_caps=_num_cap_map(_get("SIGNALWIRE_NIGHTLY_CAPS", "")),
    )


CFG = load()

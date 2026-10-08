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

    # Deepgram (real-time transcription over a SignalWire Media Stream --
    # replaces SignalWire's own Gather speech recognition, which is the
    # dominant cost line, ~12x Deepgram's rate for the same audio). Feature-
    # flagged: OFF by default, so nothing about tonight's real calling
    # changes until explicitly enabled and validated.
    deepgram_api_key: str = ""
    stream_transcription_enabled: bool = False
    # ONE-OFF DIAGNOSTIC, 2026-09-25: isolates whether SignalWire's "Speech
    # Recognition" billing line is coming from Gather's own engine (even in
    # dtmf-only mode) or from the Stream verb itself -- real calls today
    # billed Speech Recognition at basically the same rate whether or not
    # Gather requested speech input, and every test had Stream running
    # alongside Gather, so the two are still confounded. When true, /ivr/start
    # opens the Stream and then just Pauses/hangs up -- no Gather at all.
    # Remove this flag and its one call site once the answer is known.
    stream_diagnostic_no_gather: bool = False

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
    # Kept at 15 so ivr_max_gather_cycles * ivr_tail_gather_seconds (15*4=60s)
    # stays >= ivr_master_timeout_seconds -- the master timer should always be
    # the binding cap, not this cycle-count guard.
    ivr_max_gather_cycles: int = 15
    ivr_speech_model: str = "phone_call"
    ivr_initial_timeout_seconds: int = 5
    # Lowered 5->4 (was deployed at 20 on Railway) 2026-09-28: real incidents
    # (Florida's Elite Restoration, Response Flood & Fire) showed the digit
    # press after a menu decision landing too late -- since
    # STREAM_TRANSCRIPTION_ENABLED dropped speechTimeout="auto" from this
    # Gather, there's no more acoustic end-of-speech detection, so every
    # call site using this value (the post-press re-gather, the same-level
    # keep-listening loop, _conclude_not_menu's and _ivr_tail's re-gather)
    # only ever reacts after the FULL fixed window elapses, not when the far
    # end's prompt actually finishes. Both incidents showed the exact same
    # menu prompt repeating verbatim (Response Flood & Fire even captured an
    # explicit "That input was not valid") -- the far end's own listening
    # window for touch-tone input had already closed by the time our press
    # landed. Shortening this to check far more often doesn't truncate slow
    # speakers/long disclosures -- ivr_max_gather_cycles was raised to
    # compensate, and ivr_master_timeout_seconds independently caps total
    # call length regardless either way.
    ivr_tail_gather_seconds: int = 4
    ivr_confirm_gather_seconds: int = 7  # short window used only when turn 1
    # already sounds like a confident live pickup, waiting on turn 2 to
    # confirm -- deliberately much shorter than ivr_tail_gather_seconds so a
    # real person isn't left in dead air for that long (see ivr_turn's
    # gather_count==1 fast-confirm branch in server.py).
    # "Listen like a human": never decide (press a digit, resolve, hang up)
    # while the far end is still mid-speech. Deepgram interim/final results
    # arrive continuously while someone is talking, so "no transcript
    # activity for listen_quiet_seconds" is the end-of-speech signal the
    # SignalWire Gather used to provide before the Deepgram migration
    # dropped speechTimeout="auto". A turn that lands mid-speech is deferred
    # (re-checked after listen_recheck_seconds), at most listen_max_defers
    # times in a row so a looping menu can't defer forever.
    listen_gate_enabled: bool = True   # kill switch
    listen_quiet_seconds: float = 1.2
    listen_max_defers: int = 10
    # While a menu is being read, allow far more deferrals: a 25-30s prompt must
    # not make the gate fail open and press before the last option is heard.
    listen_menu_max_defers: int = 30
    # 1s (was 2s): a phone system only waits ~3-5s for input after its prompt,
    # so a 2s recheck could land after the window had closed.
    listen_recheck_seconds: int = 1
    # Pressing a digit needs a longer silence than merely "not mid-word": the
    # menu must be over. 2.0s of quiet, or the menu repeating, then press.
    # (Phone systems only wait ~3-5s for input after a prompt; 2026-09-30's
    # franchise tests pressed 3-10s after the prompt ended, and SERVPRO East
    # Nashville answered "Invalid input" before our digit arrived.)
    listen_press_quiet_seconds: float = 2.0
    listen_press_max_waits: int = 6
    # Media-stream recovery: a stream that never connects, or stops sending
    # audio, is restarted on the next turn (blank-transcript calls: 2 of 11
    # franchise tests -- "never connected", and "12 frames in a 48s call").
    stream_restart_enabled: bool = True
    # A call that shows a RINGING cadence in its audio and no text is not a finished call.
    # False = shadow: only note "would keep waiting" on the row. True = actually keep waiting
    # (bounded by the hold budget); a call still ringing at the cap is `unknown`, not a miss.
    ring_wait_enabled: bool = False
    # Days after the last email send (the date in the Queue's email_track cell) before a company may be
    # dialed or emailed again. Owner's rule, 2026-10-03.
    sched_email_hold_days: int = 90
    stream_max_restarts: int = 2
    # Hold budget: ivr_hold_budget_seconds is counted from the LAST digit
    # press (or from answer if none was pressed), so time spent in menus no
    # longer eats the wait for a person. ivr_master_timeout_seconds remains
    # the floor; ivr_hard_cap_seconds is the absolute ceiling and must stay
    # under the scheduler's pool wait backstop (150s).
    ivr_hold_budget_seconds: int = 60
    ivr_hard_cap_seconds: int = 110
    gatekeeping_detection_enabled: bool = True  # kill switch, independent of IVR_ENABLED

    # Scheduler / Sheet-as-queue (spec section 7)
    sched_queue_tab: str = "Queue"
    sched_sim_queue_tab: str = "Queue_SIM"
    sched_evening_window: str = "21:30-22:30"
    sched_deep_night_window: str = "02:00-04:00"
    sched_calling_days: str = "Sun,Mon,Tue,Wed,Thu"
    sched_min_days_between_attempts: int = 10
    # Across a quarter turn the 10-day gap is not enough: a company called 2 weeks before the turn was redialed 2 weeks
    # later (2026-10-06). Owner rule: at least this many days after the last call, whatever quarter it was in.
    sched_quarter_turn_min_days: int = 42
    # One company, several Queue rows (same website, different numbers): treat it as ONE company when deciding to dial
    # (2026-10-06: 21 companies were called on two numbers within two days).
    sched_sibling_block: bool = True
    # Companies whose second attempt is due in tonight's late-night window share the nightly cap with the evening window
    # (counted per UTC day). Without a reserve the evening window uses it all and the due calls starve (127 come due 10/11,
    # 140 on 10/14). The evening window may use at most (total cap - reserve); reserve = min(due, this fraction of the cap).
    # 0 turns the rule off.
    sched_late_reserve_fraction: float = 0.5
    sched_max_attempts_per_quarter: int = 4
    sched_first_window: str = "evening"
    sched_tick_seconds: int = 60
    sched_nightly_cap: int = 0  # 0 = unlimited. Process-lifetime count, resets daily (UTC).
    # Pre-dial health gate + error circuit breaker (app/health.py). Dialing is
    # refused unless Anthropic, Deepgram, Sheets, SignalWire and dialer-web
    # all pass a live check -- a call placed while a dependency is down can't
    # be redone (2026-09-29: the Anthropic balance ran out mid-batch and 56
    # calls pressed menu digits by keyword fallback, wrong ones included).
    gate_enabled: bool = True
    gate_recheck_ok_seconds: int = 600      # re-verify this often while healthy
    gate_recheck_fail_seconds: int = 60     # ...and this often while blocked
    breaker_min_errors: int = 3             # trip on >= this many dependency errors...
    breaker_window_calls: int = 8           # ...among the most recent this-many calls...
    breaker_window_minutes: int = 20        # ...logged within this many minutes
    alert_webhook_url: str = ""             # optional Slack/Discord-style webhook
    # Nightly cap counted from the Calls sheet (restart-proof) and, optionally,
    # spread across timezone windows so the first window can't use the whole
    # night's cap (2026-09-30: Eastern used it all; Central/Pacific were never
    # dialed). Fairness changes WHO gets called, so it is opt-in.
    sched_tz_fairness: bool = False
    sched_max_concurrent_calls: int = 5  # bounded dial pool (2026-09-21),
    # replacing an earlier fixed-delay pacing guess. Real calibration against
    # live franchise numbers: N=3/5/8 concurrent all came back 0% "unknown"
    # (blank AMD, zero transcript); an unpaced 21-at-once burst produced 81%
    # unknown -- a hard cliff somewhere between 8 and 21, not a gradual
    # slope, consistent with a real concurrent-call ceiling (most likely on
    # SignalWire's side, since raising OUR OWN gunicorn threads never helped
    # at all). 5 leaves real headroom below the confirmed-safe zone. See
    # scheduler.py::pooled_http_dialer.

    # Inbound / callback number pool (spec sections 9-10)
    inbound_pool_numbers: list[str] = field(default_factory=list)
    inbound_log_tab: str = "Inbound"
    inbound_reject_reason: str = "rejected"  # "rejected" (SIT/fast-busy) or "busy"

    # Durable AMD-verdict checkpoint (see app/amd_checkpoint.py) -- recovers a
    # real AnsweredBy signal that would otherwise be lost if a mid-call web
    # process restart wipes the in-memory CallStore before resolution.
    amd_checkpoint_tab: str = "AMD_Checkpoint"

    # Durable FULL-call-state checkpoint (see app/call_checkpoint.py) --
    # supersedes amd_checkpoint_tab's narrow AnsweredBy-only recovery with
    # full rehydration (transcript, digits pressed, navigation state) so a
    # mid-call restart can resume a call, not just log it more informatively
    # as unknown. amd_checkpoint_tab is kept as a harmless, redundant
    # extra safety net, not removed.
    call_checkpoint_tab: str = "Call_Checkpoint"

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

    def stream_url(self, path: str) -> str:
        """Same as callback_url but wss://, for the Media Stream verb --
        SignalWire connects out to this as a WebSocket, not a webhook."""
        base = self.public_base_url.rstrip("/")
        base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return base + "/" + path.lstrip("/")


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
        deepgram_api_key=_get("DEEPGRAM_API_KEY", ""),
        stream_transcription_enabled=_bool("STREAM_TRANSCRIPTION_ENABLED", False),
        stream_diagnostic_no_gather=_bool("STREAM_DIAGNOSTIC_NO_GATHER", False),
        test_mode=_bool("TEST_MODE", True),
        test_allowlist=_e164_list(_get("TEST_ALLOWLIST", "")),
        machine_detection=_get("MACHINE_DETECTION", "Enable"),
        amd_wait_seconds=int(_get("AMD_WAIT_SECONDS", "6") or "6"),
        record_calls=_bool("RECORD_CALLS", False),
        transcribe_calls=_bool("TRANSCRIBE_CALLS", False),
        port=int(_get("PORT", "8080") or "8080"),
        ivr_enabled=_bool("IVR_ENABLED", True),
        ivr_master_timeout_seconds=int(_get("IVR_MASTER_TIMEOUT_SECONDS", "60") or "60"),
        ivr_max_gather_cycles=int(_get("IVR_MAX_GATHER_CYCLES", "15") or "15"),
        ivr_speech_model=_get("IVR_SPEECH_MODEL", "phone_call"),
        ivr_initial_timeout_seconds=int(_get("IVR_INITIAL_TIMEOUT_SECONDS", "5") or "5"),
        ivr_tail_gather_seconds=int(_get("IVR_TAIL_GATHER_SECONDS", "4") or "4"),
        ivr_confirm_gather_seconds=int(_get("IVR_CONFIRM_GATHER_SECONDS", "7") or "7"),
        listen_gate_enabled=_bool("LISTEN_GATE_ENABLED", True),
        listen_quiet_seconds=float(_get("LISTEN_QUIET_SECONDS", "1.2") or "1.2"),
        listen_max_defers=int(_get("LISTEN_MAX_DEFERS", "10") or "10"),
        listen_menu_max_defers=int(_get("LISTEN_MENU_MAX_DEFERS", "30") or "30"),
        listen_recheck_seconds=int(_get("LISTEN_RECHECK_SECONDS", "1") or "1"),
        listen_press_quiet_seconds=float(_get("LISTEN_PRESS_QUIET_SECONDS", "2.0") or "2.0"),
        listen_press_max_waits=int(_get("LISTEN_PRESS_MAX_WAITS", "6") or "6"),
        stream_restart_enabled=_bool("STREAM_RESTART_ENABLED", True),
        ring_wait_enabled=_bool("RING_WAIT_ENABLED", False),
        sched_email_hold_days=int(_get("SCHED_EMAIL_HOLD_DAYS", "90") or "90"),
        stream_max_restarts=int(_get("STREAM_MAX_RESTARTS", "2") or "2"),
        ivr_hold_budget_seconds=int(_get("IVR_HOLD_BUDGET_SECONDS", "60") or "60"),
        ivr_hard_cap_seconds=int(_get("IVR_HARD_CAP_SECONDS", "110") or "110"),
        gatekeeping_detection_enabled=_bool("GATEKEEPING_DETECTION_ENABLED", True),
        sched_queue_tab=_get("SCHED_QUEUE_TAB", "Queue"),
        sched_sim_queue_tab=_get("SCHED_SIM_QUEUE_TAB", "Queue_SIM"),
        sched_evening_window=_get("SCHED_EVENING_WINDOW", "21:30-22:30"),
        sched_deep_night_window=_get("SCHED_DEEP_NIGHT_WINDOW", "02:00-04:00"),
        sched_calling_days=_get("SCHED_CALLING_DAYS", "Sun,Mon,Tue,Wed,Thu"),
        sched_min_days_between_attempts=int(_get("SCHED_MIN_DAYS_BETWEEN_ATTEMPTS", "10") or "10"),
        sched_quarter_turn_min_days=int(_get("SCHED_QUARTER_TURN_MIN_DAYS", "42") or "42"),
        sched_sibling_block=_bool("SCHED_SIBLING_BLOCK", True),
        sched_late_reserve_fraction=float(_get("SCHED_LATE_NIGHT_RESERVE", "0.5") or "0.5"),
        sched_max_attempts_per_quarter=int(_get("SCHED_MAX_ATTEMPTS_PER_QUARTER", "4") or "4"),
        sched_first_window=_get("SCHED_FIRST_WINDOW", "evening"),
        sched_tick_seconds=int(_get("SCHED_TICK_SECONDS", "60") or "60"),
        sched_nightly_cap=int(_get("SCHED_NIGHTLY_CAP", "0") or "0"),
        gate_enabled=_bool("GATE_ENABLED", True),
        gate_recheck_ok_seconds=int(_get("GATE_RECHECK_OK_SECONDS", "600") or "600"),
        gate_recheck_fail_seconds=int(_get("GATE_RECHECK_FAIL_SECONDS", "60") or "60"),
        breaker_min_errors=int(_get("BREAKER_MIN_ERRORS", "3") or "3"),
        breaker_window_calls=int(_get("BREAKER_WINDOW_CALLS", "8") or "8"),
        breaker_window_minutes=int(_get("BREAKER_WINDOW_MINUTES", "20") or "20"),
        alert_webhook_url=_get("ALERT_WEBHOOK_URL", ""),
        sched_tz_fairness=_bool("SCHED_TZ_FAIRNESS", False),
        sched_max_concurrent_calls=int(_get("SCHED_MAX_CONCURRENT_CALLS", "5") or "5"),
        inbound_pool_numbers=_e164_list(_get("INBOUND_POOL_NUMBERS", "")),
        inbound_log_tab=_get("INBOUND_LOG_TAB", "Inbound"),
        amd_checkpoint_tab=_get("AMD_CHECKPOINT_TAB", "AMD_Checkpoint"),
        call_checkpoint_tab=_get("CALL_CHECKPOINT_TAB", "Call_Checkpoint"),
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

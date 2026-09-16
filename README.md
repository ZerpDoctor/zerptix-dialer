# Zerptix AI Dialer

Orchestration service that reads a queue of companies from a Google Sheet,
places outbound calls via Twilio at each company's local calling windows,
navigates IVR menus, detects the call outcome, and writes per-company state back
to the Sheet.

> **Scope.** All of spec §3–4 (core loop + IVR), §7 (scheduler / Sheet-as-queue),
> and §9–10 (inbound / callback pool):
> - a `Queue` tab of companies → `tick()` figures out who's due right now in
>   their **own timezone** (evening 21:30–22:30 and deep-night 02:00–04:00,
>   Sun–Thu) → dials the eligible ones → the resolved outcome is written back to
>   that row. Cadence: ≤4 attempts/calendar-quarter, ≥10 days apart, alternating
>   windows, **stop the moment a usable miss is logged**.
> - inbound calls to dialer-pool numbers are **`<Reject>`ed** — never answered,
>   no audio, nothing spoken — and logged to an `Inbound` tab.
>
> Not built: number-pool rotation for *outbound* reputation management, batched
> Sheet writes, and alerting — see [`ai-dialer-claude-code-spec.md`](./ai-dialer-claude-code-spec.md) §10–11.

---

## How it works

```
 Queue tab ──► python -m app.scheduler (tick: who's due now, in their tz?)
      ▲                              │
      │                             ▼
      │             python -m app.server (persistent)                       Twilio
      │                             │                                        │
      │   POST /calls ─────────────►│── calls.create (async AMD on) ────────►│
                                        │◄── POST /ivr/start ─────────────────┤  (callee picks up)
                                        │      <Gather input="speech">        │
                                        │◄── POST /ivr/turn/menu/0 ───────────┤  (SpeechResult transcript)
                                        │   ├ looks like a menu? ── Haiku ── digit
                                        │   │    └─ <Play digits="X"/> + next <Gather>   (≤ 2 levels)
                                        │   └ not a menu / silence ── hand to AMD
                                        │◄── POST /ivr/turn/tail/0 ───────────┤  (post-navigation audio)
                                        │◄── POST /webhooks/amd ──────────────┤  (non-menu calls only)
                                        │◄── POST /webhooks/status ───────────┤  (completed / busy / no-answer / failed)
                                        │                                     │
                                        ├── resolve outcome (idempotent on CallSid)
                                        ├── append row ──► Calls tab (audit log)
                                        └── write state ──► Queue tab (attempts, status, next-eligible)
```

`python -m app.dial +1…` is still there for one-off manual calls; the scheduler
is the normal driver.

Inbound calls to pool numbers go to `POST /inbound/voice` → constant `<Reject>`
(never answers) → append a row to the `Inbound` tab. Nothing else in the system
is touched.

### IVR navigation details (spec §4)

- **Menu vs. not:** a keyword gate (needs real menu *structure* — a
  "press/select/dial" verb + digit, or "`<digit> for`") then a Claude Haiku
  confirmation. Low confidence or "not a menu" from Haiku → treated as **not a
  menu**, no DTMF sent, straight to AMD.
- **Listen-through:** the transcript **accumulates across `<Gather>` cycles**. A
  recording disclosure ("this call may be recorded…") that precedes the real
  menu instruction does **not** end the turn — only genuine silence (empty
  result), a confirmed menu, or the cycle / 60 s cap does.
- **Digit choice:** Haiku returns `{is_menu, digit, confidence, reasoning}`.
  On **any** Anthropic failure the server logs
  `ANTHROPIC API ERROR (check billing / key): …` and falls back to
  keyword priority — emergency/after-hours wins, else "reach a person", else
  lowest digit — and sets `ivr_fallback_flagged`.
- **Outcome for menu calls:** Twilio's AMD judged the *menu* audio, so menu
  calls resolve from a **post-navigation transcript classification** (human
  greeting → `answered`, "leave a message" → `voicemail`, hold language /
  silence → `extended_hold`, anything unclear → `ivr_unresolved`, flagged).
- **60 s master timer** covers navigation **and** hold detection combined —
  one budget, not two.
- **Digital gatekeeping (distinct from a menu):** an automated prompt asking
  for identifying info (zip code, account number, name, reason for calling)
  with **no digit-press option** at all. Only ever checked when the menu
  keyword gate has already said "not a menu" — a real digit-press option
  always wins and gets handled as a normal menu instead. Haiku-confirmed only
  (no keyword-only fallback: a false positive here hangs up and burns the
  quarter's remaining attempts, so an unavailable/low-confidence classifier
  means "not gatekeeping", not a guess). Resolves to `gatekeeping_miss` — same
  quarter effect as `voicemail`. Kill switch: `GATEKEEPING_DETECTION_ENABLED`.

- **Language:** Python 3.12, Flask + Gunicorn.
- **Google auth:** keyless OAuth. No service-account JSON key (blocked by org
  policy). We store an OAuth client id/secret + a long-lived **refresh token**
  and mint short-lived access tokens on demand. Calls authenticate as *your
  Google user*, who must have edit access to the Sheet. Works identically
  locally and on Railway.
- **Idempotency:** every webhook is Twilio-signature-verified, and outcome
  writes are keyed on `CallSid` so duplicate webhook delivery cannot double-log.
- **Test guard:** while `TEST_MODE=true`, the dialer refuses any number not in
  `TEST_ALLOWLIST`. This is enforced server-side, not just in the CLI.

### Known limitations of this version (intentional)

| Limitation | Why / when it's addressed |
|---|---|
| In-call state is in-process memory (`app/call_store.py`) | Fine for single-process, low volume. Gunicorn pinned to `--workers 1`. The *scheduler's* crash recovery does NOT depend on this — it uses the Sheet row (`last_call_date`) as the ledger. |
| Scheduler reads the whole `Queue` tab each tick | Fine for hundreds of rows. Needs pagination/caching at thousands. |
| `--workers 1` is required | The Queue write lock (`app/queue_writer.py`) is in-process, so all Queue writes must happen in one process. This is already required for the in-call `STORE`. A multi-instance deploy would need a durable lease instead. |
| Recording URL may not land in the row | Twilio posts the recording after the outcome is already written. `RECORD_CALLS=false` by default anyway (recording consent, spec §12). |
| No batching of Sheet writes | One append per call, a few cell writes per attempt. Fine at test volume; buffered batch writes come with real volume (spec §2). |
| `duration_sec` blank for `answered`/`voicemail` | Those resolve on the AMD callback (so we hang up immediately per spec §7); `CallDuration` only arrives later on the `completed` callback. Row is not backfilled yet. |
| Tail classification is keyword-only | Post-navigation human/voicemail/hold detection uses a keyword list, not Haiku. Ambiguous tails become `ivr_unresolved` (flagged) — safe but conservative. Haiku-assisted tail classification is a later improvement. |
| Partial speech results not used | We accumulate whole `<Gather>` results rather than streaming partials, so a menu instruction at the very tail of a long disclosure costs one extra gather cycle (still inside the 60 s budget). |
| No alerting | Spec §11 — next phases. |

---

## Setup

### 0. Prerequisites

- Python 3.12 (`python --version`)
- A SignalWire account (Space, Project ID, API Token, Signing Key) with a
  voice-capable number -- or a Twilio account (Account SID, Auth Token, voice
  number) if you set `TELEPHONY_PROVIDER=twilio`
- A Google account with **edit** access to the target Sheet
- An HTTPS tunnel for local testing. `cloudflared` needs no account:
  `Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile cloudflared.exe`
  then `.\cloudflared.exe tunnel --url http://localhost:8080`. (`ngrok http 8080`
  also works but needs a free signup + authtoken.)

### 1. Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
python -m pip install -r requirements.txt
```

### 2. Configure environment

```powershell
Copy-Item .env.example .env
```

Then fill in `.env` — see **"What to put in `.env`"** below.

### 3. Create the Google OAuth client + refresh token

1. [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services
   → Credentials** → **Create credentials → OAuth client ID**.
   - If prompted, configure the **OAuth consent screen**: User type *External*
     is fine; add your own Google account under **Test users**.
   - Application type: **Desktop app**. Name it anything.
2. Copy the **Client ID** and **Client secret** into `.env` as
   `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`.
3. Run the helper:
   ```powershell
   python scripts/get_google_refresh_token.py
   ```
   A browser opens. Sign in as the Google account with edit access to the Sheet
   and approve. Paste the printed `GOOGLE_OAUTH_REFRESH_TOKEN=...` line into `.env`.

> The service account you created is **not used** — OAuth-as-user sidesteps the
> key-creation org policy entirely. You do not need to share the Sheet with the
> service account.

### 4. Preflight

```powershell
python -m app.preflight
```

This verifies auth against the active `TELEPHONY_PROVIDER` (SignalWire or
Twilio), verifies Google Sheets access, and creates the results tab + header
row in your Sheet. Fix anything it flags before dialing.

### 5. Start the server + tunnel

In one terminal:

```powershell
python -m app.server
```

In another:

```powershell
ngrok http 8080
```

Copy the `https://....ngrok-free.app` URL ngrok prints into `.env` as
`PUBLIC_BASE_URL`, then **restart `python -m app.server`** so it picks up the
change.

> You do **not** need to configure any webhook URLs in the Twilio console for
> outbound calls — the app passes all callback URLs to Twilio per-call.

### 6. Place a test call

Add the destination number to `TEST_ALLOWLIST` in `.env` (restart the server),
then:

```powershell
python -m app.dial +15551234567
```

Watch the server terminal. You'll see the AMD result and status callbacks, then
`LOGGED call_sid=... outcome=...`. Check your Google Sheet for the new row.

### Testing without a real call (unfunded / trial-review account)

If your provider can't place calls yet (Twilio compliance review, SignalWire
trial mode), exercise the whole outcome pipeline with signed simulated
webhooks — a real signature for whichever provider is active
(`X-SignalWire-Signature` or `X-Twilio-Signature`), real code path
(verification, idempotency, outcome resolution, Sheet append). Only the
provider's actual dialing is skipped. Server + tunnel must be running and
`PUBLIC_BASE_URL` set.

```powershell
.\.venv\Scripts\python.exe scripts\simulate_webhook.py all
# or one/some scenarios by name (see the list below)
```

Then check the server log for `LOGGED ...` and the `Calls` tab. (You'll see a
harmless `Could not hang up call ...` warning per scenario — the REST hangup
isn't available on trial accounts; it works once funded.)

**Scenarios** — the "call" ones follow the returned TwiML like Twilio does
(posting the next scripted `SpeechResult` to each `<Gather>` action, recording
each `<Play digits>`):

| Scenario | What it checks |
|---|---|
| `inbound` | inbound call → exact `<Reject>` TwiML, no forbidden verbs, `Inbound` tab row |
| `no_answer` `busy` `disconnected` | call-status short-circuits (never connects) |
| `answered` `voicemail` | non-menu path, AMD-driven (core-loop regression cover) |
| `ivr_single` | single-level "press 1 for emergency service" → digit `1` |
| `ivr_two_level` | nested menu → `2` then `1`, then stop |
| `ivr_ambiguous` | no clear emergency option → keyword priority → `ivr_fallback_flagged` |
| `ivr_false_positive_human` | "hi, sorry, one sec" → **no DTMF**, proceeds to AMD |
| `ivr_disclosure_only` | "this call may be recorded…" then silence → not a menu |
| `ivr_disclosure_then_menu_split` | disclosure finalizes first, menu arrives next gather → digit still extracted from the tail |
| `ivr_disclosure_then_menu_oneshot` | disclosure + menu in one continuous result → digit extracted |
| `ivr_unresolved_tail` | menu navigated but the party reached can't be auto-classified → `ivr_unresolved`, flagged |
| `gatekeeping_zip_code` | automated prompt asks for a zip code, no digit option → hangs up, `gatekeeping_miss` |
| `ivr_account_info_with_digit` | mentions "your account number" but DOES offer a digit → normal IVR navigation, NOT gatekeeping (priority-order regression check) |

> **Testing the Haiku path.** With a placeholder or missing `ANTHROPIC_API_KEY`,
> every scenario runs the **keyword-priority fallback** (and logs the loud
> `ANTHROPIC API ERROR …` line — that's the point). To test whether *your*
> Anthropic key works end to end, put the real key in `.env`, restart the
> server, and run `ivr_single`: the row's `classifier` column will read `haiku`
> instead of `keyword_fallback` if the call succeeded.

### Exercising each outcome with a real call

| Outcome | How to produce it |
|---|---|
| `answered` | Answer the call and say "hello" like a person |
| `voicemail` | Let it ring to your carrier voicemail; stay silent after the beep |
| `no_answer` | Decline / ignore until Twilio times out |
| `busy` | Call a number that is currently on another call, or DND |
| `disconnected` | Dial a malformed / unassigned number (also sets a note) |
| `answered` via a menu | Record a greeting like "press 1 for support…", answer after the digit, speak a short greeting |
| `extended_hold` | Navigate a menu into hold music / silence, or don't resolve within 60 s |
| `ivr_unresolved` | Menu navigated but the audio after it isn't a clear human/voicemail — check the recording |
| `gatekeeping_miss` | An automated prompt asks for identifying info (zip code, account number, name, reason for calling) with no digit-press option -- hangs up immediately, same quarter effect as `voicemail` |

`unknown` (call completed but no signal gave a usable read) rounds out the full outcome list; it isn't something you'd deliberately provoke.

---

## Scheduler / Sheet-as-queue (spec §7)

### The `Queue` tab

A second tab (default name `Queue`) is the **per-company state store** — the
full spec-§2 column set. The `Calls` tab stays as the append-only per-attempt
audit log.

```powershell
python -m app.seed_queue --sample          # create the tab + a few test companies
python -m app.fill_timezones --dry-run      # propose `timezone` for blank rows; review, then drop --dry-run
```

Paste your real companies into the tab (columns match spec §2). **Every row
needs a valid IANA `timezone`** (`America/New_York`, …) — a row with a blank or
invalid timezone is **skipped and flagged**, never dialed on a guess.
`fill_timezones` only *proposes* values (from `state` + area code); split-zone
states are marked `VERIFY` for you to check against the company's city.

### Running it

```powershell
# one evaluation against the real Queue (needs the server running):
python -m app.scheduler tick

# see who WOULD be dialed at a given moment, change nothing:
python -m app.scheduler tick --dry-run --now "2026-09-07T22:00:00-04:00"

# loop forever, tick every SCHED_TICK_SECONDS (this is what Railway cron / a worker runs):
python -m app.scheduler run
```

The scheduler posts to the server's `/calls` (so TEST_MODE, the in-flight lock,
and webhook handling all stay in one process). When a call resolves, the
server writes the outcome back to that company's `Queue` row:

| resolved outcome | `this_quarter_status` | side effect |
|---|---|---|
| `voicemail` `extended_hold` `no_answer` `busy` `disconnected` | `confirmed_miss` + `miss_timestamp` | stop calling this quarter |
| `disconnected` | (as above) | also sets `do_not_call = true` (spec §5) |
| `answered` | `confirmed_covered` | stop calling this quarter |
| `ivr_unresolved` `unknown` | stays `in_progress` | inconclusive — keep attempting (up to 4) |
| Twilio API error (not a call outcome) | unchanged | **not counted as an attempt**; retried next tick |

### Eligibility rules (each `tick`, per row, all must hold)

1. not `do_not_call`, not `replied_or_closed`
2. `this_quarter_status == in_progress` and `current_quarter_attempts < 4`
3. company-local weekday is a calling day — **a calling day owns its evening and
   the 2–4 a.m. that follows it**, so with Sun–Thu the deep-night clock times
   are Mon–Fri
4. company-local time is inside the active window, and that window matches the
   company's **next** window (strict evening/deep-night alternation)
5. `today >= next_eligible_date` (≥10 days since the last attempt)
6. not already attempted today (crash/restart guard — the Sheet row is the
   ledger; a re-tick or restart mid-window sees `last_call_date == today` and skips)
7. no call to this number currently in-flight

Calendar-quarter reset (attempts → 0, status → `in_progress`) happens lazily at
the top of each tick.

### Testing a full quarterly cycle in seconds

```powershell
python -m app.sim_quarter            # ~130 virtual days across a quarter boundary
python -m app.sim_quarter --flush    # also write the final state to the Queue_SIM tab
```

Runs entirely in memory (no Twilio/Anthropic), with scripted per-company
outcomes, then prints a per-company timeline and **asserts the invariants**:
≤4 attempts/quarter, ≥10 days apart, windows alternate, stop-on-miss, only
Sun–Thu in local time, DNC/closed/no-timezone never dialed, quarter boundary
resets the count.

### The Queue write lock

All `Queue`-row writes go through `app/queue_writer.py`, serialized by a
per-phone `threading.RLock`. The **scheduler never writes the Queue** — when it
dials via `/calls` (with `record_attempt: true`) the *server* records the
attempt on the row, under the lock, in the same one `--workers 1` process that
handles webhooks and writes outcomes. There is no persistent lock state, so a
crash can't leave a row stuck (the OS drops every lock with the process).

```powershell
python -m app.sim_concurrency     # 25 threads race record_attempt + apply_outcome
```

Asserts: exactly one attempt recorded per (phone, day) under contention, every
CallSid survives in `call_sid_history` (writes merge, not clobber), no deadlock.

---

## Inbound / callback pool (spec §9–10)

Inbound calls to the dialer's own callback numbers must **never connect** — no
answer, no voicemail, no message. Twilio has no "ring forever" verb, so this is
done with **`<Reject>`**: it does not answer the call (no SIP 200, no media, $0),
and nothing is spoken or recorded.

- `POST /inbound/voice` — one webhook for the **whole pool**. Returns a constant
  `<Response><Reject reason="rejected"/></Response>` (a test asserts the exact
  bytes — no `<Say>`/`<Play>`/`<Dial>`/`<Record>`/`<Gather>` can appear). Then
  best-effort appends a row to the **`Inbound`** tab: `received_at, from_number,
  from_city, from_state, to_number, in_pool, action, call_sid`.
- `INBOUND_POOL_NUMBERS` — the pool, comma-separated E.164. **Separate from
  `TWILIO_FROM_NUMBER` and any real business number.** Empty is fine for now.
- `python -m app.setup_inbound [--apply]` — lists each pool number's Voice
  config; `--apply` points **every** number at `/inbound/voice` identically and
  clears anything that could override it (a TwiML App, fallback URL). This is
  what enforces "every number behaves identically — not a per-number setting".

**What a real caller hears:** ~1–2 rings of ringback while Twilio fetches the
webhook, then the "not in service" / fast-busy tone, then the call ends. It
**never connects**; Twilio logs it as `no-answer`, $0, 0-second duration. An
`Inbound` row appears within a second. (`INBOUND_REJECT_REASON=busy` gives a
busy signal instead.)

```powershell
python -m app.simulate... # -> scripts\simulate_webhook.py inbound
```

Posts a signed fake inbound webhook, asserts the response is exactly the reject
TwiML with no forbidden verbs, and that an `Inbound` row was written. The
real behavioural test (spec §13 DoD — "call a dialer number, confirm it rings
without connecting") waits until you have real pool numbers + a working Twilio
account.

---

## What to put in `.env`

| Variable | Where it comes from |
|---|---|
| `TELEPHONY_PROVIDER` | `signalwire` (current) or `twilio`. Selects which section below is read and which webhook signature scheme `/ivr/*` and `/webhooks/*` expect |
| `SIGNALWIRE_SPACE_URL` | SignalWire Dashboard, your Space subdomain (no scheme), e.g. `your-space.signalwire.com` |
| `SIGNALWIRE_PROJECT_ID` | Dashboard -> API Credentials -- Project ID (== `AccountSid` on the compat REST surface) |
| `SIGNALWIRE_API_TOKEN` | Dashboard -> API Credentials -- API Token |
| `SIGNALWIRE_FROM_NUMBER` | Your SignalWire voice number, E.164 |
| `SIGNALWIRE_FROM_NUMBERS` | Optional pool: comma-separated E.164 numbers the scheduler round-robins across. Falls back to `[SIGNALWIRE_FROM_NUMBER]` if blank |
| `SIGNALWIRE_NIGHTLY_CAPS` | Optional per-number cap override, `number:cap,number:cap,...`. A pool number without an entry falls back to `SCHED_NIGHTLY_CAP` |
| `SIGNALWIRE_SIGNING_KEY` | Dashboard -> API Credentials -- **Signing Key** (click Show). A separate secret from the API Token -- signs webhooks. Without it every webhook fails signature verification |
| `SIGNALWIRE_VALIDATE_SIGNATURE` | Leave `true`. `false` only for local debugging without a real tunnel |
| `TWILIO_ACCOUNT_SID` | Twilio Console home, "Account Info". Only read when `TELEPHONY_PROVIDER=twilio` |
| `TWILIO_AUTH_TOKEN` | Twilio Console home, "Account Info" (click to reveal). Only read when `TELEPHONY_PROVIDER=twilio` |
| `TWILIO_FROM_NUMBER` | Your Twilio voice number, E.164 (`+1...`). Only read when `TELEPHONY_PROVIDER=twilio` |
| `TWILIO_VALIDATE_SIGNATURE` | Leave `true`. `false` only for local debugging without a real tunnel |
| `PUBLIC_BASE_URL` | The `https://` ngrok/cloudflared URL (local) or Railway domain (prod). No trailing slash |
| `GOOGLE_OAUTH_CLIENT_ID` | Google Cloud Console → Credentials → your Desktop OAuth client |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Same place |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Output of `python scripts/get_google_refresh_token.py` |
| `GOOGLE_SHEET_URL` | Paste the full Sheet URL (or just its ID) |
| `GOOGLE_SHEET_TAB` | Tab name to append to. Default `Calls`. Created if missing |
| `ANTHROPIC_API_KEY` | Your Anthropic key. Used for one Haiku call per detected menu; on failure the dialer logs it loudly and uses keyword-priority fallback |
| `ANTHROPIC_MODEL` | Default `claude-haiku-4-5` |
| `IVR_ENABLED` | `true`. Set `false` to skip menu navigation entirely (original core-loop behaviour) |
| `TEST_MODE` | `true` for now. Hard guard against dialing non-allowlisted numbers |
| `TEST_ALLOWLIST` | Comma-separated E.164 numbers you're allowed to dial in test mode |
| `MACHINE_DETECTION` | `Enable` (fast) or `DetectMessageEnd` (waits for full greeting) |
| `AMD_WAIT_SECONDS` | Seconds to hold an answered call open for AMD (non-menu path). `6` is fine |
| `RECORD_CALLS` | `false` by default. Turn **on** for IVR testing (spec §8) — but see recording-consent note in spec §12 |
| `PORT` | Local server port. Default `8080` |
| `IVR_MAX_GATHER_CYCLES` | Max `<Gather>` cycles before giving up "listening through". Default `5` |
| `IVR_SPEECH_MODEL` / `IVR_INITIAL_TIMEOUT_SECONDS` / `IVR_TAIL_GATHER_SECONDS` | Speech-recognition tuning; defaults are fine |
| `GATEKEEPING_DETECTION_ENABLED` | Default `true`. Kill switch for digital-gatekeeping detection (`gatekeeping_miss`) -- set `false` to disable instantly without a code rollback |
| `SCHED_QUEUE_TAB` | Per-company state tab. Default `Queue` |
| `SCHED_EVENING_WINDOW` / `SCHED_DEEP_NIGHT_WINDOW` | Local calling windows, `HH:MM-HH:MM`. Defaults `21:30-22:30` / `02:00-04:00` |
| `SCHED_CALLING_DAYS` | Default `Sun,Mon,Tue,Wed,Thu` |
| `SCHED_MIN_DAYS_BETWEEN_ATTEMPTS` | Default `10` |
| `SCHED_MAX_ATTEMPTS_PER_QUARTER` / `SCHED_FIRST_WINDOW` / `SCHED_TICK_SECONDS` | Defaults `4` / `evening` / `60` |
| `SCHED_NIGHTLY_CAP` | Default `0` (unlimited). Default per-number nightly cap for any `SIGNALWIRE_FROM_NUMBERS` pool entry without its own override in `SIGNALWIRE_NIGHTLY_CAPS`; with no pool configured, it's simply the one number's cap. Simple launch stopgap -- not the full number-pool/ramp system |
| `INBOUND_POOL_NUMBERS` | Dialer callback numbers, comma-separated E.164. **Not** your `TWILIO_FROM_NUMBER`. Empty for now |
| `INBOUND_LOG_TAB` / `INBOUND_REJECT_REASON` | Defaults `Inbound` / `rejected` (`busy` = busy signal instead of not-in-service) |

### A note on the Anthropic key

The only LLM call is one Claude Haiku classification per **detected menu**
("which digit?"). If it fails — bad key, billing problem, timeout — the server
logs a distinct

```
ANTHROPIC API ERROR (check billing / key): <detail>
```

and falls back to keyword-priority digit selection, flagging the row. It is
never swallowed as a code bug and never guessed past. So if menu rows come out
`classifier=keyword_fallback` with that error in the notes, check the Anthropic
account, not the code.

---

## Deploying to Railway

1. New project → **Deploy from GitHub repo**. Railway detects Python via
   `requirements.txt`/`runtime.txt` and reads the `Procfile`, which has two
   process lines: `web` (the Flask server) and `worker` (the scheduler loop).
   Railway auto-creates **one service per Procfile line** on first connect —
   you do not need to manually add a second service or set a custom start
   command for the scheduler; it's already `python -m app.scheduler run`.
2. Keep the Procfile free of `#` comments. Railway's Procfile parser does not
   reliably skip comment lines — a comment containing a `:` gets misread as
   its own bogus process (name = text before the colon, command = text
   after), producing a phantom service that fails to build. If you ever see
   an extra service card with a garbage name, delete it; it's not real.
3. Set all the `.env` variables in **both** services' Variables tabs (do not
   commit `.env`). Set `PUBLIC_BASE_URL` to the `web` service's Railway
   domain (Settings → Networking) once it's assigned — not a placeholder.
4. `TWILIO_VALIDATE_SIGNATURE=true`, `TEST_MODE=true` until you're ready.
5. `web`'s `--workers 1` (already in the `Procfile`) is load-bearing, not a
   knob to tune — call state (`app/call_store.py`) is in-process memory, so a
   second gunicorn worker would fork a second, inconsistent copy of it.

---

## Project layout

```
app/
  config.py          env loading + validation (single CFG object)
  dialer.py          provider dispatch: routes place_call()/hang_up() to Twilio or SignalWire
  twilio_dialer.py   place_call() / hang_up() -- Twilio REST wrapper (kept intact, TELEPHONY_PROVIDER=twilio)
  signalwire_dialer.py  place_call() / hang_up() -- SignalWire Compatibility REST API, plain HTTP (default provider)
  server.py          Flask app: /calls trigger, /ivr/* state machine, webhooks, Queue write-back
  ivr.py             keyword menu detection, digit priority, tail classification
  anthropic_client.py  Claude Haiku "which digit?" call, loud failure surfacing
  outcomes.py         call-status / AMD -> outcome label
  call_store.py       in-process call + IVR state, idempotency claim
  google_sheets.py    keyless OAuth Sheets client (Calls audit log)
  dial.py             CLI: POST /calls on the running server
  preflight.py        CLI: verify Twilio + Anthropic + Google before dialing
  --- scheduler (spec §7) ---
  queue_model.py     Queue tab schema (spec §2), QueueRow, bool/csv helpers
  queue_backend.py   SheetQueue (production) + MemoryQueue (simulation)
  queue_writer.py    the ONLY place Queue rows are mutated -- per-phone RLock
  timezones.py       IANA validation + state/area-code proposals (helper only)
  scheduler.py       tick(), eligibility, windows, quarter logic, CLI
  seed_queue.py      CLI: create Queue tab, --sample companies
  fill_timezones.py  CLI: propose `timezone` values for review
  sim_quarter.py     CLI: accelerated full-cycle sim + invariant checks
  sim_concurrency.py CLI: race the Queue write lock
  --- inbound (spec §9-10) ---
  inbound_log.py     append-only Inbound tab
  setup_inbound.py   CLI: point every pool number's Voice webhook at /inbound/voice
scripts/
  get_google_refresh_token.py   one-time OAuth consent helper
  simulate_webhook.py           signed fake Twilio traffic (webhooks + IVR TwiML follower)
```

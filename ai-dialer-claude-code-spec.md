# AI Dialer — Claude Code Build Spec

Build an orchestration service that places outbound test calls via Twilio, navigates IVR menus, detects call outcome, and logs results to Google Sheets. This is NOT a conversational AI agent — no LLM on the call itself. The only LLM usage is one cheap classification call per detected IVR prompt.

Read this whole spec before writing code. Ask before deviating from any decision marked FIXED. Items marked OPEN need a decision confirmed with the user before or during build — do not silently pick a default.

---

## 1. Stack

- **Language**: Node.js (TypeScript) or Python — pick whichever you're stronger in for Twilio webhook handling; both have mature Twilio SDKs.
- **Hosting**: Railway. Needs: persistent process (not serverless/Lambda — mid-call state and timers don't fit a stateless execution model), a stable public HTTPS URL for Twilio webhooks, built-in cron for scheduled batch triggers.
- **Telephony**: Twilio Programmable Voice + Answering Machine Detection (AMD).
- **Speech-to-text for IVR parsing**: Twilio's built-in `<Gather input="speech">` transcription, or route recorded segments to a transcription API if more accuracy is needed — start with Twilio's native option, only add a separate STT provider if it proves unreliable in testing.
- **LLM classification**: Claude Haiku via Anthropic API, single-purpose calls only ("given this IVR transcript, what digit should be pressed and why").
- **State store**: Google Sheets, via a Google service account (not OAuth user flow — this needs to run unattended).
- **Secrets**: environment variables only (Twilio Account SID/Auth Token, Anthropic API key, Google service account JSON, per-number pool config). Never commit secrets to the repo.

## 2. Core Data Model

One Google Sheet, one tab as primary state store. Columns:

```
company_name | phone_e164 | domain | city | state | timezone
source (apollo/outscraper/both)
current_quarter_attempts (int, resets quarterly)
last_call_date | last_call_window (evening/deep_night)
last_outcome (answered/voicemail/extended_hold/no_answer/busy/disconnected/null)
this_quarter_status (in_progress/confirmed_miss/confirmed_covered)
next_eligible_date
miss_timestamp (ISO 8601, exact call time of the qualifying miss)
recording_url
ivr_fallback_flagged (bool)
email_track (missed_call_sequence/core_sequence/none_yet)
replied_or_closed (bool)
do_not_call (bool)
call_sid_history (comma-separated, for idempotency — see Section 6)
last_updated_at
```

Batch writes to the Sheet, not one API call per field update — Google Sheets API has per-minute quota limits that will be hit fast at real call volume. Buffer outcome writes and flush in batches (e.g. every 30 seconds or every N calls, whichever comes first).

## 3. Call Flow State Machine

Per call attempt:

1. **Pre-dial check**: skip if `do_not_call = true`, `replied_or_closed = true`, or `next_eligible_date` is in the future. Skip if a call to this `phone_e164` is already in-flight (see Section 6, race conditions).
2. **Place call** via Twilio API with `MachineDetection` enabled and a status callback URL pointed at this service.
3. **Start a single 60-second master timer** on connect. This timer covers IVR navigation AND hold detection combined — not two separate budgets.
4. **Listen for IVR prompt.** Use Gather/transcription on the initial audio. If speech is detected that looks like a menu (see Section 4 for detection heuristics), send the transcript to the Haiku classification call, get back a digit, send that DTMF tone via the Twilio API (`<Play digits="X">` or REST update).
5. **Allow up to 2 menu levels.** After sending a digit, listen again briefly for a second prompt. If another menu is detected, repeat step 4 once more, then stop attempting further navigation regardless of what follows.
6. **Hand off to AMD** once no further menu language is detected. AMD result arrives async via Twilio's AMD webhook callback (`human` / `machine_start` / `machine_end_*` / etc.).
7. **Resolve outcome**:
   - AMD = `human` → `answered`, hang up immediately.
   - AMD = any `machine_*` → `voicemail`, hang up immediately, do NOT leave a message.
   - No AMD resolution by the 60-second cap → `extended_hold`, force hangup.
   - Call-status level events (no pickup, busy, invalid number) short-circuit this whole flow — see Section 5.
8. **Write outcome to Sheet** (buffered, see Section 2).

## 4. IVR Detection & DTMF Logic

This is the highest-risk part of the build for silent data corruption — a call that never gets past a menu should never be logged as a genuine "no answer" or "voicemail," because that's not evidence of anything about the business's real coverage.

- Heuristic for "this audio is a menu, not a human greeting or voicemail message": presence of phrases like "press", digit words adjacent to "for", "to speak with", "to reach", "emergency", "after hours", "representative", "current customer". Build a keyword list, don't rely on a single trigger phrase.
- **Digit extraction**: parse the actual instructed digit from the transcript — do not default to pressing 1. Messages vary (some are "press 1", some "press 0", some "press 2 for emergency service"). The Haiku prompt should return a structured response: `{digit: string, confidence: "high"|"low", reasoning: string}`.
- **Ambiguous fallback**: if multiple options are heard with no single clear "emergency/after-hours" instruction (e.g. "press 1 for sales, press 2 for service, press 3 for billing"), apply keyword priority: emergency/after-hours language wins if present anywhere in the menu; otherwise pick whichever option sounds closest to "speak to someone" / "current customer". **Set `ivr_fallback_flagged = true`** on this row whenever this fallback logic fires, so these calls are easy to spot-check later against the recording.
- **False-positive guard**: a live human saying something like "hi, sorry, one sec" should not get misclassified as an IVR menu and receive a DTMF tone. If the Haiku classification comes back low-confidence or doesn't clearly identify a menu structure, treat it as NOT a menu and proceed straight to AMD instead of guessing.
- DTMF tones need to be sent during the actual pause after the prompt finishes, not immediately on any speech detection — sending a tone mid-prompt on some IVR systems doesn't register. Use end-of-speech detection from the Gather, not fixed timing.

## 5. Call-Status-Level Outcomes (no AMD needed)

Twilio call status callbacks handle these directly, without going through the state machine above:

- `no-answer` → outcome = `no_answer`, miss.
- `busy` → outcome = `busy`, miss.
- `failed` / invalid number format / carrier rejection → outcome = `disconnected`, miss, AND set `do_not_call = true` on that row (no point re-dialing a dead number next cycle).

## 6. Idempotency & Race Conditions

- **Duplicate webhooks**: Twilio can and will retry webhook delivery. Every webhook handler must be idempotent — key on `CallSid`, check whether this exact `CallSid` has already been processed before writing an outcome, discard duplicates.
- **Webhook signature verification**: validate every incoming Twilio webhook against the `X-Twilio-Signature` header using your Twilio auth token. Reject anything that doesn't verify — an unverified endpoint accepting arbitrary "call outcome" POSTs is a spoofable data-integrity hole.
- **Concurrent dial protection**: before dialing, check no other call to the same `phone_e164` is currently in-flight (track in-flight `CallSid`s in memory or a lightweight lock, not just relying on the Sheet, since Sheet writes are batched/delayed). Prevents double-dialing the same company in the same batch run.
- **Batch/scheduler crash recovery**: if the orchestration app crashes mid-batch, on restart it should be able to determine which companies in that batch were already dialed (via `call_sid_history` or a run-scoped in-progress marker) rather than re-dialing everyone from the start of the batch.

## 7. Scheduling

- Cron triggers fire batches at window start: evening (9:30-10:30pm) and deep-night (2-4am), **in each company's own local timezone**, not the server's timezone. Requires a timezone field per company (derivable from state, or looked up).
- OPEN — confirm before building: does Sun-Thu-only calling (per the existing manual SOP) apply to this system too? Assumed yes; do not hardcode without confirming.
- Batches should be rate-limited/throttled on concurrent outbound calls — both for Twilio account limits and to avoid a burst pattern that looks like robocalling to carriers. Pick a conservative concurrent-call cap and make it configurable, not hardcoded to whatever Twilio's account max happens to be.

## 8. Test Mode

- `TEST_MODE=true` environment flag restricts the dial list to an explicit allowlist of numbers (hardcoded in config, not the live Sheet) — this must be a hard guard, not just a UI toggle, so a mistake can't accidentally dial real prospects.
- Recording stays ON in test mode by default so call-flow issues (wrong digit pressed, premature hangup, timer misfires) can be diagnosed by listening back, not just inferred from logs.
- Graduate: test mode against own numbers → small real batch (10-20 prospects) with recording still on → full queue.

## 9. Callback (Inbound) Handling

- Separate number pool from any real business-facing number.
- **Do not answer inbound calls to these numbers at all.** No incoming-call webhook/TwiML app wired up for answer behavior — the call should simply ring until the caller hangs up or their carrier times it out. No `<Say>`, no `<Record>`, no menu, no voicemail box, no identifying content of any kind. Since the call never connects, no audio is exchanged — this also means no call-recording/consent question applies to these inbound calls (that concern is specific to the outbound test calls, see Section 12).
- Verify this in Twilio's console: the number's Voice configuration should have no webhook pointed at an answer handler for inbound calls, or should explicitly reject rather than answer-and-respond.
- Every number in the pool must behave identically — this is not something to configure per-number.
- Test this explicitly as part of Definition of Done (Section 13): place a real live call to a dialer number and confirm it rings without ever connecting, rather than assuming Twilio's default behavior matches intent.

## 10. Number Pool & Reputation

- Do not run this off a single Twilio number at volume — build config to support a pool of numbers from the start, even if only 1-2 are provisioned initially.
- Track answer-rate per number over time (in the Sheet or a small separate log) — a declining answer rate on a specific number is a signal it may be getting spam-flagged by carriers and should be rotated out.
- Round-robin or least-recently-used selection across the pool for outbound dials.

## 11. Error Handling

- Twilio API errors on call placement (insufficient balance, invalid number format, rate limit) should be caught, logged with the company row, and NOT silently dropped — surface these as a distinct status so they don't get miscounted as a real outcome.
- If the Anthropic API call for IVR digit-parsing fails or times out, do not guess — fall back to the keyword-priority logic in Section 4 directly, and flag the row.
- If the Sheet write fails (API error, quota), retry with backoff; do not lose an outcome silently. Consider a local write-ahead log/queue so a Sheet API outage doesn't drop data.
- Add basic alerting (even just an email/Slack webhook) for: scheduler failing to fire a scheduled batch, sustained Twilio API failures, sustained Sheet write failures. Silent failure of a 2am batch should not go unnoticed until someone checks days later.

## 12. Explicitly Flagged, Not This Build's Problem to Solve Alone

- **Call recording consent**: several states require all-party consent to record calls. Recording is part of this spec for QA purposes. Get this reviewed before running at real volume — do not treat silence on this as clearance.
- **TCPA/regulatory exposure** on automated outbound calling — business lines, not consumer cells, different exposure profile, but get a real legal read before scaling past small-batch testing.

## 13. Definition of Done for v1

- Can run a test-mode batch against an allowlist of numbers and correctly log outcome + recording URL for each of the 6 taxonomy outcomes.
- IVR navigation correctly handles at least: single-level "press 1", two-level nested menu, and a menu with no clear emergency option (fallback path, correctly flagged).
- Duplicate Twilio webhook delivery does not create duplicate or corrupted Sheet rows.
- A crashed/restarted batch does not re-dial already-completed calls.
- Callback to a dialer number rings without ever connecting, verified by an actual live test call.

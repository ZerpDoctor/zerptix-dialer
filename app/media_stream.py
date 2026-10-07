"""SignalWire Media Stream -> Deepgram real-time transcription bridge.

Feature-flagged (CFG.stream_transcription_enabled): when off, none of this
runs and the existing Gather-based flow is completely untouched. The whole
point is to stop paying SignalWire's own Speech Recognition meter (~12x
Deepgram's rate for the same audio, confirmed via SignalWire's published
pricing) for the parts of a call that don't need a live decision, without
changing the turn-handling/classification logic at all -- this module only
ever produces text into a per-call buffer that the existing code in
server.py/ivr.py reads from, the same way it already reads Gather's
SpeechResult.

Threading model: each active call gets one thread running
handle_signalwire_stream() (spawned by the flask-sock route in server.py,
which itself runs on its own gunicorn gthread worker thread for the
connection's lifetime), plus one short-lived reader thread for the matching
Deepgram connection. At the current dial-pool concurrency cap (5), that's a
handful of threads against a 24-thread worker -- not a scaling concern.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field

from .config import CFG

log = logging.getLogger("dialer.media_stream")

# mulaw/8000 matches SignalWire Media Streams' default codec (PCMU @ 8000Hz,
# mono) -- see the Stream verb's own docs.
# interim_results=true (2026-09-28, was false): only the *behavior* still
# relies on stabilized final segments -- see _deepgram_reader, which still
# only appends is_final=true text to the buffer nothing downstream changes.
# Turned on purely for diagnostic logging: real incident (Bay Coast
# Restoration, 2026-09-28) showed a "human" AMD verdict, 9s of real audio
# genuinely forwarded to Deepgram (confirmed via frame count), and zero
# final segments -- with interim_results off there was no way to tell
# whether Deepgram ever recognized any words that just never finalized
# before the call ended, versus genuine silence the whole time. Same
# audio duration billed either way -- Deepgram's real-time pricing is per
# minute of audio streamed, not per result message returned.
def _ulaw_to_linear(u: int) -> int:
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return (0x84 - t) if (u & 0x80) else (t - 0x84)


_ULAW = [_ulaw_to_linear(i) for i in range(256)]


_DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3&language=en-US&encoding=mulaw&sample_rate=8000"
    "&channels=1&punctuate=true&interim_results=true"
)


def ringback_pattern(energy: list) -> bool:
    """True if the per-second levels look like a phone RINGING: tone bursts of 1-4s
    (loud) separated by near-digital silence, repeating at a steady 3-8s period
    (US ringback is ~2s on / 4s off). 2026-10-01: Longview, Doan and Beacon were
    AMD "human" with zero text and levels like `-18 -12 -13 -78 -78 -78` repeating;
    we hung up at 18s while the far end was still ringing. Meant for calls where NO
    text was heard -- speech does not have this clean on/off cadence, but callers
    must still gate on silence of the transcript."""
    loud = [e > -45 for e in energy]
    quiet = [e < -60 for e in energy]
    runs, i = [], 0                       # (start, length) of each loud run
    while i < len(loud):
        if loud[i]:
            j = i
            while j < len(loud) and loud[j]:
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1
    if len(runs) < 2 or any(not 1 <= n <= 4 for _, n in runs):
        return False
    starts = [s for s, _ in runs]
    periods = [b - a for a, b in zip(starts, starts[1:])]
    if any(not 3 <= p <= 8 for p in periods) or max(periods) - min(periods) > 1:
        return False
    # every gap between bursts must be (almost) digital silence, not low-level speech
    gaps = [energy[s + n:nxt] for (s, n), nxt in zip(runs, starts[1:])]
    return all(g and sum(1 for e in g if e < -60) >= max(1, len(g) - 1) for g in gaps)


def ringing_after_greeting(energy: list) -> bool:
    """True if SOMEWHERE in the call's levels (not only from the start) there is a stretch of >=10s that looks like a
    phone ringing -- a greeting, then the line ringing on: a transfer that has not been picked up. Aeret and Royal
    Restoration, 2026-10-06: "Thank you for calling." and then 3-on / 3-off ringing until we hung up."""
    n = len(energy)
    for i in range(0, max(n - 9, 0)):
        for j in range(i + 10, n + 1):
            w = energy[i:j]
            if not ringback_pattern(w):
                continue
            # A ring tone is the same loudness every time and the gaps between rings are digital silence. Stricter
            # than ringback_pattern alone because this scans EVERY stretch of an answered call (a pair of speech
            # bursts with a pause between them passes the looser test).
            runs, k = [], 0
            while k < len(w):
                if w[k] > -45:
                    s = k
                    while k < len(w) and w[k] > -45:
                        k += 1
                    runs.append((s, k))
                else:
                    k += 1
            if len(runs) < 2:
                continue
            means = [sum(w[s:e]) / (e - s) for s, e in runs]
            gaps = [w[e1:s2] for (_, e1), (s2, _) in zip(runs, runs[1:])]
            if max(means) - min(means) <= 6 and all(g and max(g) < -65 for g in gaps):
                return True
    return False


@dataclass
class StreamBuffer:
    """Accumulates Deepgram's finalized transcript segments for one call.
    Ordering is by LIST POSITION, not wall-clock time -- an earlier version
    of this used time.monotonic() timestamps to answer "what's new since I
    last checked", but a read immediately followed by an append (exactly
    what a real turn does) can land on the same clock tick, making
    `ts > since` false and silently dropping the segment. An index-based
    "consume what's past my last read position" has no such edge case."""
    lock: threading.Lock = field(default_factory=threading.Lock)
    segments: list[str] = field(default_factory=list)
    read_count: int = 0
    error: str | None = None  # set if the Deepgram connection itself failed
    connected_at: float | None = None  # set the instant SignalWire's own WS
    # reaches handle_signalwire_stream() -- BEFORE the Deepgram connect
    # attempt, so it answers "did SignalWire even connect to us at all",
    # distinct from "did Deepgram then work". Real incident 2026-09-28: 15/90
    # calls in one batch resolved "unknown, nothing transcribed" with
    # transcript_accum literally empty at turn 1; with only a log line as
    # evidence, there was no way to tell "stream never connected" from
    # "connected but Deepgram was still slow" after the fact. See its use in
    # ivr_turn (server.py) to give the still-connecting case more patience
    # than the connected-but-quiet case.
    interim: str = ""  # latest not-yet-final Deepgram text for the utterance
    # currently in progress, if any -- cleared once that utterance finalizes
    # (see append()). Real incident 2026-09-28 (Paul Davis Restoration): a
    # real greeting ("Thank you for calling the Paul Davis Rest...") was
    # already being recognized here when the empty-turn/AMD='human' fast
    # path hung up on a finalized-but-still-empty transcript, discarding
    # words Deepgram had already heard but hadn't finalized yet. See
    # has_interim()'s call site in ivr_turn.

    # --- "is the far end still talking?" (listen-like-a-human gate) ---------
    # Deepgram emits interim/final results continuously while someone speaks,
    # so recent non-empty transcript activity IS the mid-speech signal. This
    # replaces guessing "the prompt finished" from whether accumulated text
    # grew between two ~4s polls (the design that let the dialer press a digit
    # or resolve mid-prompt -- Greenville, Yeti, Rocky Mountain, Restoration
    # Doctor, On Site, Boston Harbor, DriForce). Uses only results the stream
    # already delivers; no Deepgram connection parameters changed.
    last_activity: float | None = None   # wall time of the last non-empty interim/final result
    first_text_at: float | None = None   # wall time of the first non-empty result
    frames: int = 0                      # audio frames forwarded to Deepgram
    # Loudness of the audio we forward, one dBFS integer per second (first 90s). Words are
    # all a transcript can show; this shows whether there was SOUND. 2026-10-01: 10 of 100
    # calls were AMD "human" with ~790 audio frames and zero text -- without this there is
    # no way to tell a silent line from speech Deepgram failed to recognise.
    energy: list = field(default_factory=list)
    _e_sum: float = 0.0
    _e_n: int = 0
    results: int = 0                     # non-empty transcript results received
    # Set once the stream handler has asked Deepgram to flush and collected what came back. A call the far
    # end hangs up is resolved from the status callback, which can land before the last seconds of speech
    # were transcribed -- _resolve waits on this (briefly) before it reads the buffer.
    flushed: threading.Event = field(default_factory=threading.Event)

    def note_activity(self) -> None:
        now = time.time()
        self.last_activity = now
        self.results += 1
        if self.first_text_at is None:
            self.first_text_at = now

    def quiet_for(self) -> float | None:
        """Seconds since the last transcript activity; None if there has been none."""
        la = self.last_activity
        return None if la is None else max(0.0, time.time() - la)

    def is_speaking(self, quiet_seconds: float) -> bool:
        """True while the far end is (very probably) mid-speech: transcript
        activity within `quiet_seconds`, or an interim still awaiting
        finalization that was updated within a slightly longer window (a
        pause between words can make an interim briefly stall)."""
        age = self.quiet_for()
        if age is None:
            return False
        if age < quiet_seconds:
            return True
        with self.lock:
            has_interim = bool(self.interim.strip())
        return has_interim and age < max(quiet_seconds * 2.5, 2.5)

    def add_audio(self, payload: bytes) -> None:
        for b in payload:
            v = _ULAW[b]
            self._e_sum += v * v
        self._e_n += len(payload)
        while self._e_n >= 8000 and len(self.energy) < 90:       # 8000 mu-law bytes = 1s at 8kHz
            rms = math.sqrt(self._e_sum / self._e_n)
            self.energy.append(int(20 * math.log10(max(rms, 1.0) / 32768.0)))
            self._e_sum = 0.0
            self._e_n = 0

    def energy_note(self) -> str:
        return "audio dBFS/s: " + " ".join(str(x) for x in self.energy[:30]) if self.energy else ""

    def stats_note(self, answered_at: float | None) -> str:
        """Compact stream evidence for the Calls notes -- so a blank transcript
        can be told apart from silence: no connection, connected but no audio,
        audio forwarded but no words recognized."""
        if self.error:
            return f"stream: error ({self.error[:60]})"
        if self.connected_at is None:
            return "stream: never connected"
        first = ("none" if self.first_text_at is None
                 else f"+{self.first_text_at - (answered_at or self.connected_at):.0f}s")
        base = f"stream: connected, {self.frames} audio frames, {self.results} text results, first text {first}"
        e = self.energy_note()
        return f"{base}; {e}" if e else base

    def append(self, text: str) -> None:
        if not text.strip():
            return
        with self.lock:
            self.segments.append(text.strip())
            self.interim = ""

    def set_interim(self, text: str) -> None:
        with self.lock:
            self.interim = text

    def has_interim(self) -> bool:
        with self.lock:
            return bool(self.interim.strip())

    def text_since_last_read(self) -> str:
        """The per-turn read: 'what's new since the turn handler last
        checked', mirroring how each Gather cycle's own SpeechResult
        naturally only contains that turn's speech. Advances the read
        position as a side effect, same as consuming a queue -- call once
        per turn, not speculatively."""
        with self.lock:
            new = self.segments[self.read_count:]
            self.read_count = len(self.segments)
            return " ".join(new)

    def full_text(self) -> str:
        with self.lock:
            return " ".join(self.segments)


_buffers: dict[str, StreamBuffer] = {}
_buffers_lock = threading.Lock()


def get_buffer(call_sid: str) -> StreamBuffer:
    """Get-or-create this call's buffer. Safe to call before the stream
    connection itself has been established (e.g. the turn handler checking
    in early) -- it'll just be empty until audio starts arriving."""
    with _buffers_lock:
        buf = _buffers.get(call_sid)
        if buf is None:
            buf = StreamBuffer()
            _buffers[call_sid] = buf
        return buf


def peek_buffer(call_sid: str) -> StreamBuffer | None:
    """Like get_buffer but never creates one -- for read-only diagnostics."""
    with _buffers_lock:
        return _buffers.get(call_sid)


def drop_buffer(call_sid: str) -> None:
    """Release a finished call's buffer. Best-effort -- a missed cleanup
    just leaks a small dict entry until process restart, never breaks
    anything downstream."""
    with _buffers_lock:
        _buffers.pop(call_sid, None)


def _deepgram_reader(dg_ws, buf: StreamBuffer, call_sid: str, stop_event: threading.Event) -> None:
    """Runs in its own thread for the lifetime of the call: reads Deepgram's
    transcript events off its WebSocket and appends finalized text to the
    shared buffer. Deepgram's own message shape:
    {"channel": {"alternatives": [{"transcript": "..."}]}, "is_final": bool}
    -- unrelated event types (metadata, UtteranceEnd, etc.) are silently
    skipped rather than treated as errors, since Deepgram's stream can send
    several message shapes we don't need."""
    while not stop_event.is_set():
        try:
            msg = dg_ws.receive(timeout=1.0)
        except Exception as e:  # noqa: BLE001
            log.warning("Deepgram reader for call_sid=%s ending: %s", call_sid, e)
            return
        if msg is None:
            continue
        try:
            data = json.loads(msg)
        except (TypeError, ValueError):
            continue
        alternatives = data.get("channel", {}).get("alternatives")
        if not alternatives:
            continue
        text = alternatives[0].get("transcript", "")
        if not text:
            continue
        buf.note_activity()
        if data.get("is_final"):
            buf.append(text)
            log.info("Deepgram final segment call_sid=%s: %r", call_sid, text)
        else:
            # Never appended to buf/text_since_last_read() -- classification
            # and menu decisions still only ever act on finalized text.
            # buf.set_interim() only feeds has_interim(), which ivr_turn uses
            # to avoid hanging up mid-recognition (see StreamBuffer's
            # docstring for the real incident this closes).
            buf.set_interim(text)
            log.info("Deepgram interim (not final) call_sid=%s: %r", call_sid, text)


def handle_signalwire_stream(ws, call_sid: str) -> None:
    """Runs for the lifetime of one call's Media Stream connection. `ws` is
    the SignalWire-side WebSocket (server, via flask-sock's per-connection
    thread). Opens a matching Deepgram connection, relays audio frames to
    it, and lets _deepgram_reader fill this call's buffer in a second
    thread. Never raises -- any failure here must not take down the call's
    own TwiML turn flow, which is driven by a completely separate HTTP
    request cycle."""
    import simple_websocket

    buf = get_buffer(call_sid)
    buf.connected_at = time.time()

    if not CFG.deepgram_api_key:
        buf.error = "DEEPGRAM_API_KEY not set"
        log.error("Media stream for call_sid=%s: %s", call_sid, buf.error)
        return

    try:
        dg_ws = simple_websocket.Client(
            _DEEPGRAM_WS_URL,
            headers={"Authorization": f"Token {CFG.deepgram_api_key}"},
        )
    except Exception as e:  # noqa: BLE001
        buf.error = f"Deepgram connect failed: {e}"
        log.error("Media stream for call_sid=%s: %s", call_sid, buf.error)
        return

    stop_event = threading.Event()
    reader_thread = threading.Thread(
        target=_deepgram_reader, args=(dg_ws, buf, call_sid, stop_event), daemon=True,
    )
    reader_thread.start()

    frames_forwarded = 0
    try:
        while True:
            raw = ws.receive(timeout=65)
            if raw is None:
                break  # SignalWire closed the stream
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            etype = event.get("event")
            if etype == "media":
                payload_b64 = event.get("media", {}).get("payload", "")
                if not payload_b64:
                    continue
                try:
                    raw_audio = base64.b64decode(payload_b64)
                    dg_ws.send(raw_audio)
                    frames_forwarded += 1
                    buf.frames = frames_forwarded
                    buf.add_audio(raw_audio)
                except Exception as e:  # noqa: BLE001
                    log.warning("Deepgram send failed for call_sid=%s: %s", call_sid, e)
                    break
            elif etype == "stop":
                break
            # "connected" and "start" events carry no audio -- nothing to do.
    except Exception as e:  # noqa: BLE001
        log.warning("SignalWire stream handler for call_sid=%s ending: %s", call_sid, e)
    finally:
        # Closing Deepgram's socket outright throws away whatever it had heard but not yet finalized -- the last
        # second or two of the call (2026-10-06: Pro Services' menu began in the final 5s and never reached the
        # transcript; Greenville's text ends "Otherwise, plea"). CloseStream makes it flush its finals first.
        try:
            dg_ws.send(json.dumps({"type": "CloseStream"}))
            reader_thread.join(timeout=2.5)
        except Exception:  # noqa: BLE001
            pass
        stop_event.set()
        try:
            dg_ws.close()
        except Exception:  # noqa: BLE001
            pass
        buf.flushed.set()
        log.info("Media stream for call_sid=%s ended, %d audio frames forwarded",
                  call_sid, frames_forwarded)

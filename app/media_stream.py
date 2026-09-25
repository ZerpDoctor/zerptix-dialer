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
import threading
from dataclasses import dataclass, field

from .config import CFG

log = logging.getLogger("dialer.media_stream")

# mulaw/8000 matches SignalWire Media Streams' default codec (PCMU @ 8000Hz,
# mono) -- see the Stream verb's own docs. interim_results=false: we only
# want stabilized final segments, the same shape Gather's SpeechResult
# already delivers per turn, so nothing downstream needs to handle partial/
# updating text.
_DEEPGRAM_WS_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-3&language=en-US&encoding=mulaw&sample_rate=8000"
    "&channels=1&punctuate=true&interim_results=false"
)


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

    def append(self, text: str) -> None:
        if not text.strip():
            return
        with self.lock:
            self.segments.append(text.strip())

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
        if text and data.get("is_final"):
            buf.append(text)
            log.info("Deepgram final segment call_sid=%s: %r", call_sid, text)


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
                    dg_ws.send(base64.b64decode(payload_b64))
                    frames_forwarded += 1
                except Exception as e:  # noqa: BLE001
                    log.warning("Deepgram send failed for call_sid=%s: %s", call_sid, e)
                    break
            elif etype == "stop":
                break
            # "connected" and "start" events carry no audio -- nothing to do.
    except Exception as e:  # noqa: BLE001
        log.warning("SignalWire stream handler for call_sid=%s ending: %s", call_sid, e)
    finally:
        stop_event.set()
        try:
            dg_ws.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("Media stream for call_sid=%s ended, %d audio frames forwarded",
                  call_sid, frames_forwarded)

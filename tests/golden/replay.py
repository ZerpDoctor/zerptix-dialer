"""Replay golden calls through the real outcome logic, offline and repeatable.

The model (Haiku) is not deterministic, so its answers are FROZEN in
tests/golden/haiku_cache.json: the first time a (function, input) pair is seen
it is asked for real (`refresh` mode) and stored; every later run replays the
stored answer. A case whose model input is not in the cache raises CacheMiss --
never a silent fallback to keyword guessing, which would make the replay lie.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

try:  # the local venv may not ship flask_sock's websocket stack; not needed here
    import flask_sock  # noqa: F401
except Exception:  # pragma: no cover
    _m = types.ModuleType("flask_sock")

    class _S:
        def __init__(self, *a, **k): pass
        def route(self, *a, **k): return lambda f: f
    _m.Sock = _S
    sys.modules["flask_sock"] = _m

from app import ivr, server  # noqa: E402

GOLDEN = Path(__file__).parent
CASES = GOLDEN / "cases.json"
CACHE = GOLDEN / "haiku_cache.json"
BASELINE = GOLDEN / "baseline.json"
MODEL_FUNCS = ("classify_ivr_digit", "classify_call_audio", "classify_alt_contact", "classify_gatekeeping")


class CacheMiss(Exception):
    pass


def load_cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))


class FrozenModel:
    """Context manager: route every model call in app.ivr through the cache."""

    def __init__(self, mode: str = "offline"):
        assert mode in ("offline", "refresh")
        self.mode = mode
        self.cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
        self.dirty = False
        self._orig: dict = {}

    def __enter__(self):
        for name in MODEL_FUNCS:
            real = getattr(ivr, name)
            self._orig[name] = real
            setattr(ivr, name, self._wrap(name, real))
        return self

    def __exit__(self, *exc):
        for name, real in self._orig.items():
            setattr(ivr, name, real)
        if self.dirty:
            CACHE.write_text(json.dumps(self.cache, indent=0, sort_keys=True), encoding="utf-8")

    def _wrap(self, name, real):
        def call(*args, **kwargs):
            key = name + "|" + json.dumps([args, kwargs], sort_keys=True, default=str)
            if key in self.cache:
                hit = self.cache[key]
                if isinstance(hit, dict) and "__unavailable__" in hit:
                    raise ivr.AnthropicUnavailable(hit["__unavailable__"])
                return hit
            if self.mode == "offline":
                raise CacheMiss(name)
            try:
                out = real(*args, **kwargs)
            except ivr.AnthropicUnavailable as e:
                self.cache[key] = {"__unavailable__": str(e)}
                self.dirty = True
                raise
            self.cache[key] = out
            self.dirty = True
            return out
        return call


def build_rec(case: dict):
    return types.SimpleNamespace(
        call_sid=case["id"], call_status="", answered_by=case["answered_by"], company_name=case["company"],
        gatekeeping_detected=case["gatekeeping_detected"], alt_contact_detected=case["alt_contact_detected"],
        hit_time_cap=case["hit_time_cap"], ivr_detected=case["ivr_detected"], digits_sent=list(case["digits_sent"]),
        transcript_accum=case["transcript"], transcript_at_last_digit=case["press_idx"] or 0)


def run_case(case: dict) -> str:
    """The outcome the current code assigns to this call."""
    saved = (server.STORE.seconds_since_answered, server.get_answered_by, server.amd_checkpoint.lookup)
    server.STORE.seconds_since_answered = lambda sid: float(case["duration_sec"])
    server.get_answered_by = lambda sid: ""          # never reach out to SignalWire from a replay
    server.amd_checkpoint.lookup = lambda sid: ""    # ...or to the Sheet
    try:
        return server._compute_outcome(build_rec(case))[0]
    finally:
        server.STORE.seconds_since_answered, server.get_answered_by, server.amd_checkpoint.lookup = saved


def replay(mode: str = "offline") -> dict:
    """{'results': {id: (expected, got)}, 'misses': [id], 'errors': {id: repr}}"""
    results, misses, errors = {}, [], {}
    with FrozenModel(mode):
        for c in load_cases():
            if not c["replayable"]:
                continue
            try:
                results[c["id"]] = (c["expected"], run_case(c))
            except CacheMiss:
                misses.append(c["id"])
            except Exception as e:  # noqa: BLE001
                errors[c["id"]] = repr(e)
    return {"results": results, "misses": misses, "errors": errors}


def mismatches(res: dict) -> set[str]:
    return {i for i, (exp, got) in res["results"].items() if exp != got}

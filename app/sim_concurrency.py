"""Prove the per-phone Queue write lock (app/queue_writer.py) stops races.

Spins many threads that hammer record_attempt() + apply_outcome() on the SAME
row and checks: exactly one attempt per (phone, local_date) is recorded, every
CallSid survives in call_sid_history (merge, not clobber), and no thread hangs.

    python -m app.sim_concurrency
"""
from __future__ import annotations

import sys
import threading
from datetime import date, datetime, timezone

from .queue_backend import MemoryQueue
from .queue_model import HEADER
from . import queue_writer

PHONE = "+15550300001"


def _row(**over) -> dict:
    d = {h: "" for h in HEADER}
    d.update({"company_name": "Race Co", "phone_e164": PHONE,
              "timezone": "America/New_York", "current_quarter_attempts": "0",
              "this_quarter_status": "in_progress"})
    d.update(over)
    return d


def _thread_safe_memqueue(mem: MemoryQueue) -> MemoryQueue:
    """MemoryQueue.update_fields / find_by_phone aren't atomic on their own;
    wrap them so the ONLY serialization under test is queue_writer's lock."""
    inner_lock = threading.Lock()
    orig_update = mem.update_fields
    orig_find = mem.find_by_phone

    def update(row_number, fields):
        with inner_lock:
            orig_update(row_number, fields)

    def find(phone):
        with inner_lock:
            return orig_find(phone)

    mem.update_fields = update  # type: ignore[method-assign]
    mem.find_by_phone = find    # type: ignore[method-assign]
    return mem


def test_attempt_race(n_threads: int = 25) -> list[str]:
    fails: list[str] = []
    mem = _thread_safe_memqueue(MemoryQueue([_row()]))
    today = date(2026, 3, 2)
    now = datetime(2026, 3, 2, 2, 15, tzinfo=timezone.utc)

    recorded: list[dict] = []
    rec_lock = threading.Lock()

    def worker(i: int) -> None:
        f = queue_writer.record_attempt(
            mem, PHONE, call_sid=f"CArace{i:04d}", window="deep_night",
            local_date=today, now_utc=now,
        )
        if f is not None:
            with rec_lock:
                recorded.append(f)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    if any(t.is_alive() for t in threads):
        fails.append("a record_attempt thread hung (possible deadlock)")

    row = mem.find_by_phone(PHONE)
    if len(recorded) != 1:
        fails.append(f"{len(recorded)} threads recorded an attempt; expected exactly 1")
    if row.attempts != 1:
        fails.append(f"row shows {row.attempts} attempts; expected 1")
    if row.call_sid_history.count("CArace") != 1:
        fails.append(f"call_sid_history has {row.call_sid_history.count('CArace')} race sids; expected 1")

    print(f"  attempt race ({n_threads} threads): recorded={len(recorded)} "
          f"attempts={row.attempts} sids={row.call_sid_history or '-'}")
    return fails


def test_outcome_merge(n_threads: int = 25) -> list[str]:
    fails: list[str] = []
    mem = _thread_safe_memqueue(MemoryQueue([_row(current_quarter_attempts="1")]))
    now = datetime(2026, 3, 2, 3, 0, tzinfo=timezone.utc)

    def worker(i: int) -> None:
        queue_writer.apply_outcome(mem, PHONE, "unknown", call_sid=f"CAout{i:04d}", when_utc=now)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    if any(t.is_alive() for t in threads):
        fails.append("an apply_outcome thread hung (possible deadlock)")

    row = mem.find_by_phone(PHONE)
    got = row.call_sid_history.count("CAout")
    if got != n_threads:
        fails.append(f"call_sid_history kept {got}/{n_threads} sids -- writes clobbered each other")
    print(f"  outcome merge ({n_threads} threads): call_sid_history kept {got}/{n_threads} sids")
    return fails


def main() -> None:
    print("queue_writer lock -- concurrency checks\n")
    fails = test_attempt_race() + test_outcome_merge()
    print()
    if fails:
        for f in fails:
            print(f"  FAIL  {f}")
        sys.exit(1)
    print("  PASS: exactly-once attempt, no lost CallSids, no hangs")


if __name__ == "__main__":
    main()

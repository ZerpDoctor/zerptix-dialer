"""Fill/refresh the frozen-model cache and (optionally) reset the regression baseline.

    python scripts/refresh_golden.py                 # ask the real model for any input not yet cached
    python scripts/refresh_golden.py --rebaseline    # ...and record today's mismatches as the known-gap baseline

Needs a working ANTHROPIC_API_KEY (a few cents). Run it after changing a model prompt
or adding cases; `python -m unittest discover tests` never calls the model.
"""
from __future__ import annotations

import collections
import json
import sys

sys.path.insert(0, ".")
from tests.golden import replay  # noqa: E402


def main(rebaseline: bool) -> None:
    res = replay.replay("refresh")
    mism = replay.mismatches(res)
    n = len(res["results"])
    print(f"replayed {n} cases | match the verified outcome: {n - len(mism)} ({(n - len(mism)) / max(n, 1):.0%}) | "
          f"mismatch: {len(mism)} | errors: {len(res['errors'])}")
    cases = {c["id"]: c for c in replay.load_cases()}
    kinds = collections.Counter((res["results"][i][0], res["results"][i][1]) for i in mism)
    for (exp, got), k in kinds.most_common():
        print(f"   expected {exp:14} got {got:14} x{k}")
    for i in sorted(mism):
        c = cases[i]
        print(f"   - {i} {c['company'][:30]:30} expected={res['results'][i][0]:14} got={res['results'][i][1]:14} | {c['transcript'][:70]!r}")
    for i, e in res["errors"].items():
        print("   ERROR", i, e)
    if rebaseline:
        replay.BASELINE.write_text(json.dumps(sorted(mism), indent=0), encoding="utf-8")
        print(f"\nbaseline written: {len(mism)} known gaps -> {replay.BASELINE}")


if __name__ == "__main__":
    main("--rebaseline" in sys.argv)

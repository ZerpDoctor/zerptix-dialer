"""Golden replay: real, person-checked calls run through the real outcome logic.

This is a RATCHET, not a demand for perfection: tests/golden/baseline.json lists
the calls the current logic still gets wrong (known gaps). The suite fails if
  - any call NOT in the baseline is now wrong      -> a regression, or
  - any baseline call is now right                 -> progress: remove it from baseline.json.
Model answers are frozen in tests/golden/haiku_cache.json, so this runs offline,
free and identically every time. After changing a model prompt or adding cases:
    python scripts/build_golden.py && python scripts/refresh_golden.py --rebaseline
"""
from __future__ import annotations

import json
import unittest

from tests.golden import replay


class GoldenReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not replay.CASES.exists():
            raise unittest.SkipTest("golden fixtures not present (they hold real call data and are local only); "
                                    "build them with: python scripts/build_golden.py && python scripts/refresh_golden.py --rebaseline")
        cls.res = replay.replay("offline")
        cls.baseline = set(json.loads(replay.BASELINE.read_text(encoding="utf-8")))
        cls.cases = {c["id"]: c for c in replay.load_cases()}
        cls.bad = replay.mismatches(cls.res)

    def label(self, i):
        c = self.cases[i]
        exp, got = self.res["results"][i]
        return f"{i} {c['company'][:28]!r}: expected {exp}, got {got} | {c['transcript'][:60]!r}"

    def test_the_frozen_model_cache_is_complete(self):
        self.assertEqual(self.res["misses"], [],
                         "model input not in the cache; run: python scripts/refresh_golden.py")

    def test_replay_raises_no_errors(self):
        self.assertEqual(self.res["errors"], {})

    def test_no_regressions(self):
        new = sorted(self.bad - self.baseline)
        self.assertEqual(new, [], "calls that used to be right are now wrong:\n  "
                         + "\n  ".join(self.label(i) for i in new))

    def test_baseline_has_no_stale_entries(self):
        fixed = sorted(self.baseline - self.bad)
        self.assertEqual(fixed, [], "these known gaps are now fixed -- remove them from tests/golden/baseline.json:\n  "
                         + "\n  ".join(f"{i} {self.cases[i]['company']}" for i in fixed))

    def test_overall_agreement_does_not_fall(self):
        n = len(self.res["results"])
        self.assertGreaterEqual((n - len(self.bad)) / n, 0.97, "agreement with the verified outcomes fell below 97%")

    def test_the_replay_is_deterministic(self):
        again = replay.mismatches(replay.replay("offline"))
        self.assertEqual(again, self.bad)


if __name__ == "__main__":
    unittest.main()

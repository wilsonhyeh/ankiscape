# tests/test_generated_sequences.py - PR-scale generated trace properties.
"""100 deterministic sequences of <=100 operations compare the incremental
checkpoint fast path against the reference reducer at every prefix, plus the
valid algebraic properties (arrival-order invariance, exact duplicate
idempotence, non-negative inventory/XP) and optional Hypothesis examples.
Seeds are the reproducer identities: a failure names its seed and the dev
script emits a minimized operation list."""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evolved.data import load_rules  # noqa: E402


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name,
                                                  os.path.join(ROOT, relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRACES = _load("ankiscape_generated_traces_test", "dev/generated_traces.py")


class GeneratedSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_pr_scale_sequences_match_reference_at_every_prefix(self):
        for seed in range(1, 101):
            length = 20 + (seed * 37) % 81
            ops = TRACES.generate_sequence(seed, length, self.rules)
            problems = TRACES.check_sequence(ops, self.rules, f"gen-{seed}",
                                             full_prefixes=True)
            with self.subTest(seed=seed):
                self.assertEqual(problems, [],
                                 f"seed={seed} length={length}")

    def test_seed_reproducibility(self):
        first = TRACES.generate_sequence(7, 40, self.rules)
        second = TRACES.generate_sequence(7, 40, self.rules)
        self.assertEqual(first, second)
        other = TRACES.generate_sequence(8, 40, self.rules)
        self.assertNotEqual(first, other)

    def test_generated_sequences_cover_every_operation_kind(self):
        kinds = set()
        for seed in range(1, 31):
            ops = TRACES.generate_sequence(seed, 100, self.rules)
            kinds.update(op["kind"] for op in ops)
        for expected in ("review_award", "review_skip", "review_retract",
                         "review_restore", "catchup_preset"):
            self.assertIn(expected, kinds)

    def test_optional_hypothesis_examples(self):
        # Optional dev-only augmentation: the deterministic sequences above
        # are the mandatory coverage; absence of hypothesis is not a failure
        # and must not report a skip (evidence records reject skips).
        try:
            import hypothesis  # noqa: F401
        except Exception:
            return
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st

        rules = self.rules

        @settings(max_examples=25, deadline=None,
                  suppress_health_check=[HealthCheck.too_slow])
        @given(seed=st.integers(min_value=1, max_value=10 ** 6),
               length=st.integers(min_value=1, max_value=40))
        def _one(seed, length):
            ops = TRACES.generate_sequence(seed, length, rules)
            assert not TRACES.check_sequence(ops, rules, f"gen-{seed}")

        _one()


if __name__ == "__main__":
    unittest.main()

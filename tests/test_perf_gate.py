# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from tests.perf.benchmark_retrieval import gate, percentile


class TestPerformanceGate(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(percentile([1.0, 2.0, 3.0], 0.5), 2.0)
        self.assertAlmostEqual(percentile([1.0, 2.0], 0.95), 1.95)

    def test_absolute_and_regression_gate(self):
        result = {name: {"p95_ms": value} for name, value in (
            ("search", 12.1), ("get", 3.0), ("recent", 8.0))}
        thresholds = {"search_p95_ms": 10.0, "get_p95_ms": 10.0,
                      "recent_p95_ms": 10.0}
        baseline = {name: {"p95_ms": value} for name, value in (
            ("search", 10.0), ("get", 3.0), ("recent", 8.0))}
        failures = gate(result, thresholds, baseline=baseline, max_regression=0.20)
        self.assertEqual(len(failures), 2)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest

from tests.eval.metrics import check_thresholds, evaluate


class TestEvalMetrics(unittest.TestCase):
    def test_rank_metrics_and_abstain(self):
        records = [
            {"case": {"expected": ["cognition:2"], "should_abstain": False},
             "results": [
                 {"kind": "cognition", "id": 1, "text": "无关"},
                 {"kind": "cognition", "id": 2, "text": "正确"},
             ]},
            {"case": {"expected": [], "should_abstain": True}, "results": []},
        ]
        metrics = evaluate(records)
        self.assertEqual(metrics["recall_at_5"], 1.0)
        self.assertEqual(metrics["precision_at_5"], 0.2)
        self.assertEqual(metrics["mrr_at_5"], 0.5)
        self.assertEqual(metrics["no_answer_false_positive_rate"], 0.0)

    def test_forbidden_and_duplicate_are_counted(self):
        repeated = {"kind": "cognition", "id": 1, "text": "重复事实"}
        records = [{
            "case": {"expected": [], "forbidden": ["cognition:1"],
                     "should_abstain": True},
            "results": [repeated, {**repeated, "id": 2}],
        }]
        metrics = evaluate(records)
        self.assertEqual(metrics["forbidden_leakage_rate"], 0.5)
        self.assertEqual(metrics["duplicate_rate"], 0.5)

    def test_threshold_failures(self):
        failures = check_thresholds(
            {"recall_at_5": 0.8, "duplicate_rate": 0.1},
            {"recall_at_5": {"min": 0.9}, "duplicate_rate": {"max": 0.05}})
        self.assertEqual(len(failures), 2)


if __name__ == "__main__":
    unittest.main()

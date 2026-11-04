# -*- coding: utf-8 -*-
"""可信度降权与检索可观测字段。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import retrieval
import tools
from database import Cognition, Database, Experience

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_observability"
S1 = "s_obs"


class ObsCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")
        self._orig = tools._db
        tools._db = lambda: self.db

    def tearDown(self):
        tools._db = self._orig
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _cognition(self, content, confidence=0.9):
        item = self.db.insert_cognition(Cognition(
            subject="用户", content=content, confidence=confidence,
            source_event_id=1, created_at="2026-11-04T10:00:00+00:00"))
        self.db.index_fts("cognition", item, "", content)
        return item


class TestReliability(unittest.TestCase):
    def test_event_needs_review_is_halved(self):
        row = type("R", (), {"needs_review": 1})()
        self.assertEqual(retrieval._reliability(row, "event"), 0.5)
        self.assertEqual(retrieval._reliability(
            type("R", (), {"needs_review": 0})(), "event"), 1.0)

    def test_low_confidence_is_halved(self):
        self.assertEqual(retrieval._reliability(
            type("R", (), {"confidence": 0.3})(), "cognition"), 0.5)
        self.assertEqual(retrieval._reliability(
            type("R", (), {"confidence": 0.9})(), "cognition"), 1.0)


class TestRanking(ObsCase):
    def test_low_confidence_ranks_below_high(self):
        high = self._cognition("部署脚本放 re 子目录", confidence=0.95)
        low = self._cognition("部署脚本放 bin 目录", confidence=0.2)
        hits = retrieval.search(self.db, "部署脚本", touch=False)
        ids = [hit.id for hit in hits]
        self.assertEqual(ids[:2], [high, low], "低可信条目应排在后面")


class TestObservability(ObsCase):
    def test_search_reports_truncation_and_scope(self):
        for index in range(6):
            self._cognition(f"检索观测条目 {index}")
        out = tools.memory_search("检索观测", limit=2)
        self.assertEqual(out["count"], 2)
        self.assertTrue(out["has_more"], "被 limit 截断时要显式告知")
        self.assertEqual(out["scope_applied"], "all")
        self.assertEqual(out["filters"]["kind"], "all")
        self.assertIsNone(out["total"], "给不出全局总数时如实置空")

    def test_not_truncated_reports_has_more_false(self):
        self._cognition("只有一条的查询词")
        out = tools.memory_search("只有一条的查询词", limit=5)
        self.assertEqual(out["count"], 1)
        self.assertFalse(out["has_more"])


if __name__ == "__main__":
    unittest.main()

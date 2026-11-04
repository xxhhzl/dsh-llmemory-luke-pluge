# -*- coding: utf-8 -*-
"""memory_report：错条目的纠错通道（只标状态，不改正文）。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tools
from database import Cognition, Database, Experience

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_report"
S1 = "s_report"


class ReportCase(unittest.TestCase):
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


class TestEventReport(ReportCase):
    def test_marks_event_for_review(self):
        event = self.db.create_event(S1, "写错的事件", summary="旧摘要")
        out = tools.memory_report(event.id, "event", "与事实不符：当时并未执行")
        self.assertTrue(out["needs_review"])
        row = self.db.get_event(event.id)
        self.assertTrue(row.needs_review)
        self.assertIn("与事实不符", row.review_reason)
        self.assertEqual(row.summary, "旧摘要", "只标状态，不改内容")

    def test_missing_event_reports_not_found(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.memory_report(999999, "event", "x")
        self.assertEqual(ctx.exception.code, 1003)


class TestL3Report(ReportCase):
    def test_cognition_goes_invalid_and_leaves_index(self):
        item = self.db.insert_cognition(Cognition(
            subject="用户", content="错误事实：库在 ~/.dsh/harness",
            source_event_id=1, created_at="2026-11-04T10:00:00+00:00"))
        self.db.index_fts("cognition", item, "", "错误事实：库在 ~/.dsh/harness")
        self.assertFalse(self.db.search_fts("错误事实") == [])

        out = tools.memory_report(item, "cognition", "路径不存在")

        self.assertEqual(out["status"], "invalid")
        row = self.db.get_cognition(item)
        self.assertEqual(row.status, "invalid")
        self.assertIn("路径不存在", row.invalid_reason)
        self.assertEqual(self.db.search_fts("错误事实"), [], "失效条目应摘索引")

    def test_experience_report_works_too(self):
        item = self.db.insert_experience(Experience(
            content="错误的做法", source_event_id=1,
            created_at="2026-11-04T10:00:00+00:00"))
        tools.memory_report(item, "experience", "不成立")
        self.assertEqual(self.db.get_experience(item).status, "invalid")

    def test_missing_l3_reports_not_found(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.memory_report(999999, "cognition", "x")
        self.assertEqual(ctx.exception.code, 1003)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""反馈工具（src/tools.py 的 memory_feedback）：四档信号、批量、别名。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import retrieval
import tools
from database import Database

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_feedback"


class FeedbackCase(unittest.TestCase):
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

    def _note(self, text, kind="experience"):
        return tools.memory_note(text, kind=kind,
                                 subject=("测试主体" if kind == "cognition" else None))

    def _counts(self, kind, item_id):
        row = self.db._conn.execute(
            f"SELECT use_count, feedback_score FROM {kind} WHERE id = ?",
            (item_id,)).fetchone()
        return int(row["use_count"]), int(row["feedback_score"])

    def _hits(self, query):
        return [hit.id for hit in retrieval.search(
            self.db, query, limit=10, scope="all", touch=False)]


class TestFeedbackSignals(FeedbackCase):
    def test_useful_raises_use_count(self):
        item = self._note("有用的经验甲")
        tools.memory_feedback([item["id"]], kind="experience", signal="useful")
        use_count, score = self._counts("experience", item["id"])
        self.assertEqual(use_count, 1)
        self.assertEqual(score, 1)

    def test_useless_lowers_score_without_deleting(self):
        item = self._note("没用的经验乙")
        tools.memory_feedback([item["id"]], kind="experience", signal="useless")
        use_count, score = self._counts("experience", item["id"])
        self.assertEqual(use_count, 0)
        self.assertEqual(score, -1)
        self.assertIn(item["id"], self._hits("没用的经验乙"))

    def test_outdated_leaves_recall(self):
        item = self._note("过时的经验丙")
        tools.memory_feedback([item["id"]], kind="experience", signal="outdated")
        row = self.db._conn.execute(
            "SELECT valid_to FROM experience WHERE id = ?",
            (item["id"],)).fetchone()
        self.assertTrue(row["valid_to"])
        self.assertNotIn(item["id"], self._hits("过时的经验丙"))

    def test_wrong_marks_invalid(self):
        item = self._note("错误的认知丁", kind="cognition")
        tools.memory_feedback([item["id"]], kind="cognition", signal="wrong",
                              note="与事实不符")
        row = self.db._conn.execute(
            "SELECT status, invalid_reason FROM cognition WHERE id = ?",
            (item["id"],)).fetchone()
        self.assertEqual(row["status"], "invalid")
        self.assertEqual(row["invalid_reason"], "与事实不符")
        self.assertNotIn(item["id"], self._hits("错误的认知丁"))


class TestFeedbackBatch(FeedbackCase):
    def test_batch_updates_each(self):
        first = self._note("批量经验戊")
        second = self._note("批量经验己")
        result = tools.memory_feedback(
            [first["id"], second["id"]], kind="experience", signal="useless")
        self.assertEqual(result["updated"], 2)
        for item in (first, second):
            self.assertEqual(self._counts("experience", item["id"])[1], -1)

    def test_missing_id_reported(self):
        result = tools.memory_feedback([99999], kind="experience", signal="useless")
        self.assertEqual(result["updated"], 0)
        self.assertFalse(result["items"][0]["ok"])

    def test_empty_ids_rejected(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.memory_feedback([], kind="experience", signal="useful")
        self.assertEqual(ctx.exception.code, -32602)


class TestReportAlias(FeedbackCase):
    def test_report_equals_wrong(self):
        item = self._note("别名测试庚", kind="cognition")
        tools.memory_report(item["id"], kind="cognition", reason="过时了")
        row = self.db._conn.execute(
            "SELECT status FROM cognition WHERE id = ?",
            (item["id"],)).fetchone()
        self.assertEqual(row["status"], "invalid")


if __name__ == "__main__":
    unittest.main()

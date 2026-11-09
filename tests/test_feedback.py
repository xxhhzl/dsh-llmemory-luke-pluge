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

    def _scores(self, query):
        return {(hit.kind, hit.id): hit.score for hit in retrieval.search(
            self.db, query, limit=10, scope="all", touch=False)}


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


class TestFeedbackActuallyScores(FeedbackCase):
    '''反馈必须改变检索排序，而不只是改数据库字段。'''

    def test_useless_lowers_search_score(self):
        item = self._note("反馈降权测试辛")
        key = ("experience", item["id"])
        before = self._scores("反馈降权测试辛")[key]
        tools.memory_feedback([item["id"]], kind="experience", signal="useless")
        after = self._scores("反馈降权测试辛")[key]
        self.assertLess(after, before, "useless 之后同一条目的分数必须下降")

    def test_useful_raises_search_score(self):
        item = self._note("反馈加权测试壬")
        key = ("experience", item["id"])
        before = self._scores("反馈加权测试壬")[key]
        tools.memory_feedback([item["id"]], kind="experience", signal="useful")
        after = self._scores("反馈加权测试壬")[key]
        self.assertGreater(after, before, "useful 之后同一条目的分数必须上升")

    def test_feedback_is_bounded(self):
        '''连续 20 次 useless 不能把分数压成负数。'''
        item = self._note("反馈饱和测试癸")
        for _ in range(20):
            tools.memory_feedback([item["id"]], kind="experience", signal="useless")
        key = ("experience", item["id"])
        score = self._scores("反馈饱和测试癸")[key]
        self.assertGreaterEqual(score, 0.0)

    def test_wrong_event_leaves_recall(self):
        event = self.db.create_event("s_feedback", "错误的事件摘要")
        self.db.set_event_summary(event.id, "这是一条与事实不符的事件摘要")
        self.db.index_fts("event", event.id, event.title, "这是一条与事实不符的事件摘要",
                          session_id="s_feedback")
        self.assertIn(("event", event.id), self._scores("事实不符"))
        result = tools.memory_feedback([event.id], kind="event", signal="wrong",
                                       note="与事实不符")
        self.assertTrue(result["items"][0]["ok"])
        self.assertNotIn(("event", event.id), self._scores("事实不符"),
                         "标了 wrong 的事件必须退出召回")
        row = self.db._conn.execute(
            "SELECT valid_to, needs_review FROM event WHERE id = ?",
            (event.id,)).fetchone()
        self.assertTrue(row["valid_to"], "事件失效要留下时间戳")
        self.assertEqual(row["needs_review"], 1, "同时保留待复核标记")

    def test_wrong_l3_removes_vector(self):
        if not self.db.vec_status().get("enabled"):
            self.skipTest("向量不可用")
        item = self._note("向量残留测试子")
        dim = self.db.vec_status().get("dim") or 0
        vector = [0.0] * dim
        vector[0] = 1.0
        self.db.vec_upsert("experience", item["id"], vector)
        self.assertTrue(self.db.vec_search("experience", vector, k=5))
        tools.memory_feedback([item["id"]], kind="experience", signal="wrong")
        self.assertEqual(self.db.vec_search("experience", vector, k=5), {},
                         "失效条目必须同时摘掉向量，否则白占 KNN 名额")


class TestNoteDedupScope(FeedbackCase):
    '''memory_note 查重必须限定：同 kind、范围兼容、认知同主体（0.5.2）。'''

    def test_same_kind_same_project_is_duplicate(self):
        first = tools.memory_note("默认采用中文回复", kind="cognition",
                                  subject="项目甲", origin_project="proj_a")
        second = tools.memory_note("默认采用中文回复", kind="cognition",
                                   subject="项目甲", origin_project="proj_a")
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(self.db.count_l3("cognition"), 1)

    def test_different_project_is_not_duplicate(self):
        first = tools.memory_note("默认采用中文回复", kind="cognition",
                                  subject="项目甲", origin_project="proj_a")
        second = tools.memory_note("默认采用中文回复", kind="cognition",
                                   subject="项目甲", origin_project="proj_b")
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "created",
                         "不同项目的同名事实是两件事")
        self.assertEqual(self.db.count_l3("cognition"), 2)

    def test_different_subject_is_not_duplicate(self):
        tools.memory_note("默认采用中文回复", kind="cognition",
                          subject="项目甲", origin_project="proj_a")
        second = tools.memory_note("默认采用中文回复", kind="cognition",
                                   subject="项目乙", origin_project="proj_a")
        self.assertEqual(second["action"], "created",
                         "同一项目里的不同主体不能合并")

    def test_different_kind_is_not_duplicate(self):
        tools.memory_note("默认采用中文回复", kind="experience",
                          origin_project="proj_a")
        second = tools.memory_note("默认采用中文回复", kind="cognition",
                                   subject="项目甲", origin_project="proj_a")
        self.assertEqual(second["action"], "created",
                         "经验与认知是两类记忆，不互相判重")
        self.assertEqual(self.db.count_l3("experience"), 1)
        self.assertEqual(self.db.count_l3("cognition"), 1)

    def test_project_duplicate_of_global_is_not_created(self):
        '''全局条目对本项目可见，再写一条项目内同名经验属于重复。'''
        first = tools.memory_note("导出前统一线宽", kind="experience",
                                  origin_project=None)
        second = tools.memory_note("导出前统一线宽", kind="experience",
                                   origin_project="proj_a")
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(self.db.count_l3("experience"), 1)


if __name__ == "__main__":
    unittest.main()

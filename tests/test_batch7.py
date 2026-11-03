# -*- coding: utf-8 -*-
"""批7 回归：背压、复核状态落库、连接复用、事务计数、边界守卫。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import config
import ingest
import tools
from database import Database, Experience
from segmenter import summarize_event
from summary import SummaryResult

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_batch7"
S1 = "s_b7"


class Batch7Case(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestBackpressure(unittest.TestCase):
    """队列有界：满时有限等待后丢弃并计数，绝不无限阻塞。"""

    def test_send_reports_full_and_counts_drop(self):
        q = base.message_queue(1)
        self.assertTrue(q.send("a"))
        self.assertFalse(q.send("b"))
        self.assertEqual(q.dropped, 1)
        self.assertEqual(q.depth(), 1)

    def test_send_blocking_counts_once_not_per_retry(self):
        q = base.message_queue(1)
        q.send("a")
        self.assertFalse(q.send_blocking("b", timeout=0.1, interval=0.02))
        self.assertEqual(q.dropped, 1, "重试不应重复计丢弃")
        self.assertEqual(q.depth(), 1)

    def test_wait_empty_wakes_on_consume(self):
        q = base.message_queue(2)
        q.send("a")
        self.assertEqual(q.recv(), "a")
        self.assertTrue(q.wait_empty(0.2))

    def test_health_reports_dropped(self):
        q = base.message_queue(2)
        q.send("occupied-1")
        q.send("occupied-2")
        q.send("dropped")          # 满 → 计一次丢弃
        q.recv(); q.recv()         # 腾出空间再探活
        base.health({"type": "health", "request_id": "r", "session_id": S1,
                     "agent_id": "a"}, q)
        frames = []
        while True:
            item = q.recv()
            if not item:
                break
            frames.append(item)
        health_frame = [json.loads(f) for f in frames
                        if json.loads(f)["type"] == "health"]
        self.assertEqual(health_frame[0]["data"]["queue_dropped"], 1)


class TestReviewState(Batch7Case):
    """复核状态落库：重启后仍能查到哪些事件待补。"""

    def test_failed_summary_marks_review(self):
        result = ingest.apply_summary(
            self.db, session_id=S1,
            summary=SummaryResult(ok=False, error="json_parse", needs_review=True))
        event = self.db.get_event(result.event.id)
        self.assertEqual(event.needs_review, 1)
        self.assertEqual(event.review_reason, "summary_failed")
        self.assertEqual([e.id for e in self.db.list_events_needing_review()],
                         [result.event.id])

    def test_successful_summary_clears_review(self):
        failed = ingest.apply_summary(
            self.db, session_id=S1,
            summary=SummaryResult(ok=False, error="json_parse", needs_review=True))
        ingest.apply_summary(
            self.db, session_id=S1, event_id=failed.event.id,
            summary=SummaryResult(ok=True, title="标题", summary="摘要。"))
        event = self.db.get_event(failed.event.id)
        self.assertEqual(event.needs_review, 0)
        self.assertEqual(self.db.list_events_needing_review(), [])

    def test_needs_review_flag_also_marks(self):
        result = ingest.apply_summary(
            self.db, session_id=S1,
            summary=SummaryResult(ok=True, title="标题", summary="摘要。",
                                  needs_review=True))
        self.assertEqual(self.db.get_event(result.event.id).needs_review, 1)


class TestConnectionReuse(Batch7Case):
    def test_db_is_cached_per_path(self):
        backup = config.get_config()
        config.patch_config({"store": {"db_path": str(self.db_path)}})
        try:
            tools.reset_db_cache()
            first = tools._db()
            second = tools._db()
            self.assertIs(first, second, "同一路径应复用同一连接")
        finally:
            tools.reset_db_cache()
            config._config = backup

    def test_two_searches_share_connection(self):
        backup = config.get_config()
        config.patch_config({"store": {"db_path": str(self.db_path)}})
        try:
            tools.reset_db_cache()
            item = self.db.insert_experience(Experience(
                content="配色规范", source_event_id=1,
                created_at="2026-11-03T00:00:00+00:00"))
            self.db.index_fts("experience", item, "", "配色规范")
            tools.memory_search("配色")
            conn_before = id(tools._db()._conn)
            tools.memory_search("配色")
            self.assertEqual(id(tools._db()._conn), conn_before)
        finally:
            tools.reset_db_cache()
            config._config = backup


class TestTransactionCounting(Batch7Case):
    def test_bump_events_rolls_back_with_transaction(self):
        event = self.db.create_event(S1, "事件")
        try:
            with self.db.transaction():
                self.db.bump_events([event.id])
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        self.assertEqual(self.db.get_event(event.id).cnt, 0, "回滚应撤销计数")

    def test_bump_events_commits(self):
        event = self.db.create_event(S1, "事件")
        self.db.bump_events([event.id], 2)
        self.assertEqual(self.db.get_event(event.id).cnt, 2)


class TestBoundaries(unittest.TestCase):
    def test_summarize_event_empty_list(self):
        self.assertEqual(summarize_event([]), ("", ""))

    def test_nested_transaction_uses_unique_savepoints(self):
        d = BASE_TMP / uuid.uuid4().hex
        d.mkdir(parents=True)
        db = Database(d / "m.db")
        try:
            with db.transaction():
                with db.transaction():
                    db.insert_message(__import__("database").Message(
                        session_id=S1, message_id="m1", owner="a", seq=0,
                        role="user", content="x",
                        occur_at="2026-11-03T00:00:00+00:00",
                        created_at="2026-11-03T00:00:00+00:00"))
                with db.transaction():
                    db.insert_message(__import__("database").Message(
                        session_id=S1, message_id="m2", owner="a", seq=1,
                        role="user", content="y",
                        occur_at="2026-11-03T00:00:01+00:00",
                        created_at="2026-11-03T00:00:01+00:00"))
            self.assertEqual(db.count_messages(S1), 2)
        finally:
            db.close()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

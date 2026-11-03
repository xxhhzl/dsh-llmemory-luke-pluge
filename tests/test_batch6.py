# -*- coding: utf-8 -*-
"""批6 回归：增量切分（只判定新消息、不重复建事件、预算含已有正文）。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import config
import ingest
from database import Database
from jev import FakeTransport
from summary import FakeSummaryTransport

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_batch6"
S1 = "s_b6"
GOOD = '{"title":"标题","summary":"摘要。","experiences":[],"cognitions":[]}'


class Batch6Case(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.db = Database(self.db_path)
        self.transports: list = []
        self.worker = ingest.IngestWorker(
            q=base.message_queue(),
            db_factory=lambda: Database(self.db_path),
            jev_factory=self._jev,
            summary_factory=lambda: FakeSummaryTransport([GOOD] * 20),
            consolidate_factory=lambda: FakeSummaryTransport([]))

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self._dir, ignore_errors=True)

    def _jev(self):
        transport = FakeTransport([0.05] * 200)     # 一律 CONTINUE
        self.transports.append(transport)
        return transport

    def judge_calls(self) -> int:
        return sum(len(t.calls) for t in self.transports)

    def run_job(self, start_seq: int, items):
        messages, seq = [], start_seq
        for role, text in items:
            messages.append({"msg_id": f"m{seq}", "seq": seq, "role": role,
                             "content": text, "ts": "2026-11-03T00:00:00+00:00"})
            seq += 1
        job = {"session_id": S1, "agent_id": "a", "envelope": {}, "job_id": "j",
               "origin_project": None, "messages": messages}
        return self.worker.process(job)

    def verify(self) -> Database:
        return Database(self.db_path)


class TestIncremental(Batch6Case):
    def test_second_run_only_judges_new_messages(self):
        self.run_job(0, [("user", "任务一"), ("assistant", "好的"),
                         ("user", "继续"), ("assistant", "好")])
        first_calls = self.judge_calls()
        self.assertGreater(first_calls, 0)

        payload = self.run_job(4, [("user", "再继续"), ("assistant", "收到")])
        second_calls = self.judge_calls() - first_calls
        self.assertLessEqual(second_calls, 2,
                             f"第二轮只应判定 2 条新消息，实际 {second_calls}")

    def test_no_duplicate_event(self):
        self.run_job(0, [("user", "任务一"), ("assistant", "好的")])
        payload = self.run_job(2, [("user", "继续做"), ("assistant", "收到")])
        self.assertEqual(payload["summary"]["events_created"], 0)
        with self.verify() as db:
            self.assertEqual(db.count_events(S1), 1, "增量不应重复建事件")
            event = db.list_events(S1)[0]
            self.assertEqual(len(db.list_event_messages(event.id)), 4,
                             "新消息应全部绑到同一事件")

    def test_all_messages_bound_after_two_runs(self):
        self.run_job(0, [("user", "任务一"), ("assistant", "好的")])
        self.run_job(2, [("user", "继续做"), ("assistant", "收到")])
        with self.verify() as db:
            unbound = db._conn.execute(
                "SELECT COUNT(*) AS n FROM memory WHERE session_id = ? "
                "AND event_id IS NULL", (S1,)).fetchone()["n"]
            self.assertEqual(unbound, 0, "不该有未绑定消息")

    def test_empty_first_run_is_full_scan(self):
        payload = self.run_job(0, [("user", "第一条"), ("assistant", "好的")])
        self.assertEqual(payload["summary"]["events_created"], 1)
        payload = self.run_job(2, [("user", "第二条"), ("assistant", "好")])
        self.assertEqual(payload["summary"]["events_created"], 0)


class TestBudgetWithOpenEvent(Batch6Case):
    """open_event 的既有正文要计入 token 预算，否则续写段能无限膨胀。"""

    def test_token_budget_counts_existing_body(self):
        config.patch_config({"split": {"max_event_tokens": 500}})
        try:
            # 首轮正文约 800 token，已超 500 预算
            self.run_job(0, [("user", "内容" * 400), ("assistant", "好")])
            payload = self.run_job(2, [("user", "继续"), ("assistant", "收到")])
        finally:
            config.patch_config({"split": {"max_event_tokens": 128000}})
        self.assertGreaterEqual(payload["summary"]["events_created"], 1,
                                "旧事件已超预算，新消息应开新事件")


class TestEmptyContinuationDropped(Batch6Case):
    """续写段一条都没进来就 SPLIT：不能留下空事件（validate 会拒）。"""

    def test_first_new_message_splits(self):
        self.run_job(0, [("user", "任务一"), ("assistant", "好的")])
        payload = self.run_job(2, [("user", "换个话题：新任务")])
        self.assertEqual(payload["summary"]["events_created"], 1)
        with self.verify() as db:
            self.assertEqual(db.count_events(S1), 2)
            for event in db.list_events(S1):
                self.assertTrue(db.list_event_messages(event.id),
                                "事件不应为空")


if __name__ == "__main__":
    unittest.main()

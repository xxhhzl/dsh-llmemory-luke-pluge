# -*- coding: utf-8 -*-
"""ingest.request 链路单测：回执、worker 处理、ingest.completed 帧。"""
from __future__ import annotations

import json
import shutil
import sys
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import ingest
from database import Database
from jev import FakeTransport
from summary import FakeSummaryTransport

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_ingest_worker"
S1 = "s_worker_1"

SUMMARY_JSON = json.dumps({
    "title": "调边框",
    "summary": "把边框改成 1px，导出成功。",
    "experiences": [{"context": "导出前", "content": "统一 strokeWidth",
                     "kind": "technique", "importance": 4, "confidence": 0.9,
                     "cues": ["drawio"]}],
    "cognitions": [{"subject": "用户", "content": "偏好中文回复",
                    "kind": "preference", "importance": 5, "confidence": 0.9}],
}, ensure_ascii=False)
DECISIONS_JSON = json.dumps({"decisions": []})


def make_worker(q, db_path):
    return ingest.IngestWorker(
        q,
        db_factory=lambda: Database(db_path),
        jev_factory=lambda: FakeTransport(),
        summary_factory=lambda: FakeSummaryTransport([SUMMARY_JSON]),
        consolidate_factory=lambda: FakeSummaryTransport([DECISIONS_JSON]),
    )


def messages(seqs=(0, 1)):
    out = []
    for seq in seqs:
        out.append({
            "msg_id": f"m_{seq}", "seq": seq,
            "role": "user" if seq % 2 == 0 else "assistant",
            "content": "把边框改细" if seq % 2 == 0 else "已改成 1px",
            "ts": f"2026-11-02T10:0{seq}:00+08:00",
        })
    return out


def frame(q):
    raw = q.recv_blocking(timeout=2)
    return json.loads(raw) if raw else None


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.q = base.message_queue()

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)


class TestProcess(WorkerCase):
    def test_writes_messages_events_and_completed_frame(self):
        worker = make_worker(self.q, self.db_path)
        payload = worker.process({
            "job_id": "job_1", "session_id": S1, "agent_id": "a1",
            "messages": messages(), "origin_project": None,
            "envelope": {"type": "ingest.request", "request_id": "r1",
                         "session_id": S1, "agent_id": "a1", "job_id": "job_1"},
        })
        self.assertEqual(payload["summary"]["messages"], 2)
        self.assertEqual(payload["summary"]["events_created"], 1)
        self.assertEqual(payload["summary"]["summaries"], 1)
        self.assertIn("[L2 #", payload["digest"])
        self.assertIn("调边框", payload["digest"])
        self.assertGreater(payload["digest_tokens"], 0)

        out = frame(self.q)
        self.assertEqual(out["type"], "ingest.completed")
        self.assertEqual(out["job_id"], "job_1")
        self.assertEqual(out["data"]["summary"]["messages"], 2)

        with Database(self.db_path) as db:
            self.assertEqual(db.count_messages(S1), 2)
            self.assertEqual(db.count_events(S1), 1)
            self.assertEqual(len(db.list_experiences()), 1)
            self.assertEqual(len(db.list_cognitions()), 1)

    def test_duplicate_msg_id_is_skipped(self):
        worker = make_worker(self.q, self.db_path)
        msgs = messages() + [dict(messages()[0])]
        payload = worker.process({
            "job_id": "job_dup", "session_id": S1, "agent_id": "a1",
            "messages": msgs, "origin_project": None,
            "envelope": {"type": "ingest.request", "session_id": S1,
                         "agent_id": "a1", "job_id": "job_dup"},
        })
        self.assertEqual(payload["summary"]["messages"], 2)
        self.assertEqual(payload["summary"]["duplicates"], 1)
        self.assertIn("跳过重复 1 条", payload["display"])

    def test_second_run_resumes_existing_event(self):
        worker = make_worker(self.q, self.db_path)
        job = {"job_id": "job_1", "session_id": S1, "agent_id": "a1",
               "messages": messages(), "origin_project": None,
               "envelope": {"type": "ingest.request", "session_id": S1,
                            "agent_id": "a1", "job_id": "job_1"}}
        worker.process(job)
        worker.process({**job, "job_id": "job_2", "messages": messages((2, 3))})
        with Database(self.db_path) as db:
            self.assertEqual(db.count_events(S1), 1)      # 仍是同一个事件
            self.assertEqual(db.count_messages(S1), 4)

    def test_resume_token_budget_ignores_tool_output_in_dialog_scope(self):
        '''口径一致：dialog 范围下续写段的 open_event.tokens 不数 tool 输出。

        构造：已绑定事件 = user + 巨大 tool 输出；续写一条 user。
        若 open_event.tokens 误把 tool 输出算进去，128000 的预算会被 tool
        输出顶破，续写消息会被 token 规则硬切成新事件；口径一致时应继续留在
        原事件（FakeTransport 默认 CONTINUE，token 预算是唯一可能触发 SPLIT
        的规则）。
        '''
        big_tool = "x" * 800000                       # ≈20 万 token，远超 128000
        first = [
            {"msg_id": "m_u", "seq": 0, "role": "user",
             "content": "跑一下测试", "ts": "2026-11-02T10:00:00+08:00"},
            {"msg_id": "m_t", "seq": 1, "role": "tool",
             "content": big_tool, "ts": "2026-11-02T10:01:00+08:00"},
        ]
        worker = make_worker(self.q, self.db_path)
        worker.process({
            "job_id": "job_1", "session_id": S1, "agent_id": "a1",
            "messages": first, "origin_project": None,
            "envelope": {"type": "ingest.request", "session_id": S1,
                         "agent_id": "a1", "job_id": "job_1"}})
        worker.process({
            "job_id": "job_2", "session_id": S1, "agent_id": "a1",
            "messages": [{"msg_id": "m_u2", "seq": 2, "role": "user",
                          "content": "结果呢", "ts": "2026-11-02T10:02:00+08:00"}],
            "origin_project": None,
            "envelope": {"type": "ingest.request", "session_id": S1,
                         "agent_id": "a1", "job_id": "job_2"}})
        with Database(self.db_path) as db:
            self.assertEqual(db.count_events(S1), 1)   # 未被 tool 输出顶破预算

    def test_resume_token_budget_counts_tool_output_in_all_scope(self):
        '''all 范围：tool 输出计入预算，续写段被 token 规则硬切成新事件。'''
        import config as _config
        big_tool = "x" * 800000
        _config.patch_config({"split": {"token_scope": "all"}})
        try:
            first = [
                {"msg_id": "m_u", "seq": 0, "role": "user",
                 "content": "跑一下测试", "ts": "2026-11-02T10:00:00+08:00"},
                {"msg_id": "m_t", "seq": 1, "role": "tool",
                 "content": big_tool, "ts": "2026-11-02T10:01:00+08:00"},
            ]
            worker = make_worker(self.q, self.db_path)
            worker.process({
                "job_id": "job_1", "session_id": S1, "agent_id": "a1",
                "messages": first, "origin_project": None,
                "envelope": {"type": "ingest.request", "session_id": S1,
                             "agent_id": "a1", "job_id": "job_1"}})
            worker.process({
                "job_id": "job_2", "session_id": S1, "agent_id": "a1",
                "messages": [{"msg_id": "m_u2", "seq": 2, "role": "user",
                              "content": "结果呢",   # 不命中转折词，SPLIT 只能来自 token 预算
                              "ts": "2026-11-02T10:02:00+08:00"}],
                "origin_project": None,
                "envelope": {"type": "ingest.request", "session_id": S1,
                             "agent_id": "a1", "job_id": "job_2"}})
            with Database(self.db_path) as db:
                self.assertEqual(db.count_events(S1), 2)   # token 预算硬切
        finally:
            _config.patch_config({"split": {"token_scope": "dialog"}})


class TestRequestHandler(WorkerCase):
    def test_receipt_then_completed(self):
        worker = ingest.start_worker(
            self.q,
            db_factory=lambda: Database(self.db_path),
            jev_factory=lambda: FakeTransport(),
            summary_factory=lambda: FakeSummaryTransport([SUMMARY_JSON]),
            consolidate_factory=lambda: FakeSummaryTransport([DECISIONS_JSON]))
        data = {"type": "ingest.request", "request_id": "r1",
                "session_id": S1, "agent_id": "a1",
                "data": {"cursor": {"last_seq": -1}, "messages": messages()}}
        ingest.ingest_request(data, self.q)

        receipt = frame(self.q)
        self.assertEqual(receipt["type"], "ingest.receipt")
        self.assertTrue(receipt["data"]["accepted"])
        self.assertEqual(receipt["data"]["new_messages"], 2)

        completed = frame(self.q)
        self.assertEqual(completed["type"], "ingest.completed")
        self.assertEqual(completed["job_id"], receipt["job_id"])
        self.assertEqual(completed["data"]["summary"]["events_created"], 1)

    def test_cursor_skips_processed_and_rejects_empty(self):
        ingest.start_worker(
            self.q,
            db_factory=lambda: Database(self.db_path),
            jev_factory=lambda: FakeTransport(),
            summary_factory=lambda: FakeSummaryTransport([SUMMARY_JSON]),
            consolidate_factory=lambda: FakeSummaryTransport([DECISIONS_JSON]))
        data = {"type": "ingest.request", "request_id": "r1",
                "session_id": S1, "agent_id": "a1",
                "data": {"cursor": {"last_seq": 0}, "messages": messages()}}
        ingest.ingest_request(data, self.q)
        receipt = frame(self.q)
        self.assertEqual(receipt["data"]["new_messages"], 1)   # 只剩 seq=1
        self.assertEqual(frame(self.q)["type"], "ingest.completed")

    def test_all_processed_returns_not_accepted(self):
        ingest.start_worker(
            self.q,
            db_factory=lambda: Database(self.db_path),
            jev_factory=lambda: FakeTransport(),
            summary_factory=lambda: FakeSummaryTransport([SUMMARY_JSON]),
            consolidate_factory=lambda: FakeSummaryTransport([DECISIONS_JSON]))
        data = {"type": "ingest.request", "request_id": "r1",
                "session_id": S1, "agent_id": "a1",
                "data": {"cursor": {"last_seq": 5}, "messages": messages()}}
        ingest.ingest_request(data, self.q)
        receipt = frame(self.q)
        self.assertFalse(receipt["data"]["accepted"])
        self.assertEqual(receipt["data"]["new_messages"], 0)
        self.assertIsNone(frame(self.q))     # 不再有 completed


if __name__ == "__main__":
    unittest.main()

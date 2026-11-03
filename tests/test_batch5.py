# -*- coding: utf-8 -*-
"""批5 回归：摘要重试策略、per-event 隔离、补跑阈值、digest 刷新次数。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import ingest
from database import Database
from jev import FakeTransport
from summary import EventMeta, FakeSummaryTransport, SummaryError, extract

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_batch5"
S1 = "s_b5"
GOOD = '{"title":"标题","summary":"摘要。","experiences":[],"cognitions":[]}'


class Batch5Case(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self._dir, ignore_errors=True)


class TestRetryPolicy(Batch5Case):
    """M3：只重试瞬时错误；解析失败才追加提示后缀；重试降半输入。"""

    def meta(self):
        return EventMeta(event_id=1, session_id=S1, message_count=1,
                         first_at="2026-11-03T00:00:00+00:00",
                         last_at="2026-11-03T00:00:00+00:00", tokens=10)

    def messages(self):
        # 单条消息渲染时会被截到 800 字符，要触发总预算裁剪得多条
        return [{"msg_id": f"m{i}", "role": "user", "content": "内容" * 300,
                 "ts": "2026-11-03T00:00:00+00:00"} for i in range(200)]

    def test_retryable_error_retries_once(self):
        transport = FakeSummaryTransport([SummaryError("超时", retryable=True), GOOD])
        result = extract(self.meta(), self.messages(), transport)
        self.assertTrue(result.ok)
        self.assertEqual(len(transport.calls), 2)

    def test_non_retryable_error_does_not_retry(self):
        transport = FakeSummaryTransport([SummaryError("鉴权失败", retryable=False), GOOD])
        result = extract(self.meta(), self.messages(), transport)
        self.assertFalse(result.ok)
        self.assertEqual(len(transport.calls), 1)

    def test_parse_failure_retries_with_suffix(self):
        transport = FakeSummaryTransport(["不是 JSON", GOOD])
        result = extract(self.meta(), self.messages(), transport)
        self.assertTrue(result.ok)
        self.assertEqual(len(transport.calls), 2)
        system = transport.calls[1][0]["content"]
        self.assertIn("【重试】", system)

    def test_retry_halves_input_without_parse_suffix(self):
        transport = FakeSummaryTransport([SummaryError("timeout", retryable=True), GOOD])
        result = extract(self.meta(), self.messages(), transport,
                         max_input_chars=24000)
        self.assertTrue(result.ok)
        first_user = transport.calls[0][-1]["content"]
        second_user = transport.calls[1][-1]["content"]
        self.assertLess(len(second_user), len(first_user))
        self.assertIn("已裁剪", second_user)
        self.assertNotIn("【重试】", transport.calls[1][0]["content"])

    def test_gives_up_after_two_failures(self):
        transport = FakeSummaryTransport([SummaryError("超时", retryable=True),
                                          SummaryError("超时", retryable=True)])
        result = extract(self.meta(), self.messages(), transport)
        self.assertFalse(result.ok)
        self.assertTrue(result.needs_review)
        self.assertEqual(len(transport.calls), 2)


class SeqSummaryTransport:
    """第一次抛错、之后正常；记录调用次数。"""

    def __init__(self, fail_times=1, exc=None):
        self.fail_times = fail_times
        self.exc = exc or RuntimeError("boom")
        self.calls = []

    def complete(self, messages, *, timeout=None):
        self.calls.append(messages)
        if len(self.calls) <= self.fail_times:
            raise self.exc
        return GOOD


class TestEventIsolation(Batch5Case):
    """per-event 隔离：单个事件失败不中断整个 job。"""

    def _job(self):
        return {"session_id": S1, "agent_id": "a", "envelope": {}, "job_id": "j1",
                "origin_project": None, "messages": [
                    {"msg_id": "m1", "seq": 0, "role": "user", "content": "任务一"},
                    {"msg_id": "m2", "seq": 1, "role": "assistant", "content": "好的"},
                    {"msg_id": "m3", "seq": 2, "role": "user", "content": "换个话题：任务二"},
                    {"msg_id": "m4", "seq": 3, "role": "assistant", "content": "明白"},
                ]}

    def test_failed_event_does_not_stop_job(self):
        """第一个事件在落库阶段抛异常，第二个事件仍要成功。"""
        calls = {"n": 0}
        original = ingest.ingest_event

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("落库炸了")
            return original(*args, **kwargs)

        ingest.ingest_event = flaky
        try:
            worker = ingest.IngestWorker(
                q=base.message_queue(), db_factory=lambda: Database(self.db_path),
                jev_factory=lambda: FakeTransport([0.05]),
                summary_factory=lambda: FakeSummaryTransport([GOOD, GOOD]),
                consolidate_factory=lambda: FakeSummaryTransport([]))
            payload = worker.process(self._job())
        finally:
            ingest.ingest_event = original
        self.assertEqual(payload["summary"]["events_created"], 1)   # 第二个成功
        self.assertEqual(payload["summary"]["failed"], 1)          # 第一个已计数
        self.assertIn("失败 1 个", payload["display"])

    def test_digest_refreshed_once_per_job(self):
        counter = {"n": 0}
        original = ingest.refresh_digest

        def counted(*args, **kwargs):
            counter["n"] += 1
            return original(*args, **kwargs)

        ingest.refresh_digest = counted
        try:
            worker = ingest.IngestWorker(
                q=base.message_queue(), db_factory=lambda: Database(self.db_path),
                jev_factory=lambda: FakeTransport([0.05]),
                summary_factory=lambda: FakeSummaryTransport([GOOD, GOOD]),
                consolidate_factory=lambda: FakeSummaryTransport([]))
            payload = worker.process(self._job())
        finally:
            ingest.refresh_digest = original
        self.assertEqual(payload["summary"]["events_created"], 2)
        self.assertEqual(counter["n"], 1, "digest 应在 job 末尾只刷一次")


class TestResummarizeThreshold(Batch5Case):
    """补跑摘要：新增少不重跑，新增多才重跑，且带 previous_summary。"""

    def _run(self, worker, start_seq, contents):
        messages = []
        seq = start_seq
        for role, text in contents:
            messages.append({"msg_id": f"m{seq}", "seq": seq, "role": role,
                             "content": text, "ts": "2026-11-03T00:00:00+00:00"})
            seq += 1
        job = {"session_id": S1, "agent_id": "a", "envelope": {}, "job_id": "j",
               "origin_project": None, "messages": messages}
        return worker.process(job)

    def _worker(self, summary_calls):
        def factory():
            transport = FakeSummaryTransport([GOOD] * 10)
            summary_calls.append(transport)
            return transport

        return ingest.IngestWorker(q=base.message_queue(),
                                   db_factory=lambda: Database(self.db_path),
                                   jev_factory=lambda: FakeTransport([0.05]),
                                   summary_factory=factory,
                                   consolidate_factory=lambda: FakeSummaryTransport([]))

    def test_few_new_messages_do_not_rerun_summary(self):
        calls = []
        worker = self._worker(calls)
        self._run(worker, 0, [("user", "任务一"), ("assistant", "好的")])
        first = len(calls)
        payload = self._run(worker, 2, [("assistant", "补充一句")])
        self.assertEqual(payload["summary"]["events_created"], 0)
        self.assertEqual(len(calls), first, "新增 1 条不应重跑摘要")

    def test_many_new_messages_rerun_with_previous_summary(self):
        calls = []
        worker = self._worker(calls)
        self._run(worker, 0, [("user", "任务一"), ("assistant", "好的")])
        first = len(calls)
        payload = self._run(worker, 2, [("user", f"继续 {i}") for i in range(5)])
        self.assertEqual(payload["summary"]["events_created"], 0)
        self.assertGreater(len(calls), first, "新增 ≥5 条应重跑摘要")
        rerun_payload = calls[-1].calls[-1][-1]["content"]
        self.assertIn("[已有摘要]", rerun_payload)
        with Database(self.db_path) as verify:
            self.assertEqual(verify.count_events(S1), 1, "重跑不得新建事件")


if __name__ == "__main__":
    unittest.main()

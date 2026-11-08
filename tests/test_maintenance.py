# -*- coding: utf-8 -*-
"""维护任务（src/maintenance.py）：重跑事件摘要、归纳洞见、快照与并发保护。"""
from __future__ import annotations

import json
import shutil
import sys
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import maintenance                                 # noqa: E402
from database import Database, Experience, Memory, Message  # noqa: E402
from summary import FakeSummaryTransport           # noqa: E402

BASE_TMP = ROOT / ".tmp" / "test_maintenance"
S1 = "s_maint"
SUMMARY_OK = {
    "title": "补跑标题",
    "summary": "补跑摘要正文。",
    "experiences": [{"content": "补跑经验"}],
    "cognitions": [{"subject": "用户", "content": "补跑认知"}],
}


class MaintenanceCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")
        self._orig = {name: dict(getattr(maintenance, name))
                      for name in ("RESUMMARIZE", "INDUCE")}

        def restore():
            for name, snapshot in self._orig.items():
                getattr(maintenance, name).update(snapshot)
        self.addCleanup(restore)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def wait(self, snapshot, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not snapshot["running"]:
                return snapshot
            time.sleep(0.02)
        self.fail("维护任务未在超时内结束")

    def make_event(self, texts=(("m1", "user", "把边框改成 1px"),),
                   session: str = S1) -> int:
        event = self.db.create_event(session, "未命名事件")
        for seq, (msg_id, role, content) in enumerate(texts):
            self.db.insert_message(Message(
                session_id=session, message_id=msg_id, owner="a", seq=seq,
                role=role, content=content,
                occur_at="2026-11-03T10:00:00+08:00",
                created_at="2026-11-03T10:00:00+08:00"))
            self.db.insert_memory(Memory(
                session_id=session, message_id=msg_id, owner="a", seq=seq,
                occur_at="2026-11-03T10:00:00+08:00",
                created_at="2026-11-03T10:00:00+08:00"))
            self.db.bind_event(session, msg_id, event.id)
        self.db.set_event_review(event.id, True, "summary_failed")
        return event.id

    def seed_experience(self, content, cues=("edit",)) -> int:
        return self.db.insert_experience(Experience(
            context="改文件时", content=content, kind="pitfall",
            importance=3, origin_session=S1, source_event_id=1,
            cues=tuple(cues), created_at="2026-11-01T09:00:00+08:00"))


class TestResummarize(MaintenanceCase):
    def test_rewrites_summary_and_clears_review(self):
        event_id = self.make_event()
        transport = FakeSummaryTransport(
            [json.dumps(SUMMARY_OK, ensure_ascii=False)])
        result = maintenance.resummarize(
            self.db, event_ids=[event_id],
            transport_factory=lambda: transport, consolidate_factory=None,
            refresh_digest=False)
        self.assertTrue(result["started"])

        state = self.wait(maintenance.RESUMMARIZE)
        self.assertFalse(state["running"])
        self.assertEqual((state["ok"], state["failed"], state["done"]), (1, 0, 1))
        self.assertTrue(state["finished_at"])
        self.assertGreaterEqual(state["elapsed_ms"], 0)

        event = self.db.get_event(event_id)
        self.assertEqual(event.summary, "补跑摘要正文。")
        self.assertEqual(event.title, "补跑标题")
        self.assertEqual(event.needs_review, 0, "成功后要清待复核")
        self.assertEqual(self.db.count_l3("experience"), 1)
        self.assertEqual(self.db.count_l3("cognition"), 1)

    def test_failure_keeps_review_and_reports_note(self):
        event_id = self.make_event()
        transport = FakeSummaryTransport([RuntimeError("上游 429 限流")])
        maintenance.resummarize(
            self.db, event_ids=[event_id],
            transport_factory=lambda: transport, consolidate_factory=None,
            refresh_digest=False)
        state = self.wait(maintenance.RESUMMARIZE)
        self.assertEqual(state["failed"], 1)
        self.assertEqual(state["ok"], 0)
        self.assertIn("429", state["note"])
        self.assertEqual(self.db.get_event(event_id).needs_review, 1)

    def test_default_scope_is_pending_events(self):
        waiting = self.make_event()
        clean = self.db.create_event(S1, "已完成事件")
        self.db.set_event_summary(clean.id, "旧摘要")
        self.db.set_event_review(clean.id, False, None)
        transport = FakeSummaryTransport(
            [json.dumps(SUMMARY_OK, ensure_ascii=False)])
        maintenance.resummarize(
            self.db, transport_factory=lambda: transport,
            consolidate_factory=None, refresh_digest=False)
        state = self.wait(maintenance.RESUMMARIZE)
        self.assertEqual(state["total"], 1, "只跑待复核事件")
        self.assertEqual(state["ok"], 1)
        self.assertEqual(self.db.get_event(waiting).summary, "补跑摘要正文。")
        self.assertEqual(self.db.get_event(clean.id).summary, "旧摘要")

    def test_second_start_is_rejected(self):
        maintenance.RESUMMARIZE.update({"running": True, "done": 3, "total": 9})
        result = maintenance.resummarize(self.db, transport_factory=None)
        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "already_running")
        self.assertEqual(result["state"]["done"], 3)
        maintenance.RESUMMARIZE["running"] = False

    def test_empty_queue_finishes_without_model_call(self):
        def boom():
            raise AssertionError("没有待复核事件时不该调模型")
        maintenance.resummarize(self.db, transport_factory=boom,
                               consolidate_factory=None, refresh_digest=False)
        state = self.wait(maintenance.RESUMMARIZE)
        self.assertEqual((state["total"], state["done"], state["ok"]), (0, 0, 0))


class TestInduce(MaintenanceCase):
    def test_writes_insights_and_reports_progress(self):
        for content in ("甲做法", "乙做法", "丙做法"):
            self.seed_experience(content)
        response = json.dumps({"insights": [{
            "content": "编辑前先读取当前内容", "scope": "global",
            "confidence": 0.9, "cues": ["edit"], "source_ids": [1, 2]}]},
            ensure_ascii=False)
        transport = FakeSummaryTransport([response])
        result = maintenance.induce_insights(
            self.db, transport_factory=lambda: transport,
            min_support=3, max_clusters=1)
        self.assertTrue(result["started"])

        state = self.wait(maintenance.INDUCE)
        self.assertFalse(state["running"])
        self.assertEqual(state["error"], "")
        self.assertEqual(state["clusters"], 1)
        self.assertEqual(state["created"], 1)
        self.assertEqual(state["done"], 1)
        self.assertEqual(self.db.count_l3("insight"), 1)

    def test_second_start_is_rejected(self):
        maintenance.INDUCE.update({"running": True, "done": 2, "total": 4})
        result = maintenance.induce_insights(self.db, transport_factory=None)
        self.assertFalse(result["started"])
        self.assertEqual(result["state"]["done"], 2)
        maintenance.INDUCE["running"] = False


class TestStatus(MaintenanceCase):
    def test_reports_pending_and_shapes(self):
        self.make_event()
        state = maintenance.status(self.db)
        for key in ("resummarize", "induce", "pending_events", "insights"):
            self.assertIn(key, state)
        self.assertEqual(state["pending_events"], 1)
        self.assertIn("running", state["resummarize"])
        self.assertIn("created", state["induce"])


if __name__ == "__main__":
    unittest.main()

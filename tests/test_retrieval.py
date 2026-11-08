# -*- coding: utf-8 -*-
"""检索层（src/retrieval.py）与工具（src/tools.py）单测。

不联网：只用 SQLite + FTS。
"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import retrieval
import tools
from database import Cognition, Database, Experience

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_retrieval"
S1 = "s_retrieval_1"


def iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class RetrievalCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.db = Database(self.db_path)
        self._orig_db = tools._db
        tools._db = lambda: Database(self.db_path)

    def tearDown(self):
        tools._db = self._orig_db
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def add_event(self, title, summary, *, session=S1, cnt=0):
        event = self.db.create_event(session, title, summary=summary)
        self.db.index_fts("event", event.id, title, summary)
        if cnt:
            self.db.bump_event_cnt(event.id, cnt)
        return event.id

    def add_experience(self, content, *, context="导出前", cues=(), importance=3,
                       use_count=0, last_used_at=None, origin_project=None):
        item = self.db.insert_experience(Experience(
            context=context, content=content, kind="technique",
            importance=importance, confidence=0.9, origin_project=origin_project,
            source_event_id=1, cues=tuple(cues), created_at=iso(10),
            use_count=use_count, last_used_at=last_used_at))
        self.db.index_fts("experience", item, "", content)
        return item

    def add_cognition(self, content, *, subject="用户", importance=3,
                      origin_project=None):
        item = self.db.insert_cognition(Cognition(
            subject=subject, content=content, kind="preference",
            importance=importance, confidence=0.9, origin_project=origin_project,
            source_event_id=1, created_at=iso(10)))
        self.db.index_fts("cognition", item, "", content)
        return item


class TestActivation(RetrievalCase):
    def test_more_use_higher(self):
        now = datetime.now(timezone.utc)
        low = retrieval.activation(1, iso(1), iso(1), now)
        high = retrieval.activation(10, iso(1), iso(1), now)
        self.assertGreater(high, low)

    def test_older_lower(self):
        now = datetime.now(timezone.utc)
        fresh = retrieval.activation(1, iso(1), iso(1), now)
        old = retrieval.activation(1, iso(90), iso(90), now)
        self.assertGreater(fresh, old)


class TestSearch(RetrievalCase):
    def test_finds_event_and_l3(self):
        event = self.add_event("调 drawio 边框", "把边框改成 1px")
        exp = self.add_experience("统一 strokeWidth 再导出")
        cog = self.add_cognition("偏好中文回复")
        hits = retrieval.search(self.db, "drawio", touch=False)
        self.assertEqual(hits[0].kind, "event")
        kinds = {hit.kind for hit in hits}
        self.assertIn("event", kinds)
        self.assertGreaterEqual(len(hits), 1)

    def test_filters_inactive_and_expired(self):
        exp = self.add_experience("统一 strokeWidth 再导出")
        self.db.invalidate_experience(exp, "旧做法")
        hits = retrieval.search(self.db, "strokeWidth", touch=False)
        self.assertEqual(hits, [])

    def test_project_scope(self):
        a = self.add_cognition("项目 A 用 pnpm", origin_project="proj_a")
        b = self.add_cognition("项目 B 用 npm", origin_project="proj_b")
        scoped = retrieval.search(self.db, "项目", scope="project",
                                  origin_project="proj_a", touch=False)
        self.assertEqual({hit.id for hit in scoped if hit.kind == "cognition"}, {a})
        everything = retrieval.search(self.db, "项目", scope="all", touch=False)
        self.assertEqual({hit.id for hit in everything if hit.kind == "cognition"},
                         {a, b})

    def _same_cluster_experiences(self, count: int) -> int:
        event = self.add_event("drawio 边框", "边框 1px")
        for index in range(count):
            self.add_experience(f"drawio 经验 {index}", cues=("drawio",))
        with self.db.transaction():
            for row in self.db.list_experiences():
                self.db._conn.execute(
                    "UPDATE experience SET source_event_id = ? WHERE id = ?",
                    (event, row.id))
        return event

    def test_cluster_cap_keeps_diversity(self):
        # 同一事件有 6 条经验，另有 6 条独立认知：limit=6 时不能全被这一簇占满
        self._same_cluster_experiences(6)
        for index in range(6):
            self.add_cognition(f"drawio 认知 {index}", subject="用户")
        hits = retrieval.search(self.db, "drawio", limit=6, touch=False)
        self.assertLessEqual(len([h for h in hits if h.kind == "experience"]), 2,
                             "簇上限保证多样性")
        self.assertEqual(len(hits), 6, "但也不该欠填")

    def test_cluster_cap_backfills_when_candidates_run_out(self):
        # 全库只有这一簇：宁可放宽上限，也不要只回 2 条
        self._same_cluster_experiences(4)
        hits = retrieval.search(self.db, "drawio", limit=10, touch=False)
        self.assertEqual(len([h for h in hits if h.kind == "experience"]), 4)

    def test_touch_updates_usage(self):
        event = self.add_event("drawio 边框", "边框 1px")
        retrieval.search(self.db, "drawio", touch=True)
        self.assertEqual(self.db.get_event(event).cnt, 1)

    def test_short_query_uses_like(self):
        self.add_cognition("偏好中文回复")
        hits = retrieval.search(self.db, "偏好", touch=False)
        self.assertEqual(hits[0].kind, "cognition")


class TestGet(RetrievalCase):
    def test_event_returns_messages(self):
        event = self.add_event("drawio 边框", "边框 1px")
        from database import Memory, Message
        self.db.insert_message(Message(session_id=S1, message_id="m1", owner="a",
                                       seq=0, role="user", content="改边框",
                                       occur_at=iso(), created_at=iso()))
        self.db.insert_memory(Memory(session_id=S1, message_id="m1", owner="a",
                                     seq=0, occur_at=iso(), created_at=iso()))
        self.db.bind_event(S1, "m1", event)
        out = retrieval.get(self.db, "event", event)
        self.assertEqual(out["event"]["id"], event)
        self.assertEqual(out["messages"][0]["content"], "改边框")
        self.assertFalse(out["truncated"])
        self.assertIsNone(out["next_msg_offset"])
        self.assertEqual(out["total_chars"], 3)
        self.assertEqual(self.db.get_event(event).cnt, 1)     # 再认也算使用

    def test_event_body_pages_without_loss_or_duplication(self):
        event = self.add_event("长事件", "摘要")
        from database import Memory, Message
        for index, content in enumerate(("12345", "abcde", "最后一条")):
            msg_id = f"page_{index}"
            self.db.insert_message(Message(
                session_id=S1, message_id=msg_id, owner="a", seq=index,
                role="user", content=content, occur_at=iso(), created_at=iso()))
            self.db.insert_memory(Memory(
                session_id=S1, message_id=msg_id, owner="a", seq=index,
                occur_at=iso(), created_at=iso()))
            self.db.bind_event(S1, msg_id, event)

        first = retrieval.get(self.db, "event", event, max_chars=10)
        self.assertEqual([m["content"] for m in first["messages"]],
                         ["12345", "abcde"])
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_msg_offset"], 2)
        self.assertEqual(first["total_chars"], 14)

        second = retrieval.get(self.db, "event", event, max_chars=1,
                               msg_offset=first["next_msg_offset"])
        self.assertEqual([m["content"] for m in second["messages"]], ["最后一条"])
        self.assertFalse(second["truncated"])
        self.assertIsNone(second["next_msg_offset"])

        full = retrieval.get(self.db, "event", event, max_chars=0)
        self.assertEqual(len(full["messages"]), 3)
        self.assertFalse(full["truncated"])


class TestTools(RetrievalCase):
    def test_memory_search_tool(self):
        self.add_event("调 drawio 边框", "把边框改成 1px")
        out = tools.memory_search("drawio")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["items"][0]["kind"], "event")

    def test_tools_call_returns_result_frame(self):
        q = base.message_queue()
        envelope = {"type": "tools.calls", "request_id": "r1",
                    "session_id": S1, "agent_id": "a1"}
        tools.tools_call({**envelope, "data": {"calls": [
            {"call_id": "c1", "name": "memory_search",
             "arguments": {"query": "drawio"}},
            {"call_id": "c2", "name": "nope", "arguments": {}},
        ]}}, q)
        frame = json.loads(q.recv())
        self.assertEqual(frame["type"], "tools.result")
        self.assertEqual(len(frame["data"]), 2)
        self.assertTrue(frame["data"][0]["ok"])
        self.assertFalse(frame["data"][1]["ok"])

    def test_tools_schema_lists_all(self):
        q = base.message_queue()
        tools.get_tools_schema({"type": "tools.schema", "request_id": "r1",
                                "session_id": S1, "agent_id": "a1"}, q)
        frame = json.loads(q.recv())
        names = [item["name"] for item in frame["data"]]
        self.assertEqual(names, ["memory_search", "memory_get", "memory_recent",
                                 "memory_report", "memory_self_write"])


if __name__ == "__main__":
    unittest.main()


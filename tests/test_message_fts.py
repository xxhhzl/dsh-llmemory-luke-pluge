# -*- coding: utf-8 -*-
"""原文索引（message_fts）：摘要没写到的编号/原话也要能搜到。

复现口径：事件正文只写「讨论合同审批流程」，而原文里出现「ZXQ-73059」。
旧实现只索引事件摘要与 L3，按编号搜返回 0 条——原文存了却搜不出来。
"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import database                                    # noqa: E402
import retrieval                                   # noqa: E402
from database import Database, Memory, Message     # noqa: E402

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_message_fts"
S1 = "s_fts"
RAW = "我们把合同过了一遍，合同编号 ZXQ-73059 先在法务那边盖章"
SUMMARY = "讨论合同审批流程"


def iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MessageFtsCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _add_message(self, message_id: str, seq: int, content: str) -> None:
        now = iso()
        self.db.insert_message(Message(
            session_id=S1, message_id=message_id, owner="u", seq=seq,
            role="user", content=content, occur_at=now, created_at=now))
        self.db.insert_memory(Memory(
            session_id=S1, message_id=message_id, owner="u", seq=seq,
            occur_at=now, created_at=now))
        self.db.index_message_fts(S1, message_id, content)

    def _seed(self):
        '''建一个事件，摘要不写编号，原文写编号，消息绑定到该事件。'''
        event = self.db.create_event(S1, "合同讨论", SUMMARY)
        self.db.index_fts("event", event.id, event.title, SUMMARY, session_id=S1)
        self._add_message("m1", 1, RAW)
        self.db.bind_event(S1, "m1", event.id)
        return event


class TestMessageFts(MessageFtsCase):
    def test_raw_number_is_searchable(self):
        event = self._seed()
        hits = self.db.search_message_fts("ZXQ-73059")
        self.assertEqual(len(hits), 1, "原文里的编号必须有检索入口")
        self.assertEqual(hits[0]["event_id"], event.id)
        self.assertIn("ZXQ-73059", hits[0]["excerpt"])

    def test_retrieval_maps_message_hit_to_event(self):
        '''检索层要把「原文命中」回连成事件，否则召回拿不到这条。'''
        event = self._seed()
        hits = retrieval.search(self.db, "ZXQ-73059", scope="all", limit=5,
                                touch=False)
        self.assertIn(event.id, [hit.id for hit in hits if hit.kind == "event"])

    def test_summary_query_still_works(self):
        '''原有摘要命中的路径不能被新通路挤掉。'''
        self._seed()
        hits = retrieval.search(self.db, "合同审批", scope="all", limit=5,
                                touch=False)
        self.assertTrue(any(hit.kind == "event" for hit in hits))

    def test_unbound_message_is_not_returned(self):
        '''还没归属事件的消息不返回：召回无法说明「这是哪件事」。'''
        self._add_message("m9", 9, "孤立一笔 编号 QQ-12345")
        self.assertEqual(self.db.search_message_fts("QQ-12345"), [])

    def test_reindex_backfills_raw_messages(self):
        event = self._seed()
        # 模拟升级前的老库：清掉原文索引后 reindex 必须能补回来
        self.db._conn.execute("DELETE FROM message_fts")
        counts = self.db.reindex_fts()
        self.assertEqual(counts["message"], 1)
        hits = self.db.search_message_fts("ZXQ-73059")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["event_id"], event.id)

    def test_reindex_does_not_revive_retired_event(self):
        '''失效事件的索引行在失效时已删；全量重建不能把它加回来。'''
        event = self._seed()
        self.assertTrue(self.db.retire_event(event.id, "不再相关"))
        self.assertEqual(self.db.search_message_fts("ZXQ-73059"), [],
                         "失效后原文不该命中")
        self.assertEqual(self.db.search_fts("合同审批"), [],
                         "失效后摘要也不该命中")

        self.db.reindex_fts()
        self.assertEqual(self.db.search_message_fts("ZXQ-73059"), [],
                         "重建不能把失效事件的原文加回来")
        self.assertEqual(self.db.search_fts("合同审批"), [],
                         "重建不能把失效事件加回事件索引")

    def test_reindex_preserves_insight_and_excludes_expired_l3(self):
        from database import Experience, Insight
        old_id = self.db.insert_experience(Experience(
            content="过期经验独特词", source_event_id=0, created_at=iso()))
        self.db.index_fts("experience", old_id, "", "过期经验独特词")
        self.assertTrue(self.db.mark_l3_outdated("experience", old_id, "过时"))
        insight_id = self.db.insert_insight(Insight(
            content="独特洞见关键词", created_at=iso()))
        self.db.index_fts("insight", insight_id, "", "独特洞见关键词")

        counts = self.db.reindex_fts()
        self.assertEqual(counts["insight"], 1)
        self.assertEqual(self.db.search_fts("过期经验独特词"), [])
        self.assertEqual([(row["ref_kind"], row["ref_id"])
                          for row in self.db.search_fts("独特洞见关键词")],
                         [("insight", insight_id)])

    def test_busy_event_does_not_starve_others(self):
        '''单个事件命中几百条时，不能把别的事件挤出候选窗口。'''
        busy = self.db.create_event(S1, "热门事件", "反复提到同一个词")
        self.db.index_fts("event", busy.id, "热门事件", "反复提到同一个词",
                          session_id=S1)
        for index in range(60):
            self._add_message(f"a{index}", index + 1, "关键词相同的一大段内容")
            self.db.bind_event(S1, f"a{index}", busy.id)

        quiet = self.db.create_event(S1, "冷门事件", "只提过一次")
        self.db.index_fts("event", quiet.id, "冷门事件", "只提过一次", session_id=S1)
        self._add_message("b1", 200, "关键词相同的一大段内容")
        self.db.bind_event(S1, "b1", quiet.id)

        hits = self.db.search_message_fts("关键词", limit=2)
        ids = {item["event_id"] for item in hits}
        self.assertIn(quiet.id, ids, "单事件不能占满候选窗口")
        self.assertIn(busy.id, ids)
        self.assertEqual(len(hits), 2, "每个事件只出一条候选")


if __name__ == "__main__":
    unittest.main()

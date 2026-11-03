# -*- coding: utf-8 -*-
"""批1 回归：写入原子性、supersede 不变量、strict 绑定、事件范围列。

对应评审结论：
- H1 消息双写同事务 + seq 冲突不再被静默吞掉；
- H2 supersede 守卫（旧行非 active 时降级新增）+ 同批 target 去重；
- M1 bind_event strict（事件闭合后正文不改绑）；
- event.origin_project（范围是数据固有属性）。
"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import consolidate
from consolidate import parse_decisions
from database import Cognition, Database, Experience, Memory, Message
from ingest import IngestWorker, apply_summary
from summary import SummaryResult

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_invariants"
S1 = "s_inv_1"


def msg(msg_id: str, seq: int, content: str = "正文") -> Message:
    return Message(session_id=S1, message_id=msg_id, owner="a", seq=seq,
                   role="user", content=content,
                   occur_at="2026-11-03T10:00:00+08:00",
                   created_at="2026-11-03T10:00:00+08:00")


def exp(content: str = "做法") -> Experience:
    return Experience(content=content, source_event_id=1,
                      created_at="2026-11-03T10:00:00+08:00")


def cog(content: str = "事实") -> Cognition:
    return Cognition(subject="项目", content=content, source_event_id=1,
                     created_at="2026-11-03T10:00:00+08:00")


def summary(*, experiences=(), cognitions=()) -> SummaryResult:
    return SummaryResult(ok=True, title="标题", summary="摘要。",
                         experiences=tuple(experiences), cognitions=tuple(cognitions))


class InvariantCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestMessageAtomicity(InvariantCase):
    """H1：message 与 memory 同事务，seq 冲突不静默丢正文。"""

    def test_seq_conflict_reported_not_silently_dropped(self):
        self.assertEqual(self.db.insert_message(msg("m_1", 0, "第一条")), 1)
        # 不同 msg_id 抢同一 seq：必须是冲突（-1），不能算「重复」静默丢弃
        self.assertEqual(self.db.insert_message(msg("m_2", 0, "第二条")), -1)
        self.assertEqual(self.db.count_messages(S1), 1)
        self.assertEqual(self.db.get_message(S1, "m_1").content, "第一条")

    def test_batch_returns_conflicts(self):
        inserted, conflicts = self.db.insert_messages(
            [msg("m_1", 0), msg("m_2", 0), msg("m_3", 1)])
        self.assertEqual(inserted, 2)
        self.assertEqual(conflicts, [("m_2", 0)])

    def test_duplicate_msg_id_is_not_a_conflict(self):
        self.db.insert_message(msg("m_1", 0))
        self.assertEqual(self.db.insert_message(msg("m_1", 0)), 0)

    def test_store_messages_leaves_no_orphan_row_on_failure(self):
        """memory 写入抛错时，本批 message 一并回滚（不留孤儿行）。"""
        worker = IngestWorker(q=None, db_factory=lambda: self.db)
        job = {"session_id": S1, "messages": [
            {"msg_id": "m_1", "seq": 0, "role": "user", "content": "a"},
            {"msg_id": "m_2", "seq": 1, "role": "user", "content": "b"},
        ]}
        with mock.patch.object(self.db, "insert_memory",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                worker._store_messages(self.db, job)
        self.assertEqual(self.db.count_messages(S1), 0)      # 整批回滚
        self.assertEqual(self.db.count_memory(S1), 0)

    def test_store_messages_counts_seq_conflict(self):
        worker = IngestWorker(q=None, db_factory=lambda: self.db)
        self.db.insert_message(msg("m_old", 5, "占位"))
        job = {"session_id": S1, "messages": [
            {"msg_id": "m_new", "seq": 5, "role": "user", "content": "撞车"},
            {"msg_id": "m_ok", "seq": 6, "role": "user", "content": "正常"},
        ]}
        inserted, duplicates, conflicts = worker._store_messages(self.db, job)
        self.assertEqual((inserted, duplicates, conflicts), (1, 0, 1))
        self.assertIsNone(self.db.get_message(S1, "m_new"))   # 正文没写进去
        self.assertEqual(self.db.count_memory(S1), 1)         # 只有 m_ok 的引用


class TestSupersedeInvariant(InvariantCase):
    """H2：旧行至多一个 active 后继，且非 active 目标降级而非炸事件。"""

    def test_supersede_non_active_returns_none(self):
        old = self.db.insert_experience(exp("旧"))
        self.assertIsNotNone(self.db.supersede_experience(old, exp("新")))
        self.assertIsNone(self.db.supersede_experience(old, exp("又新")))

    def test_supersede_missing_still_raises(self):
        with self.assertRaises(KeyError):
            self.db.supersede_experience(99999, exp())

    def test_second_supersede_does_not_create_second_active_successor(self):
        old = self.db.insert_experience(exp("旧"))
        first = self.db.supersede_experience(old, exp("新1"))
        second = self.db.supersede_experience(old, exp("新2"))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        active = self.db.list_experiences()
        self.assertEqual([item.id for item in active], [first])

    def test_apply_summary_degrades_stale_target_to_new(self):
        """模型给了过期 target：该条按新增落库，事件其余部分不受影响。"""
        old = self.db.insert_experience(exp("旧"))
        self.db.supersede_experience(old, exp("已取代"))
        item = {"context": "场景", "content": "新做法", "kind": "technique",
                "importance": 3, "confidence": 0.8, "cues": ()}
        result = apply_summary(
            self.db, session_id=S1, summary=summary(experiences=(item,)),
            decisions={0: {"action": "supersede", "target_id": old}})
        self.assertEqual(len(result.experience_ids), 1)
        self.assertEqual(len(self.db.list_experiences()), 2)   # 已取代 + 新增


class TestTargetDedupe(unittest.TestCase):
    """H2 上游：同批多条指同一 target 时只保留首条 supersede。"""

    def test_duplicate_target_degrades_to_new(self):
        items = [{"table": "experience"}, {"table": "experience"}]
        candidates = [[mock.Mock(id=7)], [mock.Mock(id=7)]]
        raw = ('{"decisions": ['
               '{"index": 0, "action": "supersede", "target_id": 7},'
               '{"index": 1, "action": "supersede", "target_id": 7}]}')
        decisions = parse_decisions(raw, items, candidates).items
        self.assertEqual(decisions[0]["action"], "supersede")
        self.assertEqual(decisions[0]["target_id"], 7)
        self.assertEqual(decisions[1]["action"], "new")
        self.assertIsNone(decisions[1]["target_id"])

    def test_distinct_targets_both_kept(self):
        items = [{"table": "experience"}, {"table": "experience"}]
        candidates = [[mock.Mock(id=7)], [mock.Mock(id=8)]]
        raw = ('{"decisions": ['
               '{"index": 0, "action": "supersede", "target_id": 7},'
               '{"index": 1, "action": "invalidate", "target_id": 8, "reason": "过时"}]}')
        decisions = parse_decisions(raw, items, candidates).items
        self.assertEqual(decisions[0]["action"], "supersede")
        self.assertEqual(decisions[1]["action"], "invalidate")


class TestStrictBind(InvariantCase):
    """M1：事件闭合后正文不改绑；冲突只告警不污染。"""

    def _memory(self, msg_id: str, seq: int):
        self.db.insert_message(msg(msg_id, seq))
        self.db.insert_memory(Memory(session_id=S1, message_id=msg_id, owner="a",
                                     seq=seq, occur_at="2026-11-03T10:00:00+08:00",
                                     created_at="2026-11-03T10:00:00+08:00"))

    def test_strict_refuses_rebind(self):
        self._memory("m_1", 0)
        first = self.db.create_event(S1, "事件一")
        second = self.db.create_event(S1, "事件二")
        self.assertTrue(self.db.bind_event(S1, "m_1", first.id, strict=True))
        self.assertFalse(self.db.bind_event(S1, "m_1", second.id, strict=True))
        self.assertEqual(self.db.bound_event_of(S1, "m_1"), first.id)

    def test_non_strict_still_rebinds(self):
        self._memory("m_1", 0)
        first = self.db.create_event(S1, "事件一")
        second = self.db.create_event(S1, "事件二")
        self.db.bind_event(S1, "m_1", first.id)
        self.assertTrue(self.db.bind_event(S1, "m_1", second.id))
        self.assertEqual(self.db.bound_event_of(S1, "m_1"), second.id)

    def test_apply_summary_does_not_steal_bound_messages(self):
        self._memory("m_old", 0)
        owner = self.db.create_event(S1, "旧事件")
        self.db.bind_event(S1, "m_old", owner.id, strict=True)

        self._memory("m_new", 1)
        result = apply_summary(self.db, session_id=S1, summary=summary(),
                               message_ids=["m_old", "m_new"])
        self.assertEqual(self.db.bound_event_of(S1, "m_old"), owner.id)   # 未被抢走
        self.assertEqual(self.db.bound_event_of(S1, "m_new"), result.event.id)


class TestEventScope(InvariantCase):
    """event.origin_project：范围是数据属性，NULL 表示全局可见。"""

    def test_create_event_records_origin_project(self):
        event = self.db.create_event(S1, "项目事件", origin_project="proj_a")
        self.assertEqual(event.origin_project, "proj_a")
        self.assertEqual(self.db.get_event(event.id).to_dict()["origin_project"], "proj_a")

    def test_default_is_global(self):
        event = self.db.create_event(S1, "全局事件")
        self.assertIsNone(event.origin_project)

    def test_apply_summary_passes_origin_project(self):
        result = apply_summary(self.db, session_id=S1, summary=summary(),
                               origin_project="proj_b")
        self.assertEqual(result.event.origin_project, "proj_b")


if __name__ == "__main__":
    unittest.main()

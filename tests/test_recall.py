# -*- coding: utf-8 -*-
"""被动回想（src/core.py 的 _build_recall）：注入、不加热度、冷却、可关闭。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import config
import core
import database
import retrieval
from database import Cognition, Database, Insight, Memory, Message

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_recall"
S1 = "s_recall"


def iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecallCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")
        core._recall_state.clear()

    def tearDown(self):
        core._recall_state.clear()
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _with_recall(self, **overrides):
        base = config.get_config()
        recall = {**base.get("recall", {}), "min_score": 0.0, **overrides}
        return mock.patch.object(config, "get_config",
                                 return_value={**base, "recall": recall})

    def _seed(self):
        item = self.db.insert_cognition(Cognition(
            subject="用户", content="装插件之前先说明会改哪些文件",
            importance=4, confidence=0.9, source_event_id=1, created_at=iso()))
        self.db.index_fts("cognition", item, "用户", "装插件之前先说明会改哪些文件")
        for seq, text in enumerate(
                ("装插件之前要先说明吗", "装插件之前怎么准备"), start=1):
            self.db.insert_message(Message(
                session_id=S1, message_id=f"m{seq}", owner="u", seq=seq,
                role="user", content=text, occur_at=iso(), created_at=iso()))
        return item


class TestRecall(RecallCase):
    def test_produces_context_block(self):
        self._seed()
        with self._with_recall():
            payload = core.compile_core(self.db, session_id=S1)
        self.assertIn("<recall", payload["recall_text"])
        self.assertGreaterEqual(len(payload["recall_items"]), 1)

    def test_does_not_count_as_usage(self):
        item = self._seed()
        before = self.db.get_cognition(item).use_count
        with self._with_recall():
            core.compile_core(self.db, session_id=S1)
        self.assertEqual(self.db.get_cognition(item).use_count, before,
                         "注入不能算「被使用」，否则越召回越热")

    def test_cooldown_skips_repeat(self):
        self._seed()
        with self._with_recall():
            first = core.compile_core(self.db, session_id=S1)
            second = core.compile_core(self.db, session_id=S1)
        self.assertTrue(first["recall_items"])
        self.assertEqual(second["recall_items"], [], "同一会话内不该重复注入")

    def test_disabled_returns_empty(self):
        self._seed()
        with self._with_recall(enabled=False):
            payload = core.compile_core(self.db, session_id=S1)
        self.assertEqual(payload["recall_text"], "")
        self.assertEqual(payload["recall_items"], [])

    def test_without_session_never_borrows_latest(self):
        '''没有 session_id 时不能退回「最近有消息的会话」——那会把别的会话注入进来。'''
        self._seed()
        with self._with_recall():
            payload = core.compile_core(self.db, session_id=None)
        self.assertEqual(payload["recall_text"], "")
        self.assertEqual(payload["recall_items"], [])

    def test_query_hint_drives_recall(self):
        self._seed()
        with self._with_recall():
            payload = core.compile_core(self.db, session_id=S1,
                                        query_hint="装插件之前先说明")
        self.assertGreaterEqual(len(payload["recall_items"]), 1)

    def test_empty_recall_clears_snapshot(self):
        '''空召回也要覆盖快照，否则界面会显示上一轮的记忆。'''
        self._seed()
        with self._with_recall():
            core.compile_core(self.db, session_id=S1)
            self.assertTrue(core.last_recall()["text"])
            core.compile_core(self.db, session_id="s_other")
        snapshot = core.last_recall()
        self.assertEqual(snapshot["text"], "")
        self.assertEqual(snapshot["items"], [])
        self.assertEqual(snapshot["session_id"], "s_other")

    def test_background_compile_does_not_consume_cooldown(self):
        '''后台刷新（写入完成/设置变更）不能拨快冷却轮次。'''
        self._seed()
        with self._with_recall():
            first = core.compile_core(self.db, session_id=S1)
            self.assertTrue(first["recall_items"])
            for _ in range(9):
                core.compile_core(self.db, session_id=S1, advance_turn=False)
            second = core.compile_core(self.db, session_id=S1)
        self.assertEqual(second["recall_items"], [],
                         "冷却按用户步骤算，9 次后台编译不应把它耗光")

    def test_background_compile_keeps_snapshot(self):
        '''不编召回的后台刷新不覆盖最近一次召回快照。'''
        self._seed()
        with self._with_recall():
            core.compile_core(self.db, session_id=S1)
            before = core.last_recall()
            payload = core.compile_core(self.db, session_id=S1,
                                        advance_turn=False, use_recall=False)
            after = core.last_recall()
        self.assertTrue(payload["recall_skipped"])
        self.assertEqual(after, before)

    def test_release_undoes_unconsumed_cooldown(self):
        '''超时未消费的召回：释放后下一轮还能再召回，不白占冷却。'''
        self._seed()
        with self._with_recall():
            first = core.compile_core(self.db, session_id=S1)
            self.assertTrue(first["recall_items"])
            released = core._release_recall_state(S1)
            self.assertGreaterEqual(released, 1, "本轮标记应被撤掉")
            second = core.compile_core(self.db, session_id=S1)
        self.assertTrue(second["recall_items"], "释放后下一轮应能再召回同一条")

    def test_release_requires_matching_ticket(self):
        '''释放要凭本轮的票：抢在下一轮 core.get 前面到达时不能撤上一轮。'''
        self._seed()
        with self._with_recall():
            first = core.compile_core(self.db, session_id=S1, recall_ticket="t1")
            self.assertTrue(first["recall_items"])
            # 旧票（上一轮/未来轮）不匹配：不做任何修改
            self.assertEqual(core._release_recall_state(S1, "t0"), 0)
            self.assertTrue(core._recall_state[S1]["seen"], "标记不能被误撤")
            # 同票才生效
            self.assertGreaterEqual(core._release_recall_state(S1, "t1"), 1)

    def test_release_frame_entry(self):
        '''协议入口：外壳超时后按帧调用。'''
        self._seed()
        with self._with_recall():
            core.compile_core(self.db, session_id=S1)
            q = base.message_queue()
            core.release_recall({"type": "core.release", "request_id": "r",
                                 "data": {"session_id": S1}}, q)
            frame = json.loads(q.recv())
        self.assertEqual(frame["type"], "core.released")
        self.assertEqual(frame["data"]["session_id"], S1)
        self.assertGreaterEqual(frame["data"]["released"], 1)

    def test_query_vector_reaches_retrieval(self):
        '''外壳算好的向量必须真的交给检索层，否则被动召回仍只走字面命中。'''
        self._seed()
        sentinel = [0.125, -0.5, 0.75]
        with self._with_recall():
            with mock.patch.object(retrieval, "search",
                                   wraps=retrieval.search) as spy:
                core._build_recall(self.db, S1, "装插件之前先说明",
                                   query_vector=sentinel)
        self.assertEqual(spy.call_args.kwargs.get("query_vector"), sentinel)


    def _event_with_messages(self, session, title, message_ids):
        """给事件绑定真实引用，检验当前窗口是否完整覆盖。"""
        event = self.db.create_event(session, title, title)
        self.db.index_fts("event", event.id, title, title, session_id=session)
        for seq, message_id in enumerate(message_ids, start=1):
            self.db.insert_message(Message(
                session_id=session, message_id=message_id, owner="u", seq=seq,
                role="user", content=title, occur_at=iso(), created_at=iso()))
            self.db.insert_memory(Memory(
                session_id=session, message_id=message_id, owner="u", seq=seq,
                occur_at=iso(), created_at=iso()))
            self.db.bind_event(session, message_id, event.id)
        return event.id

    def test_core_and_recall_only_include_each_id_once(self):
        first = self.db.insert_insight(Insight(
            content="导出 SVG 前先检查字体", importance=5, scope="global",
            created_at=iso()))
        self.db.index_fts("insight", first, "", "导出 SVG 前先检查字体")
        with self._with_recall(same_id_cooldown=0):
            payload = core.compile_core(self.db, session_id=S1,
                                        query_hint="导出 SVG 前先检查字体")
        self.assertIn(f'<insight id="{first}">', payload["text"])
        self.assertNotIn(("insight", first),
                         [(row["kind"], row["id"]) for row in payload["recall_items"]])
        self.assertNotIn(("insight", first), core._recall_state[S1]["seen"])

    def test_only_actually_injected_core_items_are_excluded(self):
        first = self.db.insert_insight(Insight(
            content="检索预算 前置规则", importance=5, scope="global",
            created_at=iso()))
        second = self.db.insert_insight(Insight(
            content="检索预算 另一个重要规则", importance=4, scope="global",
            created_at=iso()))
        for item, text in ((first, "检索预算 前置规则"),
                           (second, "检索预算 另一个重要规则")):
            self.db.index_fts("insight", item, "", text)
        # 预算只够第一条；第二条仍可由被动检索补上。
        cfg = {**config.get_config()["core"], "max_chars": 150}
        with self._with_recall(same_id_cooldown=0):
            payload = core.compile_core(self.db, cfg=cfg, session_id=S1,
                                        query_hint="检索预算")
        self.assertIn(f'<insight id="{first}">', payload["text"])
        self.assertNotIn(f'<insight id="{second}">', payload["text"])
        self.assertIn(second, [row["id"] for row in payload["recall_items"]])

    def test_visible_event_is_skipped_but_partial_and_old_remain(self):
        current = self._event_with_messages(S1, "可见任务甲", ("v1", "v2"))
        partial = self._event_with_messages("partial", "可见任务甲", ("p1", "p2"))
        # 同会话另一个事件通过高序号写入，确保当前窗口不包含它。
        old = self.db.create_event(S1, "可见任务甲", "可见任务甲")
        self.db.index_fts("event", old.id, old.title, old.summary, session_id=S1)
        self.db.insert_message(Message(
            session_id=S1, message_id="old", owner="u", seq=3,
            role="user", content="可见任务甲", occur_at=iso(), created_at=iso()))
        self.db.insert_memory(Memory(
            session_id=S1, message_id="old", owner="u", seq=3,
            occur_at=iso(), created_at=iso()))
        self.db.bind_event(S1, "old", old.id)
        with self._with_recall(same_id_cooldown=0, max_items=5):
            payload = core.compile_core(
                self.db, session_id=S1, query_hint="可见任务甲",
                visible_message_ids=["v1", "v2", "p1"])
        ids = {row["id"] for row in payload["recall_items"]}
        self.assertNotIn(current, ids)
        self.assertIn(partial, ids, "别的会话即使消息 ID 相同也不应被本窗口过滤")
        self.assertIn(old.id, ids, "滑出窗口的当前会话旧事件仍可召回")
        self.assertNotIn(("event", current), core._recall_state[S1]["seen"])

    def test_visible_event_does_not_take_result_quota(self):
        current = self._event_with_messages(S1, "补位任务", ("current",))
        other = self._event_with_messages("another", "补位任务", ("external",))
        with self._with_recall(same_id_cooldown=0, max_items=1):
            payload = core.compile_core(
                self.db, session_id=S1, query_hint="补位任务",
                visible_message_ids=["current"])
        self.assertEqual([row["id"] for row in payload["recall_items"]], [other])
        self.assertNotIn(("event", current), core._recall_state[S1]["seen"])

    def test_identical_message_id_from_another_session_is_not_hidden(self):
        own = self._event_with_messages(S1, "相同编号任务", ("shared",))
        external = self._event_with_messages("external", "相同编号任务", ("shared",))
        with self._with_recall(same_id_cooldown=0, max_items=5):
            payload = core.compile_core(
                self.db, session_id=S1, query_hint="相同编号任务",
                visible_message_ids=["shared"])
        ids = {row["id"] for row in payload["recall_items"]}
        self.assertNotIn(own, ids)
        self.assertIn(external, ids, "消息 ID 仅在会话内唯一")

    def test_partly_visible_event_is_not_suppressed(self):
        event_id = self._event_with_messages(S1, "部分窗口任务", ("a", "b"))
        with self._with_recall(same_id_cooldown=0):
            payload = core.compile_core(
                self.db, session_id=S1, query_hint="部分窗口任务",
                visible_message_ids=["a"])
        self.assertIn(event_id, [row["id"] for row in payload["recall_items"]])

    def test_no_verified_projection_does_not_hide_same_session_history(self):
        event_id = self._event_with_messages(S1, "未验证投影任务", ("a",))
        with self._with_recall():
            payload = core.compile_core(
                self.db, session_id=S1, query_hint="未验证投影任务",
                visible_message_ids=None)
        self.assertIn(event_id, [row["id"] for row in payload["recall_items"]])



if __name__ == "__main__":
    unittest.main()

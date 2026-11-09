# -*- coding: utf-8 -*-
"""被动回想（src/core.py 的 _build_recall）：注入、不加热度、冷却、可关闭。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
import core
import database
from database import Cognition, Database, Message

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


if __name__ == "__main__":
    unittest.main()

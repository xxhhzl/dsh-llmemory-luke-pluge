# -*- coding: utf-8 -*-
"""核心记忆编译（src/core.py）单测：标签、预算、版本、自述快照。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import base                                       # noqa: E402
import config                                     # noqa: E402
import core                                       # noqa: E402
from database import (Cognition, Database, Experience, Insight,  # noqa: E402
                      SelfMemory)

BASE_TMP = ROOT / ".tmp" / "test_core"
S1 = "s_core"


class CoreCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def seed_self(self, content, source="configured", kind="identity", revision=1):
        return self.db.insert_self_memory(SelfMemory(
            source=source, kind=kind, content=content, revision=revision,
            origin_session=S1, created_at="2026-11-01T09:00:00+08:00"))

    def seed_insight(self, content, importance=5, scope="global"):
        return self.db.insert_insight(Insight(
            content=content, scope=scope, importance=importance,
            origin_session=S1, source_experience_ids=(1, 2), cues=("core",),
            created_at="2026-11-01T09:00:00+08:00"))

    def seed_experience(self, content, importance=5, scope="global"):
        return self.db.insert_experience(Experience(
            context="任何时候", content=content, kind="technique",
            importance=importance, scope=scope, origin_session=S1,
            source_event_id=1, cues=("core",),
            created_at="2026-11-01T09:00:00+08:00"))

    def seed_cognition(self, content, importance=5, subject="项目"):
        return self.db.insert_cognition(Cognition(
            subject=subject, content=content, kind="constraint",
            importance=importance, origin_session=S1,
            source_event_id=1, cues=("core",),
            created_at="2026-11-01T09:00:00+08:00"))


class TestCompile(CoreCase):
    def test_system_prompt_tags(self):
        self.seed_self("我是长期记忆宿主智能体。", kind="identity")
        insight_id = self.seed_insight("范围过滤要下推到召回层。")
        exp_id = self.seed_experience("编辑前先读取当前内容。")
        cog_id = self.seed_cognition("项目使用 SQLite。")
        payload = core.compile_core(self.db)

        text = payload["text"]
        self.assertIn("<long_term_memory>", text)
        self.assertIn("<self_configured>", text)
        self.assertIn('<self_item kind="identity"', text)
        self.assertIn("<core_memory>", text)
        self.assertIn(f'<insight id="{insight_id}">', text)
        self.assertIn(f'<experience id="{exp_id}">', text)
        self.assertIn(f'<cognition id="{cog_id}">', text)
        self.assertEqual(payload["stats"]["self_configured"], 1)
        self.assertEqual(payload["stats"]["insight"], 1)
        self.assertTrue(payload["version"])

    def test_self_notes_use_own_tags_and_revision(self):
        self.seed_self("我容易忘记早前约束。", source="agent",
                       kind="behavior", revision=3)
        self.seed_self("我只读记忆。", source="agent",
                       kind="limitation", revision=7)
        payload = core.compile_core(self.db)
        notes = payload["notes_text"]
        self.assertIn('<self_notes source="assistant" revision="7">', notes)
        self.assertIn('<note kind="behavior"', notes)
        self.assertIn('<note kind="limitation"', notes)
        self.assertNotIn("我容易忘记早前约束。", payload["text"], "自述不能混进系统提示词")

    def test_budget_drops_items_instead_of_truncating(self):
        for index in range(6):
            self.seed_self(f"自我条目 {index}", revision=index + 1)
        payload = core.compile_core(self.db, {"enabled": True, "max_chars": 260,
                                              "max_self_configured": 20})
        self.assertLessEqual(len(payload["text"]), 260)
        self.assertLess(payload["stats"]["self_configured"], 6)
        self.assertIn("</long_term_memory>", payload["text"], "标签必须完整")

    def test_version_stable_and_changes(self):
        self.seed_self("身份一")
        first = core.compile_core(self.db)
        second = core.compile_core(self.db)
        self.assertEqual(first["version"], second["version"])

        self.seed_self("身份二")
        third = core.compile_core(self.db)
        self.assertNotEqual(first["version"], third["version"])

    def test_disabled_returns_empty(self):
        self.seed_self("身份")
        payload = core.compile_core(self.db, {"enabled": False})
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["text"], "")
        self.assertEqual(payload["notes_text"], "")

    def test_escapes_markup_in_content(self):
        self.seed_self("身份含 <tag> 与 a & b")
        text = core.compile_core(self.db)["text"]
        self.assertIn("&lt;tag&gt;", text)
        self.assertIn("a &amp; b", text)
        self.assertNotIn("身份含 <tag>", text)

    def test_old_high_importance_not_squeezed_out(self):
        '''
        窗口效应：先取最近 N 条再筛重要性，会让一条更早的高优先级认知
        被一堆新的低优先级条目挤出核心提示词。
        '''
        self.seed_cognition("这是很早但很重要的一条约束", importance=5)
        for index in range(501):
            self.seed_cognition(f"新写的低优先级条目 {index}", importance=3)

        payload = core.compile_core(self.db, {"enabled": True, "max_chars": 4000,
                                              "max_cognitions": 4})
        self.assertIn("这是很早但很重要的一条约束", payload["text"])
        self.assertLessEqual(payload["stats"]["cognition"], 4)


class TestRoute(CoreCase):
    def test_core_get_frame(self):
        self.seed_self("身份")
        q = base.message_queue()
        core.get_core({"request_id": "r1", "session_id": S1}, q, self.db)
        frame = json.loads(q.recv())
        self.assertEqual(frame["type"], "core")
        self.assertTrue(frame["data"]["enabled"])
        self.assertIn("<self_configured>", frame["data"]["text"])


class TestCoreGetProtocolFrame(CoreCase):
    '''
    走**真实协议帧**：业务参数在帧的 `data` 里，`session_id` 在信封顶层。

    外壳发的是 `kernel.request('core.get', {query_hint, recall, advance_turn},
    {sessionId})`，帧形如
    `{type, request_id, session_id, data: {query_hint, recall, advance_turn}}`。
    只测 `compile_core()` 会绕过这一层，参数丢失也看不出来（0.5.3 的教训）。
    '''

    def setUp(self):
        super().setUp()
        core._recall_state.clear()

    def tearDown(self):
        core._recall_state.clear()
        super().tearDown()

    def _with_recall(self):
        current = config.get_config()
        recall = {**current.get("recall", {}), "min_score": 0.0}
        return mock.patch.object(config, "get_config",
                                 return_value={**current, "recall": recall})

    def _seed_recallable(self):
        item = self.seed_cognition("装插件之前先说明会改哪些文件")
        self.db.index_fts("cognition", item, "项目", "装插件之前先说明会改哪些文件")
        return item

    def _call(self, **data):
        '''按外壳的方式组帧（参数在 data 层、session 在信封），返回应答 data。'''
        q = base.message_queue()
        core.get_core({
            "type": "core.get",
            "request_id": "r_frame",
            "session_id": S1,
            "data": data,
        }, q, self.db)
        return json.loads(q.recv())["data"]

    def test_frame_flags_are_honored(self):
        self._seed_recallable()
        with self._with_recall():
            payload = self._call(query_hint="装插件之前先说明",
                                 recall=False, advance_turn=False)
        self.assertTrue(payload["recall_skipped"],
                        "recall=false 必须被内核读到")
        self.assertEqual(payload["recall_text"], "")

    def test_frame_query_hint_drives_recall(self):
        self._seed_recallable()
        with self._with_recall():
            payload = self._call(query_hint="装插件之前先说明",
                                 recall=True, advance_turn=True)
        self.assertGreaterEqual(len(payload["recall_items"]), 1,
                                "query_hint 必须被内核读到")

    def test_frame_background_refresh_does_not_consume_cooldown(self):
        '''后台帧（recall=false, advance_turn=false）不能推进冷却。'''
        self._seed_recallable()
        with self._with_recall():
            first = self._call(query_hint="装插件之前先说明",
                               recall=True, advance_turn=True)
            self.assertGreaterEqual(len(first["recall_items"]), 1)
            for _ in range(9):
                self._call(recall=False, advance_turn=False)
            second = self._call(query_hint="装插件之前先说明",
                                recall=True, advance_turn=True)
        self.assertEqual(second["recall_items"], [],
                         "冷却按用户步骤算，后台帧不该把它耗光")

    def test_envelope_session_still_works(self):
        '''外壳只给信封 session_id、data 里没有时，仍要能绑定会话。'''
        self._seed_recallable()
        q = base.message_queue()
        core.get_core({"type": "core.get", "request_id": "r_e",
                       "session_id": S1, "data": {}}, q, self.db)
        frame = json.loads(q.recv())
        self.assertTrue(frame["data"]["enabled"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""核心记忆编译（src/core.py）单测：标签、预算、版本、自述快照。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import base                                       # noqa: E402
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


class TestRoute(CoreCase):
    def test_core_get_frame(self):
        self.seed_self("身份")
        q = base.message_queue()
        core.get_core({"request_id": "r1", "session_id": S1}, q, self.db)
        frame = json.loads(q.recv())
        self.assertEqual(frame["type"], "core")
        self.assertTrue(frame["data"]["enabled"])
        self.assertIn("<self_configured>", frame["data"]["text"])


if __name__ == "__main__":
    unittest.main()

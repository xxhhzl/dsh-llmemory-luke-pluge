# -*- coding: utf-8 -*-
"""批3 回归：工具契约（参数校验 / 范围 / 上下文透传 / 错误码 / 配置生效）。"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import config
import tools
from database import Cognition, Database, Experience

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_contract"
S1 = "s_contract"


def iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class ContractCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.db = Database(self.db_path)
        self._orig_db = tools._db
        tools._db = lambda: Database(self.db_path)
        self._config_backup = copy.deepcopy(config.get_config())

    def tearDown(self):
        tools._db = self._orig_db
        config._config = copy.deepcopy(self._config_backup)
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    # ---- helpers ----
    def envelope(self, **extra):
        return {"type": "tools.calls", "request_id": "r1",
                "session_id": S1, "agent_id": "a1", **extra}

    def call(self, calls, **extra):
        q = base.message_queue()
        tools.tools_call({**self.envelope(**extra), "data": {"calls": calls}}, q)
        frame = json.loads(q.recv())
        self.assertEqual(frame["type"], "tools.result")
        return frame["data"]

    def add_cognition(self, content, *, project=None):
        item = self.db.insert_cognition(Cognition(
            subject="用户", content=content, kind="preference",
            origin_project=project, source_event_id=1, created_at=iso(1)))
        self.db.index_fts("cognition", item, "", content)
        return item


class TestArgumentValidation(ContractCase):
    """schema 是给模型看的，执行时必须真校验。"""

    def test_illegal_scope_rejected(self):
        result = self.call([{"call_id": "c1", "name": "memory_search",
                             "arguments": {"query": "x", "scope": "session"}}])[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], -32602)

    def test_illegal_kind_rejected(self):
        result = self.call([{"call_id": "c1", "name": "memory_search",
                             "arguments": {"query": "x", "kind": "memory"}}])[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], -32602)

    def test_memory_get_illegal_kind_rejected(self):
        result = self.call([{"call_id": "c1", "name": "memory_get",
                             "arguments": {"id": 1, "kind": "nope"}}])[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], -32602)

    def test_limit_out_of_schema_range_rejected(self):
        result = self.call([{"call_id": "c1", "name": "memory_search",
                             "arguments": {"query": "x", "limit": 500}}])[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], -32602)


class TestLimitClamp(ContractCase):
    def test_limit_clamped_to_config(self):
        config.patch_config({"search": {"limit_max": 3}})
        self.add_cognition("配色 一")
        out = self.call([{"call_id": "c1", "name": "memory_search",
                          "arguments": {"query": "配色", "limit": 50}}])[0]
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["limit"], 3)


class TestNotFound(ContractCase):
    def test_memory_get_missing_returns_1003(self):
        out = self.call([{"call_id": "c1", "name": "memory_get",
                          "arguments": {"id": 99999}}])[0]
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], 1003)


class TestScopePassthrough(ContractCase):
    """范围是数据属性：信封里的 origin_project 决定项目的可见集。"""

    def test_envelope_origin_project_filters(self):
        mine = self.add_cognition("项目内 配色规范", project="proj_a")
        other = self.add_cognition("别的项目 配色规范", project="proj_b")
        out = self.call([{"call_id": "c1", "name": "memory_search",
                          "arguments": {"query": "配色", "scope": "project"}}],
                        origin_project="proj_a")[0]
        ids = {item["id"] for item in out["data"]["items"] if item["kind"] == "cognition"}
        self.assertEqual(ids, {mine})

    def test_all_scope_ignores_project(self):
        a = self.add_cognition("项目 A 配色", project="proj_a")
        b = self.add_cognition("项目 B 配色", project="proj_b")
        out = self.call([{"call_id": "c1", "name": "memory_search",
                          "arguments": {"query": "配色", "scope": "all"}}],
                        origin_project="proj_a")[0]
        ids = {item["id"] for item in out["data"]["items"] if item["kind"] == "cognition"}
        self.assertEqual(ids, {a, b})

    def test_default_scope_comes_from_config(self):
        """config.search.default_scope 不再是死配置。"""
        config.patch_config({"search": {"default_scope": "project"}})
        mine = self.add_cognition("配色 A", project="proj_a")
        self.add_cognition("配色 B", project="proj_b")
        out = self.call([{"call_id": "c1", "name": "memory_search",
                          "arguments": {"query": "配色"}}], origin_project="proj_a")[0]
        self.assertEqual(out["data"]["scope"], "project")
        ids = {item["id"] for item in out["data"]["items"] if item["kind"] == "cognition"}
        self.assertEqual(ids, {mine})


class TestMinScore(ContractCase):
    def test_min_score_filters(self):
        self.add_cognition("配色规范")
        out = self.call([{"call_id": "c1", "name": "memory_search",
                          "arguments": {"query": "配色", "min_score": 0.99}}])[0]
        self.assertEqual(out["data"]["count"], 0)

    def test_config_min_score_is_used(self):
        config.patch_config({"search": {"min_score": 0.99}})
        self.add_cognition("配色规范")
        out = self.call([{"call_id": "c1", "name": "memory_search",
                          "arguments": {"query": "配色"}}])[0]
        self.assertEqual(out["data"]["count"], 0)


class TestRecentSessionDefault(ContractCase):
    def test_session_id_falls_back_to_envelope(self):
        from database import Memory, Message
        event = self.db.create_event(S1, "边框", summary="1px")
        self.db.insert_message(Message(session_id=S1, message_id="m1", owner="a",
                                       seq=0, role="user", content="x",
                                       occur_at="2026-11-03T02:00:00+00:00",
                                       created_at="2026-11-03T02:00:00+00:00"))
        self.db.insert_memory(Memory(session_id=S1, message_id="m1", owner="a",
                                     seq=0, occur_at="2026-11-03T02:00:00+00:00",
                                     created_at="2026-11-03T02:00:00+00:00"))
        self.db.bind_event(S1, "m1", event.id, strict=True)
        out = self.call([{"call_id": "c1", "name": "memory_recent",
                          "arguments": {"from_at": "2026-11-03T00:00:00+00:00",
                                        "to_at": "2026-11-03T03:00:00+00:00"}}])[0]
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["count"], 1)


class TestDeadConfigGone(ContractCase):
    """已删除的死配置不能再被 patch 进来（否则又是「设了不生效」）。"""

    def test_removed_keys_are_unknown(self):
        for patch in ({"jev": {"accept_confidence": 0.5}},
                      {"split": {"mark_end": True}}):
            with self.assertRaises(ValueError):
                config.patch_config(patch)


if __name__ == "__main__":
    unittest.main()

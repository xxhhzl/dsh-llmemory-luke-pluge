# -*- coding: utf-8 -*-
"""管理 API（src/admin.py）：表描述、增删查改与索引同步。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import admin
import base
from database import Cognition, Database, Experience, Memory, Message

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_admin"
S1 = "s_admin"


class AdminCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")
        self._orig = admin._db
        admin._db = lambda: self.db

    def tearDown(self):
        admin._db = self._orig
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestDescribe(AdminCase):
    def test_tables_cover_all_stores(self):
        info = admin.describe(self.db)
        names = {item["name"] for item in info["tables"]}
        self.assertEqual(names, {"event", "message", "memory", "experience",
                                 "cognition", "insight", "self_memory", "digest"})
        event = next(item for item in info["tables"] if item["name"] == "event")
        self.assertIn("title", [column["name"] for column in event["columns"]])
        self.assertIn("title", event["writable"])


class TestRead(AdminCase):
    def test_list_with_query_and_paging(self):
        for index in range(5):
            self.db.insert_experience(Experience(
                content=f"配色经验 {index}", source_event_id=1,
                created_at="2026-11-03T10:00:00+00:00"))
        page = admin.list_rows(self.db, table="experience", limit=2)
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["rows"]), 2)

        found = admin.list_rows(self.db, table="experience", query="配色经验 3")
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["rows"][0]["content"], "配色经验 3")

    def test_get_by_primary_key(self):
        item = self.db.insert_experience(Experience(
            content="取一行", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        row = admin.get_row(self.db, table="experience", row_id=item)
        self.assertEqual(row["content"], "取一行")

    def test_unknown_table_rejected(self):
        with self.assertRaises(admin.AdminError):
            admin.list_rows(self.db, table="sqlite_master")


class TestWrite(AdminCase):
    def test_insert_cognition_and_index(self):
        out = admin.insert_row(self.db, table="cognition", values={
            "subject": "用户", "content": "偏好中文回复", "kind": "preference",
            "importance": 4, "cues": ["偏好", "中文"],
        })
        self.assertTrue(out["id"])
        row = admin.get_row(self.db, table="cognition", row_id=out["id"])
        self.assertEqual(row["content"], "偏好中文回复")
        self.assertEqual(row["cues"], '["偏好", "中文"]')
        # 写入后应能被全文索引召回
        self.assertTrue(self.db.search_fts("偏好中文"))

    def test_update_text_syncs_fts(self):
        item = self.db.insert_experience(Experience(
            content="旧内容", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        self.db.index_fts("experience", item, "", "旧内容")
        admin.update_row(self.db, table="experience", row_id=item,
                         values={"content": "新内容"})
        self.assertEqual(self.db.get_experience(item).content, "新内容")
        self.assertEqual(self.db.search_fts("旧内容"), [])
        self.assertTrue(self.db.search_fts("新内容"))

    def test_update_rejects_unknown_column(self):
        item = self.db.insert_experience(Experience(
            content="x", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        with self.assertRaises(admin.AdminError) as ctx:
            admin.update_row(self.db, table="experience", row_id=item,
                             values={"id": 999})
        self.assertIn("没有可更新的字段", str(ctx.exception))
        # 拼进 SQL 的列名也必须被白名单挡掉（不是转义，而是直接不认）
        with self.assertRaises(admin.AdminError):
            admin.update_row(self.db, table="experience", row_id=item,
                             values={"content = 'x', status": "active"})
        self.assertEqual(self.db.get_experience(item).content, "x", "数据不该被改动")

    def test_digest_insert_uses_upsert(self):
        first = admin.insert_row(self.db, table="digest",
                                 values={"session_id": S1, "content": "第一版"})
        second = admin.insert_row(self.db, table="digest",
                                  values={"session_id": S1, "content": "第二版"})
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(self.db.get_digest(S1).content, "第二版")


class TestWritePayloads(AdminCase):
    '''回归：设置页「新增」提交的是“全部可写列 + 空串”，不能因此撞 NOT NULL。'''

    def _ui_payload(self, table, **overrides):
        values = {name: '' for name in admin.TABLES[table][1]}
        values.update(overrides)
        return values

    def test_ui_payload_with_empty_strings_succeeds(self):
        for table, payload in (
            ("experience", {"content": "UI 形状的经验"}),
            ("cognition", {"subject": "用户", "content": "UI 形状的认知"}),
        ):
            values = self._ui_payload(table, **payload)
            out = admin.insert_row(self.db, table=table, values=values)
            self.assertTrue(out["id"], f"{table} 应写入成功")
            row = admin.get_row(self.db, table=table, row_id=out["id"])
            self.assertEqual(row["content"], payload["content"])
            # 空串列应该落成默认值，而不是 NULL
            self.assertEqual(row["importance"], 3)
            self.assertAlmostEqual(row["confidence"], 0.8)
            self.assertEqual(row["source_event_id"], 0)

    def test_missing_required_column_reports_readable_error(self):
        values = self._ui_payload("experience")      # content 也是空
        with self.assertRaises(admin.AdminError) as ctx:
            admin.insert_row(self.db, table="experience", values=values)
        self.assertIn("content", str(ctx.exception))
        self.assertIn("必填", str(ctx.exception))

    def test_message_without_occur_at_reports_readable_error(self):
        values = self._ui_payload("message", session_id=S1, message_id="m9",
                                  owner="a", seq=9, role="user", content="hello")
        with self.assertRaises(admin.AdminError) as ctx:
            admin.insert_row(self.db, table="message", values=values)
        self.assertIn("occur_at", str(ctx.exception))

    def test_duplicate_seq_reports_unique_conflict(self):
        self.db.insert_message(Message(session_id=S1, message_id="m1", owner="a",
                                       seq=0, role="user", content="x",
                                       occur_at="2026-11-03T00:00:00+00:00",
                                       created_at="2026-11-03T00:00:00+00:00"))
        values = self._ui_payload("message", session_id=S1, message_id="m2",
                                  owner="a", seq=0, role="user", content="y",
                                  occur_at="2026-11-03T00:00:01+00:00")
        with self.assertRaises(admin.AdminError) as ctx:
            admin.insert_row(self.db, table="message", values=values)
        self.assertIn("唯一约束", str(ctx.exception))


class TestKeyColumns(AdminCase):
    '''回归：编辑不能改主键 / seq；digest 保存不能丢溯源与 revision。'''

    def test_describe_marks_key_columns_unupdatable(self):
        info = admin.describe(self.db)
        by_name = {item["name"]: item for item in info["tables"]}
        self.assertNotIn("message_id", by_name["message"]["updatable"])
        self.assertNotIn("seq", by_name["message"]["updatable"])
        self.assertNotIn("session_id", by_name["digest"]["updatable"])
        self.assertIn("message_id", by_name["message"]["writable"])
        self.assertIn("content", by_name["experience"]["updatable"])

    def test_update_ignores_key_columns(self):
        item = self.db.insert_experience(Experience(
            content="原文", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        with self.assertRaises(admin.AdminError):
            admin.update_row(self.db, table="experience", row_id=item,
                             values={"id": 999})
        admin.update_row(self.db, table="experience", row_id=item,
                         values={"content": "改过", "origin_project": "p1"})
        row = admin.get_row(self.db, table="experience", row_id=item)
        self.assertEqual(row["content"], "改过")
        self.assertEqual(row["id"], item, "主键不应变")

    def test_update_message_cannot_rekey(self):
        self.db.insert_message(Message(session_id=S1, message_id="m1", owner="a",
                                       seq=0, role="user", content="x",
                                       occur_at="2026-11-03T00:00:00+00:00",
                                       created_at="2026-11-03T00:00:00+00:00"))
        row_id = self.db._conn.execute(
            "SELECT rowid FROM message WHERE message_id='m1'").fetchone()["rowid"]
        with self.assertRaises(admin.AdminError):
            admin.update_row(self.db, table="message", row_id=row_id,
                             values={"message_id": "m2", "seq": 5})
        self.assertIsNotNone(self.db.get_message(S1, "m1"))

    def test_digest_update_keeps_refs_and_bumps_revision(self):
        revision = self.db.upsert_digest(S1, "第一版", ref_event_ids=[3, 7])
        digest = self.db.get_digest(S1)
        self.assertEqual(digest.ref_event_ids, (3, 7))
        out = admin.update_row(self.db, table="digest", row_id=S1,
                               values={"content": "第二版"})
        self.assertEqual(out["revision"], revision + 1)
        after = self.db.get_digest(S1)
        self.assertEqual(after.content, "第二版")
        self.assertEqual(after.ref_event_ids, (3, 7), "溯源不能被保存动作清空")

    def test_digest_insert_keeps_existing_refs(self):
        self.db.upsert_digest(S1, "旧", ref_event_ids=[11, 12])
        admin.insert_row(self.db, table="digest",
                         values={"session_id": S1, "content": "新"})
        self.assertEqual(self.db.get_digest(S1).ref_event_ids, (11, 12))


class TestDeleteReferences(AdminCase):
    '''回归：删事件要把 l3 锚点、digest 溯源、related_events 一起收干净。'''

    def test_delete_event_clears_every_reference(self):
        event = self.db.create_event(S1, "待删事件", summary="摘要")
        other = self.db.create_event(S1, "留存事件", summary="另一段",
                                     related_events=[event.id])
        exp = self.db.insert_experience(Experience(
            content="从事件学到的", source_event_id=event.id,
            created_at="2026-11-03T10:00:00+00:00"))
        cog = self.db.insert_cognition(Cognition(
            subject="用户", content="从事件推出的", source_event_id=event.id,
            created_at="2026-11-03T10:00:00+00:00"))
        self.db.upsert_digest(S1, "工作记忆", ref_event_ids=[event.id, other.id])
        self.db.index_fts("event", event.id, "待删事件", "摘要")

        admin.delete_row(self.db, table="event", row_id=event.id)

        self.assertEqual(self.db.get_experience(exp).source_event_id, 0,
                         "l3 锚点应归零，不能悬空")
        self.assertEqual(self.db.get_cognition(cog).source_event_id, 0)
        self.assertEqual(self.db.get_digest(S1).ref_event_ids, (other.id,),
                         "digest 里只该留下别的事件")
        self.assertEqual(self.db.get_event(other.id).related_events, (),
                         "别的事件的 related_events 也要剔除")
        self.assertEqual(self.db.search_fts("待删事件"), [],
                         "事件索引应删除")

    def test_delete_missing_event_leaves_references_alone(self):
        exp = self.db.insert_experience(Experience(
            content="保留", source_event_id=42,
            created_at="2026-11-03T10:00:00+00:00"))
        with self.assertRaises(admin.AdminError):
            admin.delete_row(self.db, table="event", row_id=999)
        self.assertEqual(self.db.get_experience(exp).source_event_id, 42)


class TestListShape(AdminCase):
    """列表页不能把整篇正文回给前端；搜索要按字面匹配。"""

    def _long_message(self, content: str) -> None:
        self.db.insert_message(Message(session_id=S1, message_id="m-long", owner="a",
                                       seq=0, role="user", content=content,
                                       occur_at="2026-11-03T00:00:00+00:00",
                                       created_at="2026-11-03T00:00:00+00:00"))

    def test_list_clips_long_text_but_get_returns_full(self):
        content = "长" * 5000
        self._long_message(content)
        page = admin.list_rows(self.db, table="message", limit=10)
        self.assertEqual(len(page["rows"][0]["content"]), 200, "列表只回前 200 字")
        row_id = page["rows"][0]["_rowid"]
        full = admin.get_row(self.db, table="message", row_id=row_id)
        self.assertEqual(len(full["content"]), 5000, "编辑前取整行")

    def test_search_escapes_like_wildcards(self):
        self._long_message("进度 50% 完成")
        self.db.insert_message(Message(session_id=S1, message_id="m2", owner="a",
                                       seq=1, role="user", content="进度 60 完成",
                                       occur_at="2026-11-03T00:00:01+00:00",
                                       created_at="2026-11-03T00:00:01+00:00"))
        found = admin.list_rows(self.db, table="message", query="50%")
        self.assertEqual(found["total"], 1, "% 应作为字面量匹配")
        self.assertEqual(found["rows"][0]["message_id"], "m-long")
        # 只搜一个 % ：不转义会匹配全部（LIKE '%%%'），转义后只命中含 % 的那条
        self.assertEqual(admin.list_rows(self.db, table="message", query="%")["total"], 1)

    def test_review_reason_is_searchable(self):
        event = self.db.create_event(S1, "待复核事件", summary="摘要")
        self.db.set_event_review(event.id, True, "summary_failed")
        hit = admin.list_rows(self.db, table="event", query="summary_failed")
        self.assertEqual(hit["total"], 1)


class TestInvalidationDropsIndex(AdminCase):
    def test_set_invalid_removes_fts_row(self):
        item = self.db.insert_experience(Experience(
            content="会被置为失效", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        self.db.index_fts("experience", item, "", "会被置为失效")
        self.assertTrue(self.db.search_fts("会被置为失效"))
        admin.update_row(self.db, table="experience", row_id=item,
                         values={"status": "invalid"})
        self.assertEqual(self.db.search_fts("会被置为失效"), [],
                         "失效条目应从索引摘掉")

    def test_reactivate_restores_index(self):
        item = self.db.insert_experience(Experience(
            content="先失效再启用", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        admin.update_row(self.db, table="experience", row_id=item,
                         values={"status": "invalid"})
        admin.update_row(self.db, table="experience", row_id=item,
                         values={"status": "active"})
        self.assertTrue(self.db.search_fts("先失效再启用"), "重新生效应重建索引")


class TestConnectionReuse(unittest.TestCase):
    """admin._db 必须委托给 tools 的连接缓存（用例不继承 AdminCase，避免被替换）。"""

    def test_db_delegates_to_tools_cache(self):
        import tools
        sentinel = object()
        original = tools._db
        tools._db = lambda: sentinel
        try:
            self.assertIs(admin._db(), sentinel)
        finally:
            tools._db = original


class TestDelete(AdminCase):
    def test_delete_experience_cleans_fts(self):
        item = self.db.insert_experience(Experience(
            content="待删除", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        self.db.index_fts("experience", item, "", "待删除")
        admin.delete_row(self.db, table="experience", row_id=item)
        self.assertIsNone(self.db.get_experience(item))
        self.assertEqual(self.db.search_fts("待删除"), [])

    def test_delete_event_unbinds_messages(self):
        event = self.db.create_event(S1, "待删事件", summary="摘要")
        self.db.insert_message(Message(session_id=S1, message_id="m1", owner="a",
                                       seq=0, role="user", content="x",
                                       occur_at="2026-11-03T00:00:00+00:00",
                                       created_at="2026-11-03T00:00:00+00:00"))
        self.db.insert_memory(Memory(session_id=S1, message_id="m1", owner="a",
                                     seq=0, occur_at="2026-11-03T00:00:00+00:00",
                                     created_at="2026-11-03T00:00:00+00:00"))
        self.db.bind_event(S1, "m1", event.id, strict=True)
        admin.delete_row(self.db, table="event", row_id=event.id)
        self.assertIsNone(self.db.get_event(event.id))
        self.assertIsNone(self.db.bound_event_of(S1, "m1"), "消息应被解绑")
        self.assertEqual(self.db.get_message(S1, "m1").content, "x", "正文保留")

    def test_delete_message_removes_reference(self):
        self.db.insert_message(Message(session_id=S1, message_id="m2", owner="a",
                                       seq=1, role="user", content="y",
                                       occur_at="2026-11-03T00:00:01+00:00",
                                       created_at="2026-11-03T00:00:01+00:00"))
        self.db.insert_memory(Memory(session_id=S1, message_id="m2", owner="a",
                                     seq=1, occur_at="2026-11-03T00:00:01+00:00",
                                     created_at="2026-11-03T00:00:01+00:00"))
        row_id = self.db._conn.execute(
            "SELECT rowid FROM message WHERE message_id = 'm2'").fetchone()["rowid"]
        admin.delete_row(self.db, table="message", row_id=row_id)
        self.assertIsNone(self.db.get_message(S1, "m2"))
        self.assertIsNone(self.db.get_memory(S1, "m2"), "引用应一并删除")


class TestSelf(AdminCase):
    '''用户写「设置的自我」：按 kind 取代、revision 递增、只接受 configured。'''

    def test_set_creates_and_supersedes_same_kind(self):
        first = admin.self_set(self.db, {"kind": "identity", "content": "我是 A"})
        second = admin.self_set(self.db, {"kind": "identity", "content": "我是 B"})
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        items = admin.self_list(self.db, {"source": "configured"})["items"]
        active = [item for item in items if item["status"] == "active"]
        self.assertEqual([item["content"] for item in active], ["我是 B"])
        old = self.db.get_self_memory(first["id"])
        self.assertEqual(old.status, "superseded")
        self.assertEqual(old.superseded_id, second["id"])

    def test_different_kind_keeps_both(self):
        admin.self_set(self.db, {"kind": "identity", "content": "我是 A"})
        admin.self_set(self.db, {"kind": "limitation", "content": "只读"})
        active = [item for item in admin.self_list(self.db, {})["items"]
                  if item["status"] == "active"]
        self.assertEqual(len(active), 2)

    def test_rejects_agent_source(self):
        with self.assertRaises(admin.AdminError):
            admin.self_set(self.db, {"source": "agent", "content": "x"})

    def test_validates_kind_and_length(self):
        with self.assertRaises(admin.AdminError):
            admin.self_set(self.db, {"kind": "unknown", "content": "x"})
        with self.assertRaises(admin.AdminError):
            admin.self_set(self.db, {"kind": "identity", "content": "x" * 501})
        with self.assertRaises(admin.AdminError):
            admin.self_set(self.db, {"kind": "identity", "content": "   "})

    def test_disable_marks_invalid(self):
        created = admin.self_set(self.db, {"kind": "identity", "content": "我是 A"})
        admin.self_disable(self.db, {"id": created["id"], "reason": "改口"})
        row = self.db.get_self_memory(created["id"])
        self.assertEqual(row.status, "invalid")
        self.assertEqual(row.invalid_reason, "改口")
        self.assertEqual(admin.self_list(self.db, {})["items"], [])

    def test_set_pushes_core_notify(self):
        q = base.message_queue()
        admin.admin({"type": "admin", "request_id": "r1", "session_id": S1,
                     "agent_id": "a1",
                     "data": {"op": "self.set", "kind": "identity",
                              "content": "我是 A"}}, q)
        first = json.loads(q.recv())
        second = json.loads(q.recv())
        self.assertEqual(first["type"], "admin")
        self.assertTrue(first["data"]["ok"])
        self.assertEqual(second["type"], "notify")
        self.assertEqual(second["data"]["kind"], "core")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""存储层单元测试：表结构、幂等写入、事件链、区间查询、事务回滚。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import database
from database import Cognition, Database, Event, Experience, Memory, Message

S1 = "s_20261020_1"
S2 = "s_20261021_2"

# 临时库放在工作区 .tmp 下：系统临时目录与 mkdtemp 子目录在沙箱里不可写
BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_db"


def make_message(message_id: str, seq: int, occur_at: str,
                 session_id: str = S1, role: str = "user",
                 content: str | None = None, owner: str = "lead#0") -> Message:
    return Message(
        session_id=session_id,
        message_id=message_id,
        owner=owner,
        seq=seq,
        role=role,
        content=content if content is not None else f"内容 {message_id}",
        occur_at=occur_at,
        created_at=occur_at,
    )


def make_memory(message_id: str, seq: int, occur_at: str,
                session_id: str = S1, owner: str = "lead#0") -> Memory:
    return Memory(
        session_id=session_id,
        message_id=message_id,
        owner=owner,
        seq=seq,
        occur_at=occur_at,
        created_at=occur_at,
    )


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        # 不用 tempfile.mkdtemp：沙箱下它创建的子目录不可写，改用自建唯一目录
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "sub" / "memory.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def seed_event(self, session_id: str, title: str, base_seq: int,
                   occur_at: str) -> Event:
        '''造一个事件：消息 + 记忆 + 绑定。'''
        msg = make_message(f"m_{base_seq}", base_seq, occur_at, session_id=session_id)
        self.db.insert_message(msg)
        self.db.insert_memory(make_memory(f"m_{base_seq}", base_seq, occur_at,
                                          session_id=session_id))
        event = self.db.create_event(session_id, title)
        self.db.bind_event(session_id, f"m_{base_seq}", event.id)
        return event


class TestSchema(DatabaseCase):
    def test_tables_created(self):
        names = {row["name"] for row in self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"memory", "message", "event"} <= names)

    def test_columns_match_doc(self):
        def columns(table):
            return [row["name"] for row in self.db._conn.execute(
                f"PRAGMA table_info({table})")]

        self.assertEqual(columns("memory"), [
            "session_id", "message_id", "owner", "seq", "occur_at", "created_at", "event_id"])
        self.assertEqual(columns("message"), [
            "session_id", "message_id", "owner", "seq", "role", "content",
            "occur_at", "created_at"])
        self.assertEqual(columns("event"), [
            "id", "session_id", "title", "summary", "created_at",
            "prev_event_id", "next_event_id", "related_events", "cnt",
            "origin_project", "summarized_at", "needs_review", "review_reason",
            "last_used_at"])

    def test_wal_mode(self):
        row = self.db._conn.execute("PRAGMA journal_mode").fetchone()
        self.assertEqual(row[0].lower(), "wal")

    def test_parent_directory_is_created(self):
        self.assertTrue(self.db_path.parent.exists())


class TestMessages(DatabaseCase):
    def test_insert_is_idempotent(self):
        msg = make_message("m_1", 0, "2026-10-20T10:00:00+08:00")
        self.assertTrue(self.db.insert_message(msg))
        self.assertFalse(self.db.insert_message(msg))
        self.assertEqual(self.db.count_messages(S1), 1)

    def test_get_and_list(self):
        self.db.insert_message(make_message("m_1", 0, "2026-10-20T10:00:00+08:00"))
        self.db.insert_message(make_message("m_2", 1, "2026-10-20T10:01:00+08:00"))
        self.db.insert_message(make_message("m_3", 2, "2026-10-20T10:02:00+08:00"))
        self.assertEqual(self.db.get_message(S1, "m_2").seq, 1)
        self.assertEqual([m.message_id for m in self.db.list_messages(S1)],
                         ["m_1", "m_2", "m_3"])
        # after_seq 是「严格大于」的游标语义
        self.assertEqual([m.message_id for m in self.db.list_messages(S1, after_seq=0)],
                         ["m_2", "m_3"])
        self.assertEqual([m.message_id for m in self.db.list_messages(S1, after_seq=1)],
                         ["m_3"])
        self.assertEqual(self.db.list_messages(S1, after_seq=2), [])

    def test_batch_insert_counts_new_rows(self):
        batch = [make_message("m_1", 0, "2026-10-20T10:00:00+08:00"),
                 make_message("m_2", 1, "2026-10-20T10:01:00+08:00")]
        self.assertEqual(self.db.insert_messages(batch), (2, []))
        self.assertEqual(self.db.insert_messages(batch), (0, []))

    def test_tool_message_keeps_json_content(self):
        payload = '{"call": {"name": "memory_search"}, "result": {"items": []}}'
        self.db.insert_message(make_message("m_tool", 0, "2026-10-20T10:00:00+08:00",
                                            role="tool", content=payload))
        self.assertEqual(self.db.get_message(S1, "m_tool").content, payload)


class TestMemory(DatabaseCase):
    def test_insert_and_get(self):
        self.assertTrue(self.db.insert_memory(make_memory("m_1", 0, "2026-10-20T10:00:00+08:00")))
        self.assertFalse(self.db.insert_memory(make_memory("m_1", 0, "2026-10-20T10:00:00+08:00")))
        self.assertIsNone(self.db.get_memory(S1, "m_1").event_id)

    def test_bind_event(self):
        self.db.insert_memory(make_memory("m_1", 0, "2026-10-20T10:00:00+08:00"))
        event = self.db.create_event(S1, "第一个事件")
        self.assertTrue(self.db.bind_event(S1, "m_1", event.id))
        self.assertFalse(self.db.bind_event(S1, "m_1", event.id))   # 已归属同一事件
        self.assertEqual(self.db.get_memory(S1, "m_1").event_id, event.id)

    def test_list_by_cursor(self):
        for i in range(3):
            self.db.insert_memory(make_memory(f"m_{i}", i, f"2026-10-20T10:0{i}:00+08:00"))
        self.assertEqual([m.seq for m in self.db.list_memory(S1, after_seq=1)], [2])


class TestEvents(DatabaseCase):
    def test_chain_within_session(self):
        first = self.db.create_event(S1, "事件一", summary="摘要一")
        second = self.db.create_event(S1, "事件二")
        third = self.db.create_event(S1, "事件三")

        self.assertIsNone(first.prev_event_id)
        self.assertEqual(second.prev_event_id, first.id)
        self.assertEqual(third.prev_event_id, second.id)

        reloaded_first = self.db.get_event(first.id)
        self.assertEqual(reloaded_first.next_event_id, second.id)
        self.assertEqual(self.db.get_event(second.id).next_event_id, third.id)
        self.assertEqual(self.db.get_event(third.id).summary, None)

    def test_chain_is_per_session(self):
        a = self.db.create_event(S1, "会话一的事件")
        b = self.db.create_event(S2, "会话二的事件")
        self.assertIsNone(b.prev_event_id)
        self.assertEqual(a.session_id, S1)
        self.assertEqual(self.db.get_event(a.id).next_event_id, None)

    def test_related_events_roundtrip(self):
        one = self.db.create_event(S1, "一")
        two = self.db.create_event(S1, "二", related_events=[one.id])
        self.assertEqual(self.db.get_event(two.id).related_events, (one.id,))
        self.assertEqual(self.db.get_event(one.id).related_events, ())

    def test_list_events_order(self):
        ids = [self.db.create_event(S1, f"事件{i}").id for i in range(3)]
        self.assertEqual([e.id for e in self.db.list_events(S1)], list(reversed(ids)))
        self.assertEqual([e.id for e in self.db.list_events(S1, descending=False)], ids)

    def test_set_event_summary(self):
        event = self.db.create_event(S1, "标题")
        self.assertTrue(self.db.set_event_summary(event.id, "补充的摘要"))
        self.assertEqual(self.db.get_event(event.id).summary, "补充的摘要")
        self.assertFalse(self.db.set_event_summary(99999, "不存在"))


class TestEventCnt(DatabaseCase):
    '''事件使用计数 cnt：新建为 0，每使用/召回一次 +1，可按热度排序。'''

    def test_new_event_starts_at_zero(self):
        event = self.db.create_event(S1, "新事件")
        self.assertEqual(event.cnt, 0)
        self.assertEqual(self.db.get_event(event.id).cnt, 0)

    def test_bump_increases_and_returns_new_value(self):
        event = self.db.create_event(S1, "事件")
        self.assertEqual(self.db.bump_event_cnt(event.id), 1)
        self.assertEqual(self.db.bump_event_cnt(event.id), 2)
        self.assertEqual(self.db.get_event(event.id).cnt, 2)

    def test_bump_by_n(self):
        event = self.db.create_event(S1, "事件")
        self.assertEqual(self.db.bump_event_cnt(event.id, by=5), 5)
        self.assertEqual(self.db.bump_event_cnt(event.id), 6)

    def test_bump_events_batch(self):
        a = self.db.create_event(S1, "A")
        b = self.db.create_event(S1, "B")
        self.assertEqual(self.db.bump_events([a.id, b.id]), 2)
        self.assertEqual(self.db.bump_events([a.id, 99999]), 1)   # 不存在的不计
        self.assertEqual(self.db.get_event(a.id).cnt, 2)
        self.assertEqual(self.db.get_event(b.id).cnt, 1)
        self.assertEqual(self.db.bump_events([]), 0)

    def test_bump_missing_event_returns_none(self):
        self.assertIsNone(self.db.bump_event_cnt(99999))

    def test_bump_rejects_non_positive(self):
        event = self.db.create_event(S1, "事件")
        with self.assertRaises(ValueError):
            self.db.bump_event_cnt(event.id, by=0)

    def test_list_events_order_by_cnt(self):
        low = self.db.create_event(S1, "少用")
        high = self.db.create_event(S1, "常用")
        self.db.bump_event_cnt(low.id, by=1)
        self.db.bump_event_cnt(high.id, by=3)
        self.assertEqual(
            [e.id for e in self.db.list_events(S1, order_by="cnt")], [high.id, low.id])
        self.assertEqual(
            [e.id for e in self.db.list_events(S1, order_by="cnt", descending=False)],
            [low.id, high.id])
        # 默认仍按 id 倒序（时间序），不受 cnt 影响
        self.assertEqual([e.id for e in self.db.list_events(S1)], [high.id, low.id])

    def test_list_events_rejects_unknown_order(self):
        with self.assertRaises(ValueError):
            self.db.list_events(S1, order_by="title")

    def test_migration_adds_cnt_to_old_table(self):
        old = self._dir / "old.db"
        conn = sqlite3.connect(old)
        conn.executescript(
            "CREATE TABLE event ("
            "  id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,"
            "  summary TEXT, created_at TEXT NOT NULL, prev_event_id INTEGER,"
            "  next_event_id INTEGER, related_events TEXT);"
            "INSERT INTO event (session_id, title, created_at)"
            "  VALUES ('s_old', '旧事件', '2026-10-20T10:00:00+08:00');")
        conn.commit()
        conn.close()

        db = Database(old)
        try:
            columns = [row["name"] for row in db._conn.execute("PRAGMA table_info(event)")]
            self.assertIn("cnt", columns)
            self.assertEqual(db._conn.execute("SELECT cnt FROM event").fetchone()["cnt"], 0)
            self.assertEqual(db.bump_event_cnt(1), 1)
        finally:
            db.close()


class TestL3(DatabaseCase):
    '''经验（程序记忆）与认知（语义记忆）：写入、取代、失效、召回计数。'''

    def _exp(self, **kw):
        base = dict(context="导出 SVG 前", content="改 strokeWidth 后再导出", kind="technique",
                    confidence=0.9, importance=4, origin_session=S1,
                    origin_project="proj_a", source_event_id=1, cues=("drawio", "svg"))
        base.update(kw)
        return Experience(**base)

    def _cog(self, **kw):
        base = dict(subject="用户", content="偏好中文回复", kind="preference",
                    importance=5, origin_session=S1, origin_project=None,
                    source_event_id=1, cues=("偏好",))
        base.update(kw)
        return Cognition(**base)

    def test_insert_defaults_and_roundtrip(self):
        exp_id = self.db.insert_experience(self._exp())
        exp = self.db.get_experience(exp_id)
        self.assertEqual(exp.content, "改 strokeWidth 后再导出")
        self.assertEqual(exp.status, "active")
        self.assertEqual(exp.use_count, 0)
        self.assertEqual(exp.cues, ("drawio", "svg"))
        self.assertTrue(exp.created_at)

        cog_id = self.db.insert_cognition(self._cog())
        cog = self.db.get_cognition(cog_id)
        self.assertEqual(cog.subject, "用户")
        self.assertEqual(cog.kind, "preference")
        self.assertEqual(cog.origin_project, None)

    def test_list_filters_status_and_origin(self):
        a = self.db.insert_experience(self._exp(origin_project="proj_a"))
        b = self.db.insert_experience(self._exp(origin_project="proj_b"))
        c = self.db.insert_experience(self._exp(origin_project=None))
        ids = {e.id for e in self.db.list_experiences(origin_project="proj_a")}
        self.assertEqual(ids, {a, c})       # 含通用条目
        self.assertIn(b, {e.id for e in self.db.list_experiences(origin_project="proj_b")})
        self.assertEqual({e.id for e in self.db.list_experiences()}, {a, b, c})

    def test_supersede_keeps_old_row(self):
        old = self.db.insert_experience(self._exp(content="用 SQLite"))
        new = self.db.supersede_experience(old, self._exp(content="改用 Postgres"))
        self.assertNotEqual(old, new)
        self.assertEqual(self.db.get_experience(new).superseded_id, old)
        old_row = self.db.get_experience(old)
        self.assertEqual(old_row.status, "superseded")
        self.assertTrue(old_row.valid_to)
        self.assertEqual(self.db.get_experience(old).content, "用 SQLite")   # 内容不变
        self.assertEqual([e.id for e in self.db.list_experiences()], [new])

    def test_supersede_missing_raises(self):
        with self.assertRaises(KeyError):
            self.db.supersede_experience(99999, self._exp())

    def test_invalidate_without_successor(self):
        old = self.db.insert_cognition(self._cog(content="编辑器用 VS Code"))
        self.assertTrue(self.db.invalidate_cognition(old, "用户已卸载 VS Code"))
        row = self.db.get_cognition(old)
        self.assertEqual(row.status, "invalid")
        self.assertEqual(row.invalid_reason, "用户已卸载 VS Code")
        self.assertTrue(row.valid_to)
        self.assertFalse(self.db.invalidate_cognition(old, "重复失效"))
        self.assertEqual(self.db.list_cognitions(), [])

    def test_touch_updates_usage(self):
        a = self.db.insert_experience(self._exp())
        b = self.db.insert_cognition(self._cog())
        self.assertEqual(self.db.touch_experiences([a, a]), 2)
        self.assertEqual(self.db.touch_cognitions([b]), 1)
        self.assertEqual(self.db.get_experience(a).use_count, 2)
        self.assertTrue(self.db.get_experience(a).last_used_at)
        self.assertEqual(self.db.touch_experiences([]), 0)


class TestDigest(DatabaseCase):
    def test_upsert_overwrites_and_bumps_revision(self):
        self.assertEqual(self.db.upsert_digest(S1, "第一版", [1, 2]), 1)
        self.assertEqual(self.db.upsert_digest(S1, "第二版", [2, 3]), 2)
        digest = self.db.get_digest(S1)
        self.assertEqual(digest.content, "第二版")
        self.assertEqual(digest.ref_event_ids, (2, 3))
        self.assertEqual(digest.revision, 2)
        self.assertIsNone(self.db.get_digest(S2))


class TestFts(DatabaseCase):
    def test_index_and_search(self):
        event = self.db.create_event(S1, "调 drawio 配色", summary="把边框改成 1px")
        self.db.index_fts("event", event.id, event.title, event.summary)
        hits = self.db.search_fts("drawio")
        self.assertEqual([(h["ref_kind"], h["ref_id"]) for h in hits], [("event", event.id)])

    def test_chinese_search_uses_trigram(self):
        exp = self.db.insert_experience(self._exp(content="数据库用 SQLite 存储"))
        self.db.index_fts("experience", exp, "", "数据库用 SQLite 存储")
        self.assertEqual(self.db.search_fts("数据库")[0]["ref_id"], exp)
        self.assertEqual(self.db.search_fts("SQLite")[0]["ref_id"], exp)

    def test_short_query_falls_back_to_like(self):
        cog = self.db.insert_cognition(self._cog(content="偏好中文回复"))
        self.db.index_fts("cognition", cog, "", "偏好中文回复")
        self.assertEqual(self.db.search_fts("偏好")[0]["ref_id"], cog)

    def test_reindex_replaces_old_body(self):
        exp = self.db.insert_experience(self._exp())
        self.db.index_fts("experience", exp, "", "旧内容")
        self.db.index_fts("experience", exp, "", "新内容")
        self.assertEqual(self.db.search_fts("旧内容"), [])
        self.assertTrue(self.db.search_fts("新内容"))

    def test_special_characters_do_not_break_match(self):
        exp = self.db.insert_experience(self._exp(content="修复 sys.stdin.reconfig 拼写"))
        self.db.index_fts("experience", exp, "", "修复 sys.stdin.reconfig 拼写")
        hits = self.db.search_fts("sys.stdin.reconfig")
        self.assertEqual(hits[0]["ref_id"], exp)

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            self.db.index_fts("memory", 1, "", "x")

    def _exp(self, **kw):
        base = dict(context="c", content="x", source_event_id=1)
        base.update(kw)
        return Experience(**base)

    def _cog(self, **kw):
        base = dict(subject="用户", content="x", source_event_id=1)
        base.update(kw)
        return Cognition(**base)


class TestSpans(DatabaseCase):
    def test_event_span_from_messages(self):
        self.db.insert_message(make_message("m_1", 0, "2026-10-20T10:00:00+08:00"))
        self.db.insert_message(make_message("m_2", 1, "2026-10-20T10:05:00+08:00"))
        for mid in ("m_1", "m_2"):
            self.db.insert_memory(make_memory(mid, 0 if mid == "m_1" else 1,
                                              "2026-10-20T10:00:00+08:00"))
        event = self.db.create_event(S1, "一个事件")
        self.db.bind_event(S1, "m_1", event.id)
        self.db.bind_event(S1, "m_2", event.id)

        span = self.db.event_span(event.id)
        self.assertEqual(span.first_at, "2026-10-20T10:00:00+08:00")
        self.assertEqual(span.last_at, "2026-10-20T10:05:00+08:00")

    def test_event_span_without_messages(self):
        event = self.db.create_event(S1, "空事件")
        self.assertIsNone(self.db.event_span(event.id))

    def test_overlapping_query(self):
        morning = self.seed_event(S1, "上午", 0, "2026-10-20T09:00:00+08:00")
        noon = self.seed_event(S1, "中午", 1, "2026-10-20T12:00:00+08:00")

        hits = self.db.list_events_overlapping(S1, "2026-10-20T08:30:00+08:00",
                                               "2026-10-20T09:30:00+08:00")
        self.assertEqual([s.event.id for s in hits], [morning.id])

        both = self.db.list_events_overlapping(S1, "2026-10-20T08:00:00+08:00",
                                               "2026-10-20T13:00:00+08:00")
        self.assertEqual({s.event.id for s in both}, {morning.id, noon.id})

        none = self.db.list_events_overlapping(S1, "2026-10-21T00:00:00+08:00",
                                               "2026-10-21T01:00:00+08:00")
        self.assertEqual(none, [])


class TestTransactions(DatabaseCase):
    def test_rollback_on_exception(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                self.db.insert_message(make_message("m_x", 0, "2026-10-20T10:00:00+08:00"))
                raise RuntimeError("boom")
        self.assertEqual(self.db.count_messages(S1), 0)

    def test_nested_transaction_uses_savepoint(self):
        with self.db.transaction():
            self.db.insert_message(make_message("m_1", 0, "2026-10-20T10:00:00+08:00"))
            self.db.insert_messages([make_message("m_2", 1, "2026-10-20T10:01:00+08:00")])
        self.assertEqual(self.db.count_messages(S1), 2)

    def test_commit_keeps_rows(self):
        with self.db.transaction():
            self.db.insert_message(make_message("m_1", 0, "2026-10-20T10:00:00+08:00"))
        self.assertEqual(self.db.count_messages(S1), 1)


class TestAppendOnly(DatabaseCase):
    def test_no_hard_delete_api(self):
        '''允许 invalidate（软失效：只改状态、不删行）；禁止 delete/remove/purge/expire 类接口。'''
        public = [name for name in dir(Database) if not name.startswith("_")]
        for banned in ("delete", "remove", "purge", "expire"):
            self.assertFalse([n for n in public if banned in n],
                             f"存储层不应提供 {banned} 类接口: {public}")


if __name__ == "__main__":
    unittest.main()

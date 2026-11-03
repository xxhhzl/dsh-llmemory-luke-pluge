# -*- coding: utf-8 -*-
"""批2 回归：事件激活、查询次数、LIKE 转义、短查询排序、时区迁移、FTS 清理。"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import retrieval
from database import Cognition, Database, Experience, Memory, Message

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_batch2"
S1 = "s_b2"
SEQ = 0


def iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class Batch2Case(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def add_event(self, title, summary, *, cnt=0):
        event = self.db.create_event(S1, title, summary=summary)
        self.db.index_fts("event", event.id, title, summary)
        if cnt:
            self.db.bump_event_cnt(event.id, cnt)
        return event.id

    def add_experience(self, content, *, cues=(), importance=3, use_count=0):
        item = self.db.insert_experience(Experience(
            content=content, kind="technique", importance=importance,
            confidence=0.9, source_event_id=1, cues=tuple(cues),
            created_at=iso(5), use_count=use_count))
        self.db.index_fts("experience", item, "", content)
        return item


class TestEventActivation(Batch2Case):
    """M4：事件的 cnt 必须进入 ACT-R 激活（字段名不同曾导致恒为 0）。"""

    def test_higher_cnt_scores_higher_at_equal_relevance(self):
        """同等相关性下，cnt 高的事件综合分更高（相关性权重仍占主导）。"""
        old = self.add_event("drawio 边框", "把边框改成 1px", cnt=0)
        used = self.add_event("drawio 边框", "把边框改成 1px", cnt=10)
        found = {
            ("event", old): {"row": self.db.get_event(old), "rel": 0.8, "assoc": 1.0},
            ("event", used): {"row": self.db.get_event(used), "rel": 0.8, "assoc": 1.0},
        }
        scored = {row[2]: row[0] for row in retrieval._score(
            found, retrieval._weights(), None)}
        self.assertGreater(scored[used], scored[old])

    def test_meta_reports_cnt(self):
        event = self.add_event("drawio 边框", "把边框改成 1px", cnt=7)
        hits = retrieval.search(self.db, "drawio", touch=False)
        self.assertEqual(hits[0].meta["use_count"], 7)

    def test_activation_reads_cnt(self):
        self.assertEqual(retrieval._use_count(type("E", (), {"cnt": 5})(), "event"), 5)
        self.assertEqual(retrieval._use_count(
            type("L", (), {"use_count": 3})(), "experience"), 3)

    def test_used_event_not_in_explore_slot(self):
        """探索位只挑 use_count==0；事件用 cnt 判定，不能恒为零。"""
        used = self.add_event("drawio 边框", "边框 1px", cnt=3)
        fresh = self.add_event("drawio 配色", "配色改蓝", cnt=0)
        hits = retrieval.search(self.db, "drawio", touch=False, limit=2)
        self.assertIn(used, [hit.id for hit in hits if hit.kind == "event"])


class TestQueryCount(Batch2Case):
    """M5：检索的 SQL 条数要随命中数**批量**，不能线性放大。"""

    def test_search_sql_count_is_bounded(self):
        for index in range(30):
            self.add_event(f"drawio 事件 {index}", f"摘要 {index}")
        for index in range(20):
            self.add_experience(f"drawio 经验 {index}", cues=("drawio",))
        statements = []
        self.db._conn.set_trace_callback(statements.append)
        try:
            retrieval.search(self.db, "drawio", touch=False, limit=10)
        finally:
            self.db._conn.set_trace_callback(None)

        # 只统计应用层查询：FTS5 内部会发 PRAGMA data_version / pgno 查找等噪音
        app_sql = [stmt for stmt in statements
                   if stmt.lstrip().upper().startswith("SELECT")
                   and "memory_fts_idx" not in stmt
                   and "data_version" not in stmt]
        # 旧实现：50 条命中各 1 次 get + 各 1 次联想查询 ≈ 100 条
        self.assertLess(len(app_sql), 30, f"实际 {len(app_sql)} 条应用层 SQL")


class TestLikeEscape(Batch2Case):
    def test_wildcard_is_literal(self):
        a = self.add_experience("正常经验")
        self.add_experience("另一条经验")
        # 查询里的 % 是字面量，不能命中全部
        self.assertEqual(retrieval._candidates(
            self.db, "%", None, "all", {"experience"},
            datetime.now(timezone.utc)), {})

    def test_underscore_is_literal(self):
        self.add_experience("abc经验")
        found = retrieval._candidates(self.db, "a_c", None, "all",
                                      {"experience"}, datetime.now(timezone.utc))
        self.assertEqual(found, {})


class TestShortQueryOrder(Batch2Case):
    """中文两字查询走 LIKE 回退：标题命中应优先于仅正文命中。"""

    def test_title_hit_first(self):
        body_only = self.add_experience("先做一件事然后配色调整")
        title_hit = self.add_experience("配色")
        hits = retrieval.search(self.db, "配色", touch=False, limit=5)
        ids = [hit.id for hit in hits if hit.kind == "experience"]
        self.assertEqual(ids[0], title_hit)


class TestTimestampMigration(Batch2Case):
    """时区归一：老库的 +08:00 在打开时改写成 +00:00，且只做一次。"""

    def test_legacy_offsets_normalized_once(self):
        msg = Message(session_id=S1, message_id="m_legacy", owner="a", seq=0,
                      role="user", content="x",
                      occur_at="2026-11-03T10:00:00+08:00",
                      created_at="2026-11-03T10:00:00+08:00")
        self.db.insert_message(msg)
        self.db.insert_memory(Memory(session_id=S1, message_id="m_legacy", owner="a",
                                     seq=0, occur_at=msg.occur_at,
                                     created_at=msg.created_at))
        self.db._conn.execute("PRAGMA user_version = 0")
        self.db.close()

        self.db = Database(self.db_path)
        row = self.db._conn.execute(
            "SELECT occur_at FROM message WHERE message_id = 'm_legacy'").fetchone()
        self.assertEqual(row["occur_at"], "2026-11-03T02:00:00+00:00")
        version = self.db._conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)

        # 再打开一次：不再改写（幂等）
        self.db.close()
        self.db = Database(self.db_path)
        row = self.db._conn.execute(
            "SELECT occur_at FROM message WHERE message_id = 'm_legacy'").fetchone()
        self.assertEqual(row["occur_at"], "2026-11-03T02:00:00+00:00")

    def test_to_utc_iso_handles_z_and_naive(self):
        self.assertEqual(base.to_utc_iso("2026-11-03T10:00:00Z"),
                         "2026-11-03T10:00:00+00:00")
        self.assertEqual(base.to_utc_iso("2026-11-03T10:00:00"),
                         "2026-11-03T10:00:00+00:00")
        self.assertEqual(base.to_utc_iso("2026-11-03T10:00:00+08:00"),
                         "2026-11-03T02:00:00+00:00")


class TestFtsCleanup(Batch2Case):
    """失效/被取代的条目要退出全文索引，不能占着召回名额。"""

    def test_invalidate_removes_fts_row(self):
        item = self.add_experience("strokeWidth 用法")
        self.assertTrue(self.db.search_fts("strokeWidth"))
        self.db.invalidate_experience(item, "过时")
        self.assertEqual(self.db.search_fts("strokeWidth"), [])

    def test_supersede_removes_old_fts_row(self):
        old = self.add_experience("用 SQLite")
        new = self.db.supersede_experience(old, Experience(
            content="改用 Postgres", source_event_id=1, created_at=iso(0)))
        rows = self.db.search_fts("SQLite")
        self.assertEqual(rows, [])
        self.db.index_fts("experience", new, "", "改用 Postgres")
        self.assertEqual(len(self.db.search_fts("Postgres")), 1)


if __name__ == "__main__":
    unittest.main()

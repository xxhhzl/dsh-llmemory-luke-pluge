# -*- coding: utf-8 -*-
"""本轮审查修复的回归：摘要覆盖、事件范围列、失败不清空。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ingest
from database import Cognition, Database, Experience
from summary import SummaryResult

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_review_fixes"
S1 = "s_fix"


class FixCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def fts_scope(self, event_id: int):
        row = self.db._conn.execute(
            "SELECT origin_project, session_id FROM memory_fts "
            "WHERE ref_kind = 'event' AND ref_id = ?", (event_id,)).fetchone()
        return (row["origin_project"], row["session_id"]) if row else (None, None)


class TestResummarizeKeepsSummary(FixCase):
    """补摘要失败/为空时，不能把上一次的好摘要抹掉。"""

    def test_failed_summary_keeps_previous(self):
        event = self.db.create_event(S1, "旧标题", summary="旧摘要")
        before = self.db.get_event(event.id)
        ingest.apply_summary(self.db, session_id=S1,
                             summary=SummaryResult(ok=False, error="超时"),
                             event_id=event.id, refresh=False)
        after = self.db.get_event(event.id)
        self.assertEqual(after.summary, "旧摘要", "失败不能清空摘要")
        self.assertEqual(after.summarized_at, before.summarized_at,
                         "失败不能推进摘要时间（否则 24h 阈值不补跑）")
        self.assertTrue(after.needs_review, "失败应标记待复核")

    def test_empty_summary_keeps_previous(self):
        event = self.db.create_event(S1, "标题", summary="旧摘要")
        ingest.apply_summary(self.db, session_id=S1,
                             summary=SummaryResult(ok=True, title="新标题", summary=""),
                             event_id=event.id, refresh=False)
        after = self.db.get_event(event.id)
        self.assertEqual(after.summary, "旧摘要")
        self.assertEqual(after.title, "新标题", "标题仍然可以更新")

    def test_success_replaces_summary(self):
        event = self.db.create_event(S1, "旧标题", summary="旧摘要")
        ingest.apply_summary(self.db, session_id=S1,
                             summary=SummaryResult(ok=True, title="新标题",
                                                   summary="新摘要"),
                             event_id=event.id, refresh=False)
        after = self.db.get_event(event.id)
        self.assertEqual(after.summary, "新摘要")
        self.assertTrue(after.summarized_at)
        self.assertFalse(after.needs_review)


class TestEventScopeInFts(FixCase):
    """事件 FTS 的范围列取事件自身归属，而不是本次 job 的项目。"""

    def test_global_event_stays_global_after_project_job(self):
        event = self.db.create_event(S1, "全局事件", summary="初始摘要")
        self.db.index_fts("event", event.id, "全局事件", "初始摘要",
                          origin_project=None, session_id=S1)
        ingest.apply_summary(self.db, session_id=S1,
                             summary=SummaryResult(ok=True, title="全局事件",
                                                   summary="续写后的摘要"),
                             event_id=event.id, origin_project="projA",
                             refresh=False)
        scope, _ = self.fts_scope(event.id)
        self.assertIsNone(scope, "全局事件被项目续写后仍应全局可见")

    def test_new_event_keeps_its_project_scope(self):
        ingest.apply_summary(self.db, session_id=S1,
                             summary=SummaryResult(ok=True, title="项目事件",
                                                   summary="摘要内容"),
                             origin_project="projA", refresh=False)
        row = self.db._conn.execute(
            "SELECT ref_id, origin_project FROM memory_fts "
            "WHERE ref_kind = 'event' AND title = '项目事件'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["origin_project"], "projA")


class TestFtsReindex(FixCase):
    """重建索引：只回灌生效条目；老库迁移当场回灌，不留空索引。"""

    def _experience(self, content: str) -> int:
        return self.db.insert_experience(Experience(
            content=content, source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))

    def test_reindex_skips_inactive_rows(self):
        active = self._experience("有效经验")
        superseded = self._experience("被取代的经验")
        invalid = self.db.insert_cognition(Cognition(
            subject="用户", content="失效认知", source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        self.db._conn.execute("UPDATE experience SET status = 'superseded' WHERE id = ?",
                              (superseded,))
        self.db._conn.execute("UPDATE cognition SET status = 'invalid' WHERE id = ?",
                              (invalid,))
        self.db._conn.commit()

        counts = self.db.reindex_fts()

        keys = {(row["ref_kind"], row["ref_id"]) for row in
                self.db._conn.execute("SELECT ref_kind, ref_id FROM memory_fts")}
        self.assertIn(("experience", active), keys)
        self.assertNotIn(("experience", superseded), keys, "被取代的条目不该回灌")
        self.assertNotIn(("cognition", invalid), keys, "失效条目不该回灌")
        self.assertEqual(counts["experience"], 1)
        self.assertEqual(counts["cognition"], 0)

    def test_migration_rebuilds_index_in_same_transaction(self):
        event = self.db.create_event(S1, "老库事件", summary="老库摘要内容")
        # 造一个“老库”：memory_fts 只有 5 列且没有范围列
        self.db._conn.execute("DROP TABLE memory_fts")
        self.db._conn.execute(
            "CREATE VIRTUAL TABLE memory_fts USING fts5("
            "ref_kind UNINDEXED, ref_id UNINDEXED, title, body, tokenize='trigram')")
        self.db._conn.execute(
            "INSERT INTO memory_fts (ref_kind, ref_id, title, body) "
            "VALUES ('event', ?, '老库事件', '老库摘要内容')", (event.id,))
        self.db._conn.commit()
        self.db.close()

        self.db = Database(self._dir / "memory.db")     # 重开触发迁移
        columns = [row["name"] for row in
                   self.db._conn.execute("PRAGMA table_info(memory_fts)")]
        self.assertIn("origin_project", columns)
        self.assertIn("session_id", columns)
        self.assertTrue(self.db.search_fts("老库摘要"), "迁移后索引必须立刻可用")
        scope = self.db._conn.execute(
            "SELECT session_id FROM memory_fts WHERE ref_kind='event' AND ref_id=?",
            (event.id,)).fetchone()
        self.assertEqual(scope["session_id"], S1, "回灌要带上范围列")


class TestTimestampsAndConfig(FixCase):
    """occur_at 兜底必须是 UTC；rrf_k 这类死配置不该留在配置树里。"""

    def test_missing_ts_falls_back_to_utc(self):
        job = {"session_id": S1, "messages": [
            {"msg_id": "m1", "seq": 0, "role": "user", "content": "无时间戳"},
        ]}
        ingest.IngestWorker._store_messages(None, self.db, job)
        row = self.db.get_message(S1, "m1")
        self.assertTrue(row.occur_at.endswith("+00:00"),
                        f"兜底时间必须是 UTC，实际 {row.occur_at}")

    def test_invalid_ts_falls_back_to_utc(self):
        job = {"session_id": S1, "messages": [
            {"msg_id": "m2", "seq": 0, "role": "user", "content": "坏时间戳",
             "ts": "不是时间"},
        ]}
        ingest.IngestWorker._store_messages(None, self.db, job)
        self.assertTrue(self.db.get_message(S1, "m2").occur_at.endswith("+00:00"))

    def test_local_ts_is_normalized(self):
        job = {"session_id": S1, "messages": [
            {"msg_id": "m3", "seq": 0, "role": "user", "content": "东八区",
             "ts": "2026-11-03T20:00:00+08:00"},
        ]}
        ingest.IngestWorker._store_messages(None, self.db, job)
        self.assertEqual(self.db.get_message(S1, "m3").occur_at,
                         "2026-11-03T12:00:00+00:00")

    def test_rrf_k_is_gone_from_config(self):
        import config
        self.assertNotIn("rrf_k", config.DEFAULT_CONFIG["search"],
                         "rrf_k 从未被实现，留在配置里只会误导")


class TestMinmax(FixCase):
    def test_single_value_scores_top(self):
        from retrieval import _minmax
        self.assertEqual(_minmax([]), [])
        self.assertEqual(_minmax([3.2]), [1.0], "单条命中不该被压到 0.5")
        self.assertEqual(_minmax([2.0, 2.0]), [0.5, 0.5], "全等值保持中性")
        self.assertEqual(_minmax([1.0, 3.0]), [0.0, 1.0])


if __name__ == "__main__":
    unittest.main()

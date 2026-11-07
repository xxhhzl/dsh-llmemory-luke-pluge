# -*- coding: utf-8 -*-
"""批8 回归：FTS 范围预过滤与迁移、事件 last_used_at、跨项目衰减、经验泛化晋升。"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import config
import ingest
import retrieval
from database import Cognition, Database, Experience

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_batch8"
S1 = "s_b8"


def exp(content, *, project=None, use_count=0, event_id=1):
    return Experience(content=content, source_event_id=event_id,
                      origin_project=project, use_count=use_count,
                      created_at="2026-11-03T00:00:00+00:00")


class Batch8Case(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestFtsScopePrefilter(Batch8Case):
    """范围过滤要发生在召回**之前**：范围外条目不能占满 top-k。"""

    def test_project_scope_not_crowded_out(self):
        mine = []
        for index in range(5):
            item = self.db.insert_experience(exp(f"配色规范 我的 {index}",
                                                 project=S1, event_id=100 + index))
            self.db.index_fts("experience", item, "", f"配色规范 我的 {index}",
                              origin_project=S1, session_id=S1)
            mine.append(item)
        for index in range(60):
            item = self.db.insert_experience(exp(f"配色规范 别的 {index}",
                                                 project="proj_other",
                                                 event_id=200 + index))
            self.db.index_fts("experience", item, "", f"配色规范 别的 {index}",
                              origin_project="proj_other", session_id="s_other")

        hits = retrieval.search(self.db, "配色规范", scope="project",
                                origin_project=S1, limit=10, touch=False)
        ids = {hit.id for hit in hits if hit.kind == "experience"}
        self.assertEqual(ids, set(mine),
                         "本项目条目应全部召回，不被 60 条范围外条目挤掉")

    def test_all_scope_sees_everything(self):
        a = self.db.insert_experience(exp("配色 A", project=S1))
        self.db.index_fts("experience", a, "", "配色 A", origin_project=S1)
        b = self.db.insert_experience(exp("配色 B", project="proj_other"))
        self.db.index_fts("experience", b, "", "配色 B", origin_project="proj_other")
        hits = retrieval.search(self.db, "配色", scope="all", origin_project=S1,
                                limit=10, touch=False)
        self.assertEqual({hit.id for hit in hits if hit.kind == "experience"}, {a, b})


class TestSqlitePragmas(Batch8Case):
    """受限环境下系统临时目录可能不可写：SQLite 临时表必须放内存。"""

    def test_temp_store_is_memory(self):
        value = self.db._conn.execute("PRAGMA temp_store").fetchone()[0]
        self.assertEqual(value, 2, "temp_store 应为 MEMORY")


class TestFtsMigration(Batch8Case):
    """老库的 memory_fts 没有范围列：打开时要重建并从源表回灌。"""

    def test_old_fts_is_rebuilt(self):
        event = self.db.create_event(S1, "调 drawio", summary="把边框改成 1px",
                                     origin_project=S1)
        item = self.db.insert_experience(exp("统一 strokeWidth", project=S1))
        self.db.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE memory_fts")
        conn.execute("CREATE VIRTUAL TABLE memory_fts USING fts5("
                     "ref_kind UNINDEXED, ref_id UNINDEXED, title, body, "
                     "tokenize='trigram')")
        conn.commit()
        conn.close()

        self.db = Database(self.db_path)          # 触发迁移
        columns = [row["name"] for row in
                   self.db._conn.execute("PRAGMA table_info(memory_fts)")]
        self.assertIn("origin_project", columns)
        self.assertIn("session_id", columns)
        self.assertTrue(self.db.search_fts("drawio"), "事件索引应被回灌")
        self.assertTrue(self.db.search_fts("strokeWidth"), "l3 索引应被回灌")
        row = self.db._conn.execute(
            "SELECT origin_project FROM memory_fts WHERE ref_kind='event' AND ref_id=?",
            (event.id,)).fetchone()
        self.assertEqual(row["origin_project"], S1, "回灌要带回范围列")
        self.assertEqual(self.db.get_experience(item).content, "统一 strokeWidth")


class TestEventLastUsed(Batch8Case):
    def test_bump_sets_last_used_at(self):
        event = self.db.create_event(S1, "事件")
        self.assertIsNone(self.db.get_event(event.id).last_used_at)
        self.db.bump_events([event.id])
        self.assertIsNotNone(self.db.get_event(event.id).last_used_at)

    def test_bump_event_cnt_sets_last_used_at(self):
        event = self.db.create_event(S1, "事件")
        self.db.bump_event_cnt(event.id)
        self.assertIsNotNone(self.db.get_event(event.id).last_used_at)

    def test_recently_used_event_activates_higher(self):
        old = self.db.create_event(S1, "旧事件")
        fresh = self.db.create_event(S1, "新事件")
        self.db.bump_events([fresh.id])
        old_row = self.db.get_event(old.id)
        fresh_row = self.db.get_event(fresh.id)
        old_act = retrieval.activation(retrieval._use_count(old_row, "event"),
                                       retrieval._last_used(old_row, "event"),
                                       old_row.created_at)
        fresh_act = retrieval.activation(retrieval._use_count(fresh_row, "event"),
                                         retrieval._last_used(fresh_row, "event"),
                                         fresh_row.created_at)
        self.assertGreater(fresh_act, old_act)


class TestCrossProjectPenalty(Batch8Case):
    def _scores(self, penalty: float) -> tuple:
        config.patch_config({"search": {"cross_project_penalty": penalty}})
        try:
            same = self.db.insert_cognition(Cognition(
                subject="用户", content="配色偏好", origin_project=S1,
                source_event_id=1, created_at="2026-11-03T00:00:00+00:00"))
            cross = self.db.insert_cognition(Cognition(
                subject="用户", content="配色偏好", origin_project="proj_other",
                source_event_id=1, created_at="2026-11-03T00:00:00+00:00"))
            found = {
                ("cognition", same): {"row": self.db.get_cognition(same),
                                      "rel": 0.8, "assoc": 1.0},
                ("cognition", cross): {"row": self.db.get_cognition(cross),
                                       "rel": 0.8, "assoc": 1.0},
            }
            scored = {row[2]: row[0] for row in retrieval._score(
                found, retrieval._weights(), S1)}
            return scored[same], scored[cross]
        finally:
            config.patch_config({"search": {"cross_project_penalty": 0.0}})

    def test_default_penalty_is_off(self):
        self.assertEqual(config.get_config()["search"]["cross_project_penalty"], 0.0)
        same, cross = self._scores(0.0)
        self.assertGreater(same, cross)          # 只有项目匹配的加分差

    def test_penalty_widens_the_gap(self):
        same_off, cross_off = self._scores(0.0)
        same_on, cross_on = self._scores(1.0)
        self.assertAlmostEqual(same_off, same_on, places=6)
        self.assertLess(cross_on, cross_off)


class TestPromotion(Batch8Case):
    """经验泛化晋升：反复使用后转通用，遵循只追加语义。"""

    def test_promotes_after_threshold(self):
        item = self.db.insert_experience(exp("先跑迁移再改库", project=S1,
                                             use_count=3))
        self.db.index_fts("experience", item, "", "先跑迁移再改库",
                          origin_project=S1, session_id=S1)

        promoted = ingest.promote_experiences(self.db, min_use=3)
        self.assertEqual(len(promoted), 1)
        new_id = promoted[0]

        new_row = self.db.get_experience(new_id)
        self.assertIsNone(new_row.origin_project, "晋升后应为通用")
        self.assertEqual(new_row.scope, "global", "晋升即视为跨项目适用")
        self.assertEqual(new_row.content, "先跑迁移再改库", "正文不变")
        old_row = self.db.get_experience(item)
        self.assertEqual(old_row.status, "superseded", "旧行保留但退休")
        # 通用条目对本项目仍可见
        visible = {row.id for row in self.db.list_experiences(origin_project=S1)}
        self.assertIn(new_id, visible)

    def test_below_threshold_untouched(self):
        self.db.insert_experience(exp("只用了两次", project=S1, use_count=2))
        self.assertEqual(ingest.promote_experiences(self.db, min_use=3), [])

    def test_global_entries_skipped(self):
        self.db.insert_experience(exp("已经是通用", project=None, use_count=9))
        self.assertEqual(ingest.promote_experiences(self.db, min_use=3), [])

    def test_fts_follows_promotion(self):
        item = self.db.insert_experience(exp("幂等写法", project=S1, use_count=5))
        self.db.index_fts("experience", item, "", "幂等写法",
                          origin_project=S1, session_id=S1)
        new_id = ingest.promote_experiences(self.db, min_use=3)[0]
        rows = self.db.search_fts("幂等写法")
        self.assertEqual([row["ref_id"] for row in rows], [new_id])
        self.assertIsNone(rows[0]["origin_project"], "新索引应为通用")


if __name__ == "__main__":
    unittest.main()

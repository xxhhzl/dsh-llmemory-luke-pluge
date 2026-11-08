# -*- coding: utf-8 -*-
"""ANN 检索（src/ann.py）：与精确 KNN 排序一致、增量同步、降级路径。

未安装 usearch/numpy 时整组跳过（那时检索走 vec0 精确 KNN，功能不变）。
"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ann                                      # noqa: E402
import database                                 # noqa: E402
import retrieval                                # noqa: E402
from database import Database, Experience       # noqa: E402

BASE_TMP = ROOT / ".tmp" / "test_ann"
HAS_ANN = ann.available() and getattr(database.Database, "_vec_enabled", None)


def vec(seed: int) -> list:
    rng = np.random.default_rng(seed)
    return rng.normal(size=1024).astype(np.float32).tolist()


def ordered(hits: dict) -> list:
    return [ref_id for ref_id, _ in sorted(hits.items(), key=lambda item: item[1])]


class AnnCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")
        if not self.db._vec_enabled:
            self.skipTest("未安装 sqlite-vec")
        if not ann.available():
            self.skipTest(f"usearch 不可用: {ann.import_error()}")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestAnnSearch(AnnCase):
    def seed(self, content, vector, project=None) -> int:
        ref_id = self.db.insert_experience(Experience(
            content=content, source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        self.db.vec_upsert("experience", ref_id, vector, project)
        return ref_id

    def test_matches_exact_ordering(self):
        vectors = {index: vec(index) for index in range(8)}
        ids = [self.seed(f"经验 {index}", vectors[index],
                         "proj_a" if index % 2 else None)
               for index in range(8)]
        query = vectors[3]
        exact = self.db.vec_search("experience", query, k=4)
        approx = self.db.search_vectors("experience", query, k=4)
        self.assertEqual(ordered(approx), ordered(exact))
        self.assertEqual(ordered(approx)[0], ids[3])

    def test_project_scope_filter(self):
        a = self.seed("项目 A", vec(1), "proj_a")
        b = self.seed("项目 B", vec(2), "proj_b")
        shared = self.seed("通用", vec(3))
        hits = self.db.search_vectors("experience", vec(1), k=10,
                                      origin_project="proj_a", scope="project")
        self.assertEqual(set(hits), {a, shared}, "本项目 + 通用，不含别的项目")
        everything = self.db.search_vectors("experience", vec(1), k=10,
                                            scope="all")
        self.assertEqual(set(everything), {a, b, shared})

    def test_overwrite_and_delete_sync(self):
        ref_id = self.seed("会变的向量", vec(10))
        hits = self.db.search_vectors("experience", vec(10), k=1)
        self.assertEqual(ordered(hits), [ref_id])

        new_vector = vec(99)
        self.db.vec_upsert("experience", ref_id, new_vector)
        hits = self.db.search_vectors("experience", new_vector, k=1)
        self.assertEqual(ordered(hits), [ref_id], "覆盖写后要命中新向量")

        self.db.vec_unindex("experience", ref_id)
        self.assertEqual(self.db.search_vectors("experience", new_vector, k=3), {})

    def test_new_rows_are_picked_up(self):
        self.seed("第一条", vec(21))
        self.assertEqual(len(self.db.search_vectors("experience", vec(21), k=5)), 1)
        second = self.seed("第二条", vec(22))
        hits = self.db.search_vectors("experience", vec(22), k=5)
        self.assertIn(second, hits, "新增条目要能立刻被 ANN 找回")

    def test_status_reports_backend(self):
        self.seed("随便", vec(31))
        self.db.search_vectors("experience", vec(31), k=1)   # 触发装载
        status = ann.get_store().status()
        self.assertTrue(status["available"])
        self.assertEqual(status["backend"], "usearch")
        self.assertGreaterEqual(status["indexed"].get("experience", 0), 1)

    def test_fallback_when_ann_unavailable(self):
        ref_id = self.seed("降级也要能搜", vec(41))
        with mock.patch.object(ann, "available", return_value=False):
            hits = self.db.search_vectors("experience", vec(41), k=1)
        self.assertEqual(ordered(hits), [ref_id], "退回精确 KNN 仍要正确")

    def test_retrieval_goes_through_search_vectors(self):
        self.seed("drawio 导出要统一线宽", vec(51))
        calls = []
        original = self.db.search_vectors

        def recording(kind, vector, **kwargs):
            calls.append(kind)
            return original(kind, vector, **kwargs)

        self.db.search_vectors = recording
        retrieval.search(self.db, "drawio", query_vector=vec(51), touch=False)
        self.assertIn("experience", calls, "检索要走 search_vectors（ANN 入口）")


if __name__ == "__main__":
    unittest.main()

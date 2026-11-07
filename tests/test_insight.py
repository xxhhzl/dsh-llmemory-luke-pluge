# -*- coding: utf-8 -*-
"""洞见归纳（src/insight.py）单测：聚类、解析校验、落库与检索。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import database                                   # noqa: E402
import insight                                    # noqa: E402
import retrieval                                  # noqa: E402
from database import Database, Experience         # noqa: E402
from summary import FakeSummaryTransport, SummaryError  # noqa: E402

BASE_TMP = ROOT / ".tmp" / "test_insight"
S1 = "s_insight"


class InsightCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def seed(self, content, cues=("edit", "old_string"), kind="pitfall"):
        new_id = self.db.insert_experience(Experience(
            context="改文件时", content=content, kind=kind, importance=3,
            origin_session=S1, origin_project="proj_a", scope="global",
            source_event_id=1, cues=cues, created_at="2026-11-01T09:00:00+08:00"))
        return self.db.get_experience(new_id)


class TestClusters(InsightCase):
    def test_groups_by_primary_cue_and_threshold(self):
        for text in ("先 read 再改", "先定位再改", "别盲猜内容"):
            self.seed(text)
        self.seed("另一类做法", cues=("队列", "重试"))
        groups = insight.clusters(self.db, min_support=3)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], "edit")
        self.assertEqual(len(groups[0][1]), 3)

    def test_no_cues_skipped(self):
        for text in ("无线索甲", "无线索乙", "无线索丙"):
            self.seed(text, cues=())
        self.assertEqual(insight.clusters(self.db, min_support=2), [])


class TestParse(InsightCase):
    def setUp(self):
        super().setUp()
        self.rows = [self.seed("先 read 再改"), self.seed("先定位再改"),
                     self.seed("别盲猜内容")]

    def test_valid_insight(self):
        raw = json.dumps({"insights": [{
            "content": "编辑前先读取当前内容", "scope": "global",
            "confidence": 0.9, "cues": ["edit"], "source_ids": [1, 2]}]})
        result = insight.parse_insights(raw, self.rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_ids"], (1, 2))

    def test_single_source_rejected(self):
        raw = json.dumps({"insights": [{"content": "只有一条", "source_ids": [1]}]})
        self.assertEqual(insight.parse_insights(raw, self.rows), [])

    def test_foreign_source_rejected(self):
        raw = json.dumps({"insights": [{
            "content": "编造证据", "source_ids": [1, 999]}]})
        self.assertEqual(insight.parse_insights(raw, self.rows), [])

    def test_duplicate_content_deduped(self):
        raw = json.dumps({"insights": [
            {"content": "同一条", "source_ids": [1, 2]},
            {"content": "同一条", "source_ids": [1, 3]}]})
        self.assertEqual(len(insight.parse_insights(raw, self.rows)), 1)

    def test_unknown_scope_defaults_global(self):
        raw = json.dumps({"insights": [{
            "content": "规则", "scope": "everywhere", "source_ids": [1, 2]}]})
        self.assertEqual(insight.parse_insights(raw, self.rows)[0]["scope"], "global")


class TestInduce(InsightCase):
    def test_induce_writes_rows_and_fts(self):
        rows = [self.seed("先 read 再改"), self.seed("先定位再改"),
                self.seed("别盲猜内容")]
        response = json.dumps({"insights": [{
            "content": "编辑前先读取当前内容再构造替换串",
            "scope": "global", "confidence": 0.9, "cues": ["edit", "old_string"],
            "source_ids": [row.id for row in rows]}]}, ensure_ascii=False)
        transport = FakeSummaryTransport([response])
        report = insight.induce(self.db, transport, min_support=3)

        self.assertEqual(len(report), 1)
        self.assertEqual(len(report[0]["ids"]), 1)
        new_id = report[0]["ids"][0]
        row = self.db.get_insight(new_id)
        self.assertEqual(row.content, "编辑前先读取当前内容再构造替换串")
        self.assertEqual(set(row.source_experience_ids), {r.id for r in rows})
        self.assertEqual(row.scope, "global")

        hits = retrieval.search(self.db, "编辑前先读取", kinds=("insight",))
        self.assertEqual([hit.kind for hit in hits], ["insight"])
        self.assertEqual(hits[0].id, new_id)

    def test_transport_error_does_not_crash(self):
        for text in ("甲", "乙", "丙"):
            self.seed(text)
        report = insight.induce(self.db, FakeSummaryTransport([SummaryError("boom")]),
                                min_support=3)
        self.assertEqual(len(report), 1)
        self.assertIn("boom", report[0]["error"])

    def test_commit_false_does_not_write(self):
        for text in ("甲", "乙", "丙"):
            self.seed(text)
        response = json.dumps({"insights": [{
            "content": "规则", "source_ids": [1, 2]}]})
        report = insight.induce(self.db, FakeSummaryTransport([response]),
                                min_support=3, commit=False)
        self.assertEqual(report[0]["ids"], [])
        self.assertEqual(self.db.list_insights(), [])


if __name__ == "__main__":
    unittest.main()

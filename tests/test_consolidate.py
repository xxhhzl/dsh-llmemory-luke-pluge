# -*- coding: utf-8 -*-
"""调用 B（src/consolidate.py）单测：候选召回、裁决解析、与落库联动。"""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import database
from database import Cognition, Database, Experience
from consolidate import decide, gather_candidates, parse_decisions
from ingest import ingest_event
from summary import FakeSummaryTransport, SummaryError, SummaryResult

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_consolidate"
S1 = "s_consolidate_1"


def cog_item(content="数据库改用 Postgres", subject="项目 X"):
    return {"table": "cognition", "subject": subject, "content": content,
            "kind": "constraint", "importance": 5}


def summary_with(*, experiences=(), cognitions=()):
    return SummaryResult(ok=True, title="换库", summary="把数据库从 SQLite 换成 Postgres。",
                         experiences=tuple(experiences), cognitions=tuple(cognitions))


class ConsolidateCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def seed_cognition(self, content="数据库用 SQLite", subject="项目 X"):
        return self.db.insert_cognition(Cognition(
            subject=subject, content=content, kind="constraint", importance=5,
            origin_session=S1, source_event_id=1, created_at="2026-11-01T09:00:00+08:00"))

    def seed_experience(self, content="改库前先跑迁移脚本"):
        return self.db.insert_experience(Experience(
            context="切换数据库时", content=content, kind="technique", importance=4,
            origin_session=S1, source_event_id=1, created_at="2026-11-01T09:00:00+08:00"))


class TestGather(ConsolidateCase):
    def test_matches_subject(self):
        old = self.seed_cognition()
        items = [cog_item()]
        candidates = gather_candidates(self.db, items)
        self.assertEqual([row.id for row in candidates[0]], [old])

    def test_matches_subject_variant(self):
        '''同物异名也要召回：主体写「项目 X 环境」时能找到「项目 X」。'''
        old = self.seed_cognition(subject="项目 X")
        items = [cog_item(subject="项目 X 环境")]
        candidates = gather_candidates(self.db, items)
        self.assertEqual([row.id for row in candidates[0]], [old])

    def test_excludes_superseded(self):
        old = self.seed_cognition()
        self.db.invalidate_cognition(old, "旧结论")
        self.assertEqual(gather_candidates(self.db, [cog_item()])[0], [])


class TestParse(ConsolidateCase):
    def test_invalid_target_degrades_to_new(self):
        old = self.seed_cognition()
        items = [cog_item()]
        candidates = gather_candidates(self.db, items)
        raw = json.dumps({"decisions": [{"index": 0, "action": "supersede",
                                          "target_id": 999, "reason": "x"}]})
        result = parse_decisions(raw, items, candidates)
        self.assertEqual(result.items[0]["action"], "new")

    def test_unknown_action_degrades(self):
        items = [cog_item()]
        candidates = gather_candidates(self.db, items)
        raw = json.dumps({"decisions": [{"index": 0, "action": "delete"}]})
        self.assertEqual(parse_decisions(raw, items, candidates).items[0]["action"], "new")

    def test_garbage_marks_review(self):
        result = parse_decisions("不是 json", [cog_item()], [[]])
        self.assertTrue(result.needs_review)
        self.assertEqual(result.error, "json_parse")

    def test_canonical_subject_taken_from_candidate(self):
        '''同物异名：模型给出候选的写法，裁决沿用该写法。'''
        self.seed_cognition(subject="项目 X")
        items = [cog_item(subject="项目 X 环境")]
        candidates = gather_candidates(self.db, items)
        raw = json.dumps({"decisions": [{"index": 0, "action": "supersede",
                                          "target_id": candidates[0][0].id,
                                          "reason": "同一项目",
                                          "subject": "项目 X"}]})
        result = parse_decisions(raw, items, candidates)
        self.assertEqual(result.items[0]["subject"], "项目 X")

    def test_fabricated_subject_is_ignored(self):
        '''模型自造第三种写法 → 忽略（保持原样），防止又造一个变体。'''
        self.seed_cognition(subject="项目 X")
        items = [cog_item(subject="项目 X 环境")]
        candidates = gather_candidates(self.db, items)
        raw = json.dumps({"decisions": [{"index": 0, "action": "new",
                                          "subject": "X 项目的运行环境"}]})
        result = parse_decisions(raw, items, candidates)
        self.assertIsNone(result.items[0]["subject"])

    def test_subject_same_as_own_is_ignored(self):
        self.seed_cognition(subject="项目 X")
        items = [cog_item(subject="项目 X 环境")]
        candidates = gather_candidates(self.db, items)
        raw = json.dumps({"decisions": [{"index": 0, "action": "new",
                                          "subject": "项目 X 环境"}]})
        result = parse_decisions(raw, items, candidates)
        self.assertIsNone(result.items[0]["subject"])

    def test_experience_subject_is_never_taken(self):
        old = self.seed_experience()
        items = [{"table": "experience", "content": "改库前先跑迁移脚本",
                  "kind": "technique"}]
        candidates = gather_candidates(self.db, items)
        raw = json.dumps({"decisions": [{"index": 0, "action": "supersede",
                                          "target_id": old, "reason": "同一做法",
                                          "subject": "随便一个主体"}]})
        result = parse_decisions(raw, items, candidates)
        self.assertIsNone(result.items[0]["subject"])


class TestDecide(ConsolidateCase):
    def test_no_candidates_skips_model(self):
        transport = FakeSummaryTransport([json.dumps({"decisions": []})])
        result = decide(self.db, [cog_item()], transport)
        self.assertEqual(result.items, {})
        self.assertEqual(transport.calls, [])

    def test_transport_failure_returns_empty(self):
        self.seed_cognition()
        transport = FakeSummaryTransport([SummaryError("timeout")])
        result = decide(self.db, [cog_item()], transport)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.items, {})

    def test_supersede_decision(self):
        old = self.seed_cognition()
        transport = FakeSummaryTransport([json.dumps(
            {"decisions": [{"index": 0, "action": "supersede",
                            "target_id": old, "reason": "换库"}]})])
        result = decide(self.db, [cog_item()], transport)
        self.assertEqual(result.items[0]["action"], "supersede")
        self.assertEqual(result.items[0]["target_id"], old)


class TestIngestWithDecisions(ConsolidateCase):
    def test_supersede_writes_new_row_and_retires_old(self):
        old = self.seed_cognition()
        transport = FakeSummaryTransport([json.dumps(
            {"decisions": [{"index": 0, "action": "supersede",
                            "target_id": old, "reason": "换库"}]})])
        result = ingest_event(self.db, session_id=S1,
                              summary=summary_with(cognitions=[cog_item()]),
                              transport=transport)
        self.assertEqual(len(result.cognition_ids), 1)
        new_id = result.cognition_ids[0]
        self.assertNotEqual(new_id, old)
        self.assertEqual(self.db.get_cognition(old).status, "superseded")
        self.assertEqual(self.db.get_cognition(new_id).superseded_id, old)
        self.assertEqual([row.id for row in self.db.list_cognitions()], [new_id])
        self.assertIn("Postgres", self.db.get_cognition(new_id).content)

    def test_invalidate_retires_old_without_new_row(self):
        old = self.seed_experience()
        item = {"table": "experience", "context": "切换数据库时",
                "content": "旧迁移脚本已废弃", "kind": "technique",
                "cues": ("迁移",)}
        transport = FakeSummaryTransport([json.dumps(
            {"decisions": [{"index": 0, "action": "invalidate",
                            "target_id": old, "reason": "脚本被删除"}]})])
        result = ingest_event(self.db, session_id=S1,
                              summary=summary_with(experiences=[item]),
                              transport=transport)
        self.assertEqual(result.invalidated, (old,))
        self.assertEqual(result.experience_ids, ())
        row = self.db.get_experience(old)
        self.assertEqual(row.status, "invalid")
        self.assertEqual(row.invalid_reason, "脚本被删除")

    def test_new_when_no_candidates(self):
        transport = FakeSummaryTransport([json.dumps({"decisions": []})])
        result = ingest_event(self.db, session_id=S1,
                              summary=summary_with(cognitions=[cog_item()]),
                              transport=transport)
        self.assertEqual(len(result.cognition_ids), 1)
        self.assertEqual(self.db.get_cognition(result.cognition_ids[0]).status, "active")

    def test_canonical_subject_applied_on_insert(self):
        '''落库时用裁决给的规范主体，而不是模型新造的写法。'''
        old = self.seed_cognition(subject="项目 X")
        transport = FakeSummaryTransport([json.dumps(
            {"decisions": [{"index": 0, "action": "new",
                            "subject": "项目 X"}]})])
        result = ingest_event(self.db, session_id=S1,
                              summary=summary_with(cognitions=[
                                  cog_item(subject="项目 X 环境")]),
                              transport=transport)
        row = self.db.get_cognition(result.cognition_ids[0])
        self.assertEqual(row.subject, "项目 X")
        self.assertEqual(self.db.get_cognition(old).status, "active")


if __name__ == "__main__":
    unittest.main()

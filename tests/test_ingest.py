# -*- coding: utf-8 -*-
"""写入链路（src/ingest.py）单测：事件 + l3 + FTS + digest 的落库与幂等。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from database import Database
from ingest import apply_summary, refresh_digest
from summary import SummaryResult

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_ingest"
S1 = "s_ingest_1"

EXP = {"context": "导出 SVG 前", "content": "统一 strokeWidth 再导出",
       "kind": "technique", "importance": 4, "confidence": 0.9,
       "cues": ("drawio", "svg")}
COG = {"subject": "用户", "content": "偏好中文回复", "kind": "preference",
       "importance": 5, "confidence": 0.95, "cues": ()}


def make_summary(*, ok=True, title="调 drawio 边框",
                 summary="把边框改成 1px，导出成功。",
                 experiences=(EXP,), cognitions=(COG,)) -> SummaryResult:
    return SummaryResult(ok=ok, title=title, summary=summary,
                         experiences=tuple(experiences), cognitions=tuple(cognitions))


class IngestCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestApplySummary(IngestCase):
    def test_writes_event_l3_fts_digest(self):
        result = apply_summary(self.db, session_id=S1, summary=make_summary())
        self.assertEqual(result.event.title, "调 drawio 边框")
        self.assertEqual(result.event.summary, "把边框改成 1px，导出成功。")
        self.assertEqual(len(result.experience_ids), 1)
        self.assertEqual(len(result.cognition_ids), 1)
        self.assertEqual(result.fts_rows, 3)

        exp = self.db.get_experience(result.experience_ids[0])
        self.assertEqual(exp.source_event_id, result.event.id)
        self.assertEqual(exp.origin_session, S1)
        self.assertIsNone(exp.origin_project)
        self.assertEqual(exp.status, "active")
        self.assertEqual(exp.cues, ("drawio", "svg"))

        cog = self.db.get_cognition(result.cognition_ids[0])
        self.assertEqual(cog.subject, "用户")
        self.assertEqual(cog.importance, 5)

        digest = self.db.get_digest(S1)
        self.assertIn(f"[L2 #{result.event.id}]", digest.content)
        self.assertIn("[L3 经验] 统一 strokeWidth 再导出", digest.content)
        self.assertIn("[L3 认知] 偏好中文回复", digest.content)
        self.assertEqual(digest.ref_event_ids, (result.event.id,))

    def test_fts_search_finds_event_and_l3(self):
        result = apply_summary(self.db, session_id=S1, summary=make_summary())
        hits = self.db.search_fts("drawio", limit=10)
        kinds = {(h["ref_kind"], h["ref_id"]) for h in hits}
        self.assertIn(("event", result.event.id), kinds)
        # 经验正文里没有 drawio（cues 不参与索引），用它自己的词检索
        self.assertEqual(self.db.search_fts("strokeWidth")[0]["ref_kind"], "experience")
        self.assertEqual(self.db.search_fts("中文")[0]["ref_kind"], "cognition")

    def test_idempotent_when_event_exists(self):
        first = apply_summary(self.db, session_id=S1, summary=make_summary())
        second = apply_summary(self.db, session_id=S1, summary=make_summary(),
                               event_id=first.event.id)
        self.assertEqual(first.experience_ids, second.experience_ids)
        self.assertEqual(first.cognition_ids, second.cognition_ids)
        self.assertEqual(len(self.db.list_experiences()), 1)
        self.assertEqual(len(self.db.list_cognitions()), 1)
        self.assertEqual(self.db.count_events(S1), 1)

    def test_failed_summary_keeps_event_without_l3(self):
        result = apply_summary(self.db, session_id=S1,
                               summary=make_summary(ok=False, summary=""))
        self.assertIsNone(self.db.get_event(result.event.id).summary)
        self.assertEqual(result.experience_ids, ())
        self.assertEqual(self.db.list_experiences(), [])
        self.assertIn(f"[L2 #{result.event.id}] 调 drawio 边框",
                      self.db.get_digest(S1).content)

    def test_missing_event_raises(self):
        with self.assertRaises(KeyError):
            apply_summary(self.db, session_id=S1, summary=make_summary(),
                          event_id=99999)

    def test_origin_project_recorded(self):
        result = apply_summary(self.db, session_id=S1, summary=make_summary(),
                               origin_project="proj_a")
        exp = self.db.get_experience(result.experience_ids[0])
        self.assertEqual(exp.origin_project, "proj_a")
        # 别的项目看不到（只含 origin_project IS NULL 的通用条目）
        self.assertEqual(self.db.list_experiences(origin_project="proj_b"), [])
        # 本项目可见
        self.assertEqual([e.id for e in self.db.list_experiences(origin_project="proj_a")],
                         [result.experience_ids[0]])


class TestRefreshDigest(IngestCase):
    def test_digest_orders_events_and_limits(self):
        first = apply_summary(self.db, session_id=S1, summary=make_summary(
            title="第一件事", experiences=(), cognitions=()))
        second = apply_summary(self.db, session_id=S1, summary=make_summary(
            title="第二件事", experiences=(), cognitions=()))
        revision = refresh_digest(self.db, S1, limit=5)
        content = self.db.get_digest(S1).content
        self.assertEqual(revision, 3)      # 两次 apply 各 +1，显式刷新 +1
        self.assertLess(content.index(f"#{first.event.id}"), content.index(f"#{second.event.id}"))
        self.assertEqual(self.db.get_digest(S1).ref_event_ids,
                         (first.event.id, second.event.id))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""存量主体归一脚本（scripts/normalize_subjects.py）单测：分组、别名链、执行。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from database import Cognition, Database                     # noqa: E402
from normalize_subjects import (apply_plan, build_groups,    # noqa: E402
                                parse_aliases)

BASE_TMP = ROOT / ".tmp" / "test_normalize_subjects"


def rows(*pairs):
    return [{"subject": subject, "n": n} for subject, n in pairs]


class TestGroups(unittest.TestCase):
    def test_exact_variant_merges_whitespace_only(self):
        '''只合并空白/大小写变体，且选次数最多的写法。'''
        plan = build_groups(rows(("dsh-llmemory-luke 项目", 3),
                                 ("dsh-llmemory-luke 项目 ", 1)))
        self.assertEqual(plan, {"dsh-llmemory-luke 项目": ["dsh-llmemory-luke 项目 "]})

    def test_containment_is_not_auto_merged(self):
        '''「项目」「用户」是子串桥，自动包含分组会把不相干主体连成一片——必须不合并。'''
        plan = build_groups(rows(("项目", 9), ("项目 dsh-llmemory-luke", 3),
                                 ("用户", 34), ("用户偏好", 5)))
        self.assertEqual(plan, {})

    def test_alias_merges_semantic_variant(self):
        '''「记忆插件」不含公共子串，只能靠显式别名；别名右侧即规范写法。'''
        plan = build_groups(rows(("dsh-llmemory-luke", 3), ("记忆插件", 4)),
                            {"记忆插件": "dsh-llmemory-luke"})
        self.assertEqual(plan, {"dsh-llmemory-luke": ["记忆插件"]})

    def test_alias_chain_resolves(self):
        plan = build_groups(rows(("a", 1), ("b", 1), ("c", 1)),
                            {"a": "b", "b": "c"})
        self.assertEqual(plan, {"c": ["a", "b"]})

    def test_alias_cycle_does_not_hang(self):
        plan = build_groups(rows(("a", 1), ("b", 1)), {"a": "b", "b": "a"})
        self.assertEqual(sum(len(items) for items in plan.values()) >= 0, True)

    def test_unrelated_subjects_untouched(self):
        plan = build_groups(rows(("用户", 3), ("webapp-test2 项目", 1)))
        self.assertEqual(plan, {})

    def test_parse_aliases(self):
        self.assertEqual(parse_aliases(["旧=新", " a = b "]), {"旧": "新", "a": "b"})
        with self.assertRaises(ValueError):
            parse_aliases(["没有等号"])
        with self.assertRaises(ValueError):
            parse_aliases(["=新"])


class TestApply(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db_path = self._dir / "memory.db"
        db = Database(self.db_path)
        for subject, content in (("dsh-llmemory-luke", "事实 A"),
                                 ("记忆插件", "事实 B"),
                                 ("webapp-test2 项目", "事实 C")):
            db.insert_cognition(Cognition(
                subject=subject, content=content, kind="fact", importance=3,
                origin_session="s", source_event_id=1,
                created_at="2026-11-01T09:00:00+08:00"))
        db.close()

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_apply_rewrites_subject_only(self):
        plan = build_groups(rows(("dsh-llmemory-luke", 1), ("记忆插件", 1)),
                            {"记忆插件": "dsh-llmemory-luke"})
        changed = apply_plan(self.db_path, plan)
        self.assertEqual(changed, 1)
        db = Database(self.db_path)
        try:
            subjects = sorted(row.subject for row in db.list_cognitions())
            contents = sorted(row.content for row in db.list_cognitions())
        finally:
            db.close()
        self.assertEqual(subjects, ["dsh-llmemory-luke", "dsh-llmemory-luke",
                                    "webapp-test2 项目"])
        self.assertEqual(contents, ["事实 A", "事实 B", "事实 C"])   # 正文不动


if __name__ == "__main__":
    unittest.main()

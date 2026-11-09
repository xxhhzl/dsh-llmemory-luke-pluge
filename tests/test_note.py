# -*- coding: utf-8 -*-
"""写入工具（src/tools.py 的 memory_note）：主体校验、向量查重、索引同步。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tools
from database import Database

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_note"


class NoteCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")
        self._orig = tools._db
        tools._db = lambda: self.db
        self.dim = self.db.vec_status().get("dim") or 0

    def tearDown(self):
        tools._db = self._orig
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def _vec(self, *head):
        values = [0.0] * self.dim
        for index, value in enumerate(head):
            values[index] = float(value)
        return values


class TestNoteValidation(NoteCase):
    def test_cognition_requires_subject(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.memory_note("没有主体的一条认知", kind="cognition")
        self.assertEqual(ctx.exception.code, -32602)

    def test_empty_content_rejected(self):
        with self.assertRaises(tools.ToolError) as ctx:
            tools.memory_note("   ", kind="experience")
        self.assertEqual(ctx.exception.code, -32602)


class TestNoteDedup(NoteCase):
    def test_same_entry_collapses(self):
        first = tools.memory_note("完全相同的一句话", kind="experience")
        second = tools.memory_note("完全相同的一句话", kind="experience")
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "duplicate")
        self.assertEqual(second["id"], first["id"])

    def test_paraphrase_collapses_with_vector(self):
        if not self.dim:
            self.skipTest("向量不可用")
        vector = self._vec(1.0)
        first = tools.memory_note("改插件先进源码改并跑测试再覆盖安装",
                                  kind="experience", query_vector=vector)
        second = tools.memory_note("插件改完要先在源码跑测试，再覆盖安装到运行目录",
                                   kind="experience", query_vector=vector)
        self.assertEqual(first["action"], "created")
        self.assertEqual(second["action"], "duplicate")

    def test_unrelated_entry_kept(self):
        if not self.dim:
            self.skipTest("向量不可用")
        a = tools.memory_note("第一条", kind="experience",
                              query_vector=self._vec(1.0))
        b = tools.memory_note("完全不同的另一件事",
                              kind="cognition", subject="测试主体",
                              query_vector=self._vec(0.0, 1.0))
        self.assertEqual(a["action"], "created")
        self.assertEqual(b["action"], "created")


class TestNoteIndexing(NoteCase):
    def test_new_entry_is_searchable(self):
        tools.memory_note("这是一条独一无二的测试文本甲乙丙", kind="experience")
        rows = self.db.search_fts("独一无二的测试文本", limit=5)
        self.assertTrue(rows, "新写入的条目必须能立刻被 FTS 检索到")


if __name__ == "__main__":
    unittest.main()

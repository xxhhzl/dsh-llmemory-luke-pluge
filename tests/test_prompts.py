# -*- coding: utf-8 -*-
"""prompt.md 与源码同步性检查。

提示词唯一来源是 src/，prompt.md 由 scripts/export_prompts.py 生成；
这里守住两件事：① 文档没过期；② 四处提示词都被收录。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import consolidate  # noqa: E402
import insight  # noqa: E402
import jev  # noqa: E402
import summary  # noqa: E402

DOC = ROOT / "docs" / "prompt.md"


class TestPromptDoc(unittest.TestCase):
    def test_doc_exists(self):
        self.assertTrue(DOC.exists(), "prompt.md 缺失，跑 scripts/export_prompts.py")

    def test_doc_is_in_sync_with_source(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "export_prompts.py"), "--check"],
            cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr + "（改完提示词请重跑导出脚本）")

    def test_doc_contains_every_prompt(self):
        text = DOC.read_text(encoding="utf-8")
        for prompt, label in (
            (summary.SUMMARY_SYSTEM_PROMPT, "事件归档器"),
            (summary.RETRY_SUFFIX, "重试后缀"),
            (jev.SYSTEM_PROMPT, "事件边界判定器"),
            (consolidate.CONSOLIDATE_SYSTEM_PROMPT, "合并裁决器"),
            (insight.INDUCTION_SYSTEM_PROMPT, "洞见归纳器"),
        ):
            first_line = prompt.strip("\n").splitlines()[0]
            self.assertIn(first_line[:20], text, f"{label} 未被收录")
        for tool in ("memory_search", "memory_get", "memory_recent",
                     "memory_report", "memory_self_write"):
            self.assertIn(f"`{tool}` 工具定义", text, f"{tool} 的定义未被收录")

    def test_sections_use_required_heading(self):
        text = DOC.read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count("### 提示词来源："), 7,
                                "每条提示词都要有「### 提示词来源：…」标题")


if __name__ == "__main__":
    unittest.main()

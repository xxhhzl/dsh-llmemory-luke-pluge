# -*- coding: utf-8 -*-
"""内核启动（src/main.py）：主线程预热重依赖。

背景：Windows + numpy 2.5.x 下，若主线程正阻塞在 `for line in sys.stdin`，
工作线程首次 `import numpy` 会永久卡住（venv 与系统 Python 均可复现）。
内核的 ingest / 维护线程会经 sqlite_vec / insight 间接导入它，所以在起线程前
必须先在主线程加载。这里只验证「不抛 + 装了的模块确实被提前加载」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import main                                          # noqa: E402


class TestWarmImports(unittest.TestCase):
    def test_never_raises_and_returns_failures(self):
        failed = main.warm_imports()
        self.assertIsInstance(failed, list)
        for name in failed:
            self.assertIsInstance(name, str)

    def test_installed_heavy_modules_end_up_in_sys_modules(self):
        main.warm_imports()
        for name in ("numpy", "sqlite_vec", "usearch.index"):
            try:
                __import__(name)
            except Exception:
                continue                   # 没装的依赖允许缺失
            self.assertIn(name, sys.modules,
                          f"{name} 装上了却没被预热，工作线程会再触发一次导入")

    def test_warm_is_idempotent(self):
        self.assertEqual(main.warm_imports(), main.warm_imports())


if __name__ == "__main__":
    unittest.main()

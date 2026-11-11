# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts" / "check_public_snapshot.py"
SPEC = importlib.util.spec_from_file_location("check_public_snapshot", PATH)
snapshot = importlib.util.module_from_spec(SPEC)
sys.modules["check_public_snapshot"] = snapshot
assert SPEC.loader is not None
SPEC.loader.exec_module(snapshot)


class TestPublicSnapshot(unittest.TestCase):
    def test_forbidden_paths_and_text_are_absent(self):
        self.assertEqual(snapshot.collect_errors(), [])


if __name__ == "__main__":
    unittest.main()

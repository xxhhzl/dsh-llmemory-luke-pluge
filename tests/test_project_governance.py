# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


preflight = load_script("preflight")
consistency = load_script("check_project_consistency")


class TestPreflight(unittest.TestCase):
    def test_overall_priority(self):
        Check = preflight.Check
        self.assertEqual(preflight.overall([Check("a", "PASS", "")]), "PASS")
        self.assertEqual(preflight.overall([Check("a", "DEGRADED", "")]), "DEGRADED")
        self.assertEqual(preflight.overall([
            Check("a", "DEGRADED", ""), Check("b", "FAIL", "")]), "FAIL")

    def test_optional_package_failure_is_degraded(self):
        with mock.patch.object(preflight.importlib, "import_module",
                               side_effect=ImportError("missing")):
            result = preflight._package("optional_x", "optional-x", optional=True)
        self.assertEqual(result.status, "DEGRADED")


class TestConsistency(unittest.TestCase):
    def test_registered_tools_are_the_seven_public_tools(self):
        self.assertEqual(consistency.registered_tools(), [
            "memory_search", "memory_get", "memory_recent", "memory_report",
            "memory_feedback", "memory_self_write", "memory_note",
        ])


if __name__ == "__main__":
    unittest.main()

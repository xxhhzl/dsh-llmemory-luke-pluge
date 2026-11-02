# -*- coding: utf-8 -*-
"""annotate/annotate.py 的单测：候选选择、kappa、读写往返。"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

_SPEC = importlib.util.spec_from_file_location("annotate_tool", ROOT / "annotate" / "annotate.py")
annotate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(annotate)

from segmenter import Decision  # noqa: E402


def message(index, role="user", content=None):
    return {
        "msg_id": f"m{index}",
        "seq": index,
        "role": role,
        "content": content if content is not None else f"内容 {index}",
        "ts": f"2026-10-20T09:0{index}:00+08:00",
    }


def decision(index, value):
    return Decision(message_id=f"m{index}", role="user", seq=index,
                    decision=value, source="rule", stage="g0",
                    boundary_type="session" if value == "SPLIT" else "")


class TestKappa(unittest.TestCase):
    def test_perfect_agreement(self):
        self.assertEqual(annotate.cohen_kappa(["B", "N", "B"], ["B", "N", "B"]), 1.0)

    def test_chance_level(self):
        # 各 50% B/N，但完全反着标 → kappa 0
        self.assertEqual(annotate.cohen_kappa(["B", "B", "N", "N"],
                                              ["B", "N", "N", "B"]), 0.0)

    def test_ignores_unlabelled(self):
        self.assertEqual(annotate.cohen_kappa(["B", "", "N"], ["B", "N", "N"]), 1.0)

    def test_empty(self):
        self.assertEqual(annotate.cohen_kappa([], []), 0.0)


class TestSelectCandidates(unittest.TestCase):
    def setUp(self):
        self.messages = [message(i, "user" if i % 2 == 0 else "assistant")
                         for i in range(8)]
        self.decisions = [decision(i, "SPLIT" if i in (0, 3) else "CONTINUE")
                          for i in range(8)]

    def test_positives_and_neighbors(self):
        items = annotate.select_candidates(
            self.messages, self.decisions, radius=1, extra_negatives=2,
            max_items=100, seed=1)
        ids = {item["msg_id"] for item in items}
        self.assertTrue({"m0", "m3"} <= ids)          # 正样本
        self.assertTrue({"m1", "m2", "m4"} <= ids)    # 邻接负样本
        self.assertLessEqual(len(items), 100)

    def test_fields_and_labels_blank(self):
        items = annotate.select_candidates(
            self.messages, self.decisions, radius=2, extra_negatives=1, seed=1)
        item = items[0]
        for key in annotate.FIELDS:
            self.assertIn(key, item)
        self.assertEqual(item["label_a"], "")
        self.assertEqual(item["label_b"], "")
        self.assertIn("current", item)

    def test_event_ids(self):
        items = annotate.select_candidates(
            self.messages, self.decisions, radius=1, extra_negatives=0, seed=1)
        by_id = {item["msg_id"]: item for item in items}
        self.assertEqual(by_id["m0"]["event_id"], 1)
        self.assertEqual(by_id["m3"]["event_id"], 2)

    def test_context_truncated(self):
        messages = [message(0, "user", "头" * 500 + "尾" * 500), message(1)]
        decisions = [decision(0, "SPLIT"), decision(1, "CONTINUE")]
        items = annotate.select_candidates(messages, decisions, radius=1,
                                           extra_negatives=0, seed=1)
        self.assertIn("省略", items[0]["current"])


class TestDisplay(unittest.TestCase):
    def test_block_wraps_and_has_separator(self):
        lines = annotate._format_block("current", "字" * 300, current=True, width=40)
        self.assertTrue(lines[0].startswith(">> current"))
        self.assertGreater(len(lines), 2)
        self.assertTrue(all("│" in line for line in lines))
        self.assertTrue(lines[-1].strip().endswith("│"))

    def test_block_missing_value(self):
        lines = annotate._format_block("prev_2", "", width=60)
        self.assertIn("（无）", lines[0])

    def test_show_separates_messages_with_blank_line(self):
        item = {
            "sample_id": "0001", "seq": 0, "role": "user",
            "model_decision": "SPLIT", "p_split": 0.9, "source": "jev/g1",
            "event_id": 1, "label_a": "",
            "prev_2": "", "prev_1": "user: 上一条", "current": "user: 当前",
            "next_1": "assistant: 下一条", "next_2": "",
        }
        buffer = io.StringIO()
        original_clear = annotate._clear
        annotate._clear = lambda: None
        try:
            with redirect_stdout(buffer):
                annotate._show(item, 0, 1, "label_a")
        finally:
            annotate._clear = original_clear
        text = buffer.getvalue()
        self.assertGreaterEqual(text.count("\n\n"), 4)     # 5 个块之间的空行
        self.assertIn(">> current", text)
        self.assertIn("│", text)


class TestIO(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".tmp" / "annotate_test"
        base.mkdir(parents=True, exist_ok=True)
        self.folder = base / uuid.uuid4().hex
        self.folder.mkdir()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_roundtrip_jsonl(self):
        items = [{"sample_id": "0001", "label_a": "B"},
                 {"sample_id": "0002", "label_a": ""}]
        path = self.folder / "sample.jsonl"
        annotate.save_items(path, items)
        loaded = annotate.load_items(path)
        self.assertEqual(loaded, items)

    def test_write_outputs_csv_header(self):
        items = [{"sample_id": "0001", "msg_id": "m1", "label_a": "", "label_b": ""}]
        base = self.folder / "out"
        jsonl, csv_path = annotate._write_outputs(items, base)
        self.assertTrue(jsonl.exists() and csv_path.exists())
        header = csv_path.read_text(encoding="utf-8-sig").splitlines()[0]
        for field in annotate.FIELDS:
            self.assertIn(field, header)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""摘要器（src/summary.py）单测：输入构造、输出解析、重试、外壳传输。

不联网：外壳调用用 FakeSummaryTransport / 假 LlmClient。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from summary import (
    EventMeta,
    FakeSummaryTransport,
    ShellSummaryTransport,
    SummaryError,
    build_messages,
    extract,
    parse_result,
)


def msg(role, content):
    return {"role": role, "content": content, "ts": "2026-11-02T10:00:00+08:00"}


META = EventMeta(event_id=7, session_id="s_1", message_count=2,
                 first_at="2026-11-02T10:00:00+08:00",
                 last_at="2026-11-02T10:05:00+08:00", tokens=120)


class TestBuildMessages(unittest.TestCase):
    def test_header_and_transcript(self):
        messages = [msg("user", "把边框改细"), msg("assistant", "已改成 1px")]
        payload = build_messages(META, messages)
        self.assertEqual(payload[0]["role"], "system")
        self.assertIn("事件归档器", payload[0]["content"])
        user = payload[1]["content"]
        self.assertIn("#7", user)
        self.assertIn("2 条消息", user)
        self.assertIn("120 token", user)
        self.assertIn("user: 把边框改细", user)
        self.assertIn("assistant: 已改成 1px", user)
        self.assertNotIn("已裁剪", user)

    def test_long_transcript_is_trimmed(self):
        messages = [msg("user", "开头" * 200)] + \
                   [msg("assistant", f"第{i}条" + "内容" * 100) for i in range(30)] + \
                   [msg("assistant", "结尾" * 200)]
        payload = build_messages(META, messages, max_input_chars=2000)
        user = payload[1]["content"]
        self.assertIn("（已裁剪）", user)
        self.assertIn("中间省略", user)
        self.assertTrue(user.endswith("结尾" * 100))   # 尾部保留

    def test_previous_summary_reserved(self):
        payload = build_messages(META, [msg("user", "x")], previous_summary="上一版摘要")
        self.assertIn("[已有摘要]", payload[1]["content"])
        self.assertIn("上一版摘要", payload[1]["content"])


class TestParse(unittest.TestCase):
    def test_valid_payload(self):
        raw = json.dumps({
            "title": "调 drawio 边框",
            "summary": "把边框改成 1px，导出成功。",
            "experiences": [{"context": "导出前", "content": "统一 strokeWidth",
                             "kind": "technique", "importance": 4,
                             "confidence": 0.9, "cues": ["drawio", "svg"]}],
            "cognitions": [{"subject": "用户", "content": "偏好中文回复",
                            "kind": "preference", "importance": 5,
                            "confidence": 0.95, "cues": ["偏好"]}],
        }, ensure_ascii=False)
        result = parse_result(raw)
        self.assertTrue(result.ok)
        self.assertEqual(result.title, "调 drawio 边框")
        self.assertEqual(len(result.experiences), 1)
        self.assertEqual(result.experiences[0]["kind"], "technique")
        self.assertEqual(result.experiences[0]["cues"], ("drawio", "svg"))
        self.assertEqual(result.cognitions[0]["subject"], "用户")
        self.assertFalse(result.needs_review)

    def test_fenced_json(self):
        raw = '```json\n{"title": "t", "summary": "s"}\n```'
        result = parse_result(raw)
        self.assertTrue(result.ok)
        self.assertEqual(result.summary, "s")

    def test_missing_summary_fails(self):
        result = parse_result('{"title": "只有标题"}')
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "empty_summary")

    def test_garbage_fails(self):
        result = parse_result("我觉得可以")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "json_parse")

    def test_lengths_are_clamped(self):
        raw = json.dumps({"title": "标" * 50, "summary": "摘" * 500}, ensure_ascii=False)
        result = parse_result(raw)
        self.assertEqual(len(result.title), 24)
        self.assertEqual(len(result.summary), 200)
        self.assertTrue(result.truncated)

    def test_invalid_items_dropped_and_flagged(self):
        raw = json.dumps({
            "title": "t", "summary": "s",
            "experiences": ["不是对象", {"context": "缺 content"}],
            "cognitions": {"not": "a list"},
        }, ensure_ascii=False)
        result = parse_result(raw)
        self.assertTrue(result.ok)
        self.assertEqual(result.experiences, ())
        self.assertTrue(result.needs_review)

    def test_values_are_normalized(self):
        raw = json.dumps({
            "title": "t", "summary": "s",
            "experiences": [{"content": "x", "kind": "unknown",
                             "importance": 9, "confidence": 5, "cues": ["a"] * 20}],
            "cognitions": [{"subject": "用户", "content": "y", "kind": "weird",
                            "importance": 0, "confidence": -1}],
        }, ensure_ascii=False)
        result = parse_result(raw)
        exp = result.experiences[0]
        self.assertEqual(exp["kind"], "technique")
        self.assertEqual(exp["importance"], 5)
        self.assertEqual(exp["confidence"], 1.0)
        self.assertEqual(len(exp["cues"]), 10)
        self.assertEqual(result.cognitions[0]["kind"], "fact")
        self.assertEqual(result.cognitions[0]["importance"], 1)
        self.assertEqual(result.cognitions[0]["confidence"], 0.0)

    def test_actions_ignored_without_allow(self):
        raw = json.dumps({"title": "t", "summary": "s",
                          "experiences": [{"content": "x", "action": "supersede",
                                           "target_id": 3}]}, ensure_ascii=False)
        result = parse_result(raw)
        self.assertEqual(result.experiences[0]["action"], "new")
        self.assertIsNone(result.experiences[0]["target_id"])

    def test_actions_allowed(self):
        raw = json.dumps({
            "title": "t", "summary": "s",
            "experiences": [{"content": "x", "action": "supersede", "target_id": 3}],
            "cognitions": [{"subject": "用户", "content": "y", "action": "invalidate"}],
        }, ensure_ascii=False)
        result = parse_result(raw, allow_actions=True)
        self.assertEqual(result.experiences[0]["action"], "supersede")
        self.assertEqual(result.experiences[0]["target_id"], 3)
        self.assertEqual(len(result.cognitions), 0)      # invalidate 缺 reason → 丢弃
        self.assertTrue(result.needs_review)


class TestExtract(unittest.TestCase):
    def test_retry_once_then_success(self):
        good = json.dumps({"title": "t", "summary": "s"}, ensure_ascii=False)
        transport = FakeSummaryTransport(["不是 json", good])
        result = extract(META, [msg("user", "x")], transport)
        self.assertTrue(result.ok)
        self.assertEqual(len(transport.calls), 2)
        second_system = transport.calls[1][0]["content"]
        self.assertIn("重试", second_system)

    def test_two_failures_return_not_ok(self):
        transport = FakeSummaryTransport(["坏", "还是坏"])
        result = extract(META, [msg("user", "x")], transport)
        self.assertFalse(result.ok)
        self.assertTrue(result.needs_review)
        self.assertEqual(len(transport.calls), 2)

    def test_transport_error_returns_not_ok(self):
        transport = FakeSummaryTransport([SummaryError("timeout")])
        result = extract(META, [msg("user", "x")], transport)
        self.assertFalse(result.ok)
        self.assertIn("timeout", result.error)


class TestShellTransport(unittest.TestCase):
    def test_uses_purpose_and_json_format(self):
        class Client:
            def __init__(self):
                self.calls = []

            def request(self, messages, **kwargs):
                self.calls.append((messages, kwargs))

                class R:
                    content = '{"title":"t","summary":"s"}'
                return R()

        client = Client()
        content = ShellSummaryTransport(client).complete([{"role": "user", "content": "x"}])
        self.assertEqual(content, '{"title":"t","summary":"s"}')
        _, kwargs = client.calls[0]
        self.assertEqual(kwargs["purpose"], "summary")
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_llm_error_becomes_summary_error(self):
        class Broken:
            def request(self, messages, **kwargs):
                from llm import LlmError
                raise LlmError("boom")

        with self.assertRaises(SummaryError):
            ShellSummaryTransport(Broken()).complete([])


if __name__ == "__main__":
    unittest.main()

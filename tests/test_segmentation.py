# -*- coding: utf-8 -*-
"""切分脚手架的单测：规则、判定门、装配不变量、解析与配置。

不联网：G1/G2 用 FakeTransport。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent          # .../tests
SRC_DIR = TESTS_DIR.parent / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(TESTS_DIR))

from segmentation.dsh_sessions import event_to_message
from segmentation.env import _parse_env_file, ApiConfig
from segmentation.direct import DecisionTransport, parse_choice_answer
from jev import (
    CRITERIA,
    FakeTransport,
    JudgeError,
    ShellTransport,
    build_judge_messages,
    build_state,
    gap_minutes,
    parse_judgement,
    truncate,
)
from segmenter import (
    Event,
    SegmentConfig,
    Segmenter,
    SegmentationResult,
    check_rules,
    estimate_tokens,
    summarize_event,
    validate,
)
from segmentation.run_demo import boundary_scores, load_conversations

FIXTURE = TESTS_DIR / "fixtures" / "segmentation_sample.jsonl"


def msg(msg_id, seq, role, content, ts="2026-10-20T09:00:00+08:00"):
    return {"msg_id": msg_id, "seq": seq, "role": role, "content": content, "ts": ts}


class TestRules(unittest.TestCase):
    def test_first_message_starts_event(self):
        hit = check_rules(msg("m1", 0, "user", "你好"), None,
                          has_open_event=False, open_event_messages=0)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.boundary_type, "session")

    def test_time_gap_is_soft_by_default(self):
        prev = msg("m1", 0, "user", "a", "2026-10-20T09:00:00+08:00")
        cur = msg("m2", 1, "user", "b", "2026-10-20T12:30:00+08:00")
        self.assertAlmostEqual(gap_minutes(prev["ts"], cur["ts"]), 210.0, places=1)
        hit = check_rules(cur, prev, has_open_event=True, open_event_messages=1)
        self.assertEqual(hit.boundary_type, "time_soft")
        self.assertTrue(hit.soft)

    def test_time_gap_hard_when_gap_soft_disabled(self):
        prev = msg("m1", 0, "user", "a", "2026-10-20T09:00:00+08:00")
        cur = msg("m2", 1, "user", "b", "2026-10-20T12:30:00+08:00")
        hit = check_rules(cur, prev, has_open_event=True, open_event_messages=1,
                          gap_soft=False)
        self.assertEqual(hit.boundary_type, "time")
        self.assertFalse(hit.soft)

    def test_time_soft_yields_to_hard_rules(self):
        # 长间隔 + 显式转折词：硬规则优先，仍然直接切
        prev = msg("m1", 0, "user", "a", "2026-10-20T09:00:00+08:00")
        cur = msg("m2", 1, "user", "换个话题", "2026-10-20T12:30:00+08:00")
        hit = check_rules(cur, prev, has_open_event=True, open_event_messages=1)
        self.assertEqual(hit.boundary_type, "explicit")

    def test_length_cap(self):
        hit = check_rules(msg("m2", 1, "assistant", "继续"), None,
                          has_open_event=True, open_event_messages=40,
                          max_messages=40)
        self.assertEqual(hit.boundary_type, "length")

    def test_explicit_marker(self):
        hit = check_rules(msg("m2", 1, "user", "换个话题，聊聊发布"), None,
                          has_open_event=True, open_event_messages=2)
        self.assertEqual(hit.boundary_type, "explicit")

    def test_explicit_marker_ignored_for_assistant(self):
        # assistant 复述用户原话（总结里出现「换个话题」）不算转折
        hit = check_rules(msg("m2", 1, "assistant", "你说「换个话题，聊聊发布」，我先列提纲"),
                          None, has_open_event=True, open_event_messages=2)
        self.assertIsNone(hit)

    def test_assistant_with_marker_stays_in_event(self):
        prev = msg("m1", 0, "user", "继续", "2026-10-20T09:00:00+08:00")
        cur = msg("m2", 1, "assistant", "好的，另起一段说明「新任务」的定义",
                  "2026-10-20T09:01:00+08:00")
        hit = check_rules(cur, prev, has_open_event=True, open_event_messages=1)
        self.assertEqual(hit.boundary_type, "reply")

    def test_tool_is_not_judged(self):
        hit = check_rules(msg("m2", 1, "tool", "ok"), None,
                          has_open_event=True, open_event_messages=2)
        self.assertEqual(hit.boundary_type, "tool")

    def test_token_budget_splits(self):
        hit = check_rules(msg("m2", 1, "user", "继续"), None,
                          has_open_event=True, open_event_messages=3,
                          open_event_tokens=8000, max_event_tokens=8000)
        self.assertEqual(hit.boundary_type, "tokens")

    def test_message_cap_is_emergency_only(self):
        hit = check_rules(msg("m2", 1, "user", "继续"), None,
                          has_open_event=True, open_event_messages=200,
                          open_event_tokens=100, max_event_tokens=8000,
                          max_messages=200)
        self.assertEqual(hit.boundary_type, "length")

    def test_normal_message_defers_to_model(self):
        self.assertIsNone(check_rules(msg("m2", 1, "user", "边框改成 2px"), None,
                                      has_open_event=True, open_event_messages=2))

    def test_assistant_reply_stays_in_event(self):
        prev = msg("m1", 0, "user", "把边框调细", "2026-10-20T09:00:00+08:00")
        cur = msg("m2", 1, "assistant", "已改成 1px", "2026-10-20T09:01:00+08:00")
        hit = check_rules(cur, prev, has_open_event=True, open_event_messages=1)
        self.assertEqual(hit.boundary_type, "reply")

    def test_assistant_reply_rule_can_be_disabled(self):
        prev = msg("m1", 0, "user", "把边框调细", "2026-10-20T09:00:00+08:00")
        cur = msg("m2", 1, "assistant", "已改成 1px", "2026-10-20T09:01:00+08:00")
        self.assertIsNone(check_rules(cur, prev, has_open_event=True,
                                      open_event_messages=1,
                                      assistant_reply_continues=False))

    def test_late_assistant_reply_not_forced(self):
        prev = msg("m1", 0, "user", "把边框调细", "2026-10-20T09:00:00+08:00")
        cur = msg("m2", 1, "assistant", "已改成 1px", "2026-10-20T09:40:00+08:00")
        self.assertIsNone(check_rules(cur, prev, has_open_event=True,
                                      open_event_messages=1))

    def test_assistant_after_tool_continues(self):
        prev = msg("m1", 0, "tool", "命令输出")
        cur = msg("m2", 1, "assistant", "根据输出可以看到")
        hit = check_rules(cur, prev, has_open_event=True, open_event_messages=1)
        self.assertEqual(hit.boundary_type, "tool_followup")

    def test_tool_followup_rule_can_be_disabled(self):
        prev = msg("m1", 0, "tool", "命令输出")
        cur = msg("m2", 1, "assistant", "根据输出可以看到")
        self.assertIsNone(check_rules(cur, prev, has_open_event=True,
                                      open_event_messages=1,
                                      tool_followup_continues=False))


class TestJudgeHelpers(unittest.TestCase):
    def test_truncate_head_tail(self):
        text = "头" * 300 + "中" * 100 + "尾" * 300
        out = truncate(text, 200, 200)
        self.assertTrue(out.startswith("头" * 200))
        self.assertTrue(out.endswith("尾" * 200))
        self.assertIn("省略 300 字", out)

    def test_short_text_untouched(self):
        self.assertEqual(truncate("短消息", 200, 200), "短消息")

    def test_parse_json_judgement(self):
        self.assertEqual(parse_judgement('{"decision":"SPLIT","confidence":0.83}'),
                         ("SPLIT", 0.83))

    def test_continue_confidence_is_flipped(self):
        # confidence 是「所选选项的把握」：CONTINUE 0.72 → p_split 0.28
        self.assertEqual(parse_judgement('{"decision":"CONTINUE","confidence":0.72}'),
                         ("CONTINUE", 0.28))

    def test_parse_fenced_json(self):
        raw = '```json\n{"decision": "SPLIT", "confidence": 0.9}\n```'
        self.assertEqual(parse_judgement(raw), ("SPLIT", 0.9))

    def test_parse_bare_label(self):
        self.assertEqual(parse_judgement("SPLIT"), ("SPLIT", 0.5))

    def test_parse_rejects_garbage(self):
        with self.assertRaises(JudgeError):
            parse_judgement("我觉得应该可以吧")

    def test_parse_rejects_unknown_decision(self):
        with self.assertRaises(JudgeError):
            parse_judgement('{"decision":"MAYBE","confidence":0.9}')


class TestDshConverter(unittest.TestCase):
    """a：只保留 source.kind == "user" 的真实用户输入。"""

    @staticmethod
    def _user(text, source_kind="user"):
        data = {"content": [{"type": "text", "text": text}], "id": "m1"}
        if source_kind is not None:
            data["source"] = {"kind": source_kind}
        return {"type": "user/message", "time": 1792994633161, "data": data}

    def test_real_user_kept(self):
        message = event_to_message(self._user("分析当前项目"), "s1", 0)
        self.assertIsNotNone(message)
        self.assertEqual(message["role"], "user")
        self.assertEqual(message["content"], "分析当前项目")
        self.assertEqual(message["seq"], 0)

    def test_synthetic_user_messages_dropped(self):
        for kind in ("time-context", "runtime-context", "model-selection",
                     "skill-catalog", "compact-checkpoint", "goal",
                     "user-approval", "agent-message", "tool-jobs"):
            self.assertIsNone(event_to_message(self._user("x", kind), "s1", 0), kind)

    def test_missing_source_kind_is_kept(self):
        self.assertIsNotNone(event_to_message(self._user("老日志", None), "s1", 0))

    def test_assistant_text_kept_reasoning_skipped(self):
        event = {"type": "assistant/message", "time": 1792994641962, "data": {
            "message": {"id": "a1", "content": [
                {"type": "reasoning", "text": "内部推理"},
                {"type": "text", "text": "对外回答"},
            ]}}}
        message = event_to_message(event, "s1", 3)
        self.assertEqual(message["role"], "assistant")
        self.assertEqual(message["content"], "对外回答")
        self.assertEqual(message["seq"], 3)

    def test_tool_result_kept(self):
        event = {"type": "tool/result", "time": 1792994641962, "data": {
            "toolCallId": "call_1",
            "message": {"content": [{"type": "text", "text": "命令输出"}]}}}
        message = event_to_message(event, "s1", 1)
        self.assertEqual(message["role"], "tool")
        self.assertEqual(message["msg_id"], "call_1")

    def test_system_event_ignored(self):
        event = {"type": "system/message", "time": 1, "data": {"content": []}}
        self.assertIsNone(event_to_message(event, "s1", 0))

    def test_tool_call_kept_with_truncated_args(self):
        long_args = '{"command":"' + "x" * 500 + '"}'
        event = {"type": "assistant/message", "time": 1792994641962, "data": {
            "message": {"id": "a1", "content": [
                {"type": "text", "text": "我来读取文件"},
                {"type": "tool-call", "name": "read", "arguments": long_args},
            ]}}}
        message = event_to_message(event, "s1", 0)
        self.assertIn("我来读取文件", message["content"])
        self.assertIn("[tool-call] read", message["content"])
        call_line = message["content"].splitlines()[-1]
        self.assertLessEqual(len(call_line), len("[tool-call] read ") + 200 + 1)
        self.assertTrue(call_line.endswith("…"))

    def test_tool_call_only_assistant_is_kept(self):
        event = {"type": "assistant/message", "time": 1792994641962, "data": {
            "message": {"id": "a2", "content": [
                {"type": "reasoning", "text": "内部推理"},
                {"type": "tool-call", "name": "pwsh", "arguments": "{}"},
            ]}}}
        message = event_to_message(event, "s1", 0)
        self.assertIsNotNone(message)
        self.assertEqual(message["content"], "[tool-call] pwsh {}")

    def test_content_is_not_pre_truncated(self):
        text = "字" * 5000
        event = self._user(text)
        message = event_to_message(event, "s1", 0)
        self.assertEqual(len(message["content"]), 5000)

    def test_build_state_truncates_all_messages(self):
        state = build_state(
            {"role": "user", "content": "头" * 300 + "中" * 300 + "尾" * 300,
             "ts": "2026-10-20T09:00:00+08:00"},
            None, event_message_count=1,
            history=[{"role": "assistant", "content": "甲" * 1000}],
        )
        self.assertLessEqual(len(state["current"]["content"]), 420)
        self.assertLessEqual(len(state["recent"][0]["content"]), 420)
        self.assertIn("省略", state["current"]["content"])


class TestContextModes(unittest.TestCase):
    """工具信息的三种呈现方式（raw / generic / digest）。"""

    @staticmethod
    def _assistant():
        return {"role": "assistant",
                "content": '先跑测试\n[tool-call] pwsh {"command":"python -m unittest","path":"tests"}',
                "ts": "2026-10-20T09:00:00+08:00"}

    def test_raw_keeps_tool_call_text(self):
        state = build_state(self._assistant(), None, event_message_count=1, mode="raw")
        self.assertIn("pwsh", state["current"]["content"])
        self.assertIn("python -m unittest", state["current"]["content"])

    def test_generic_replaces_tool_call(self):
        state = build_state(self._assistant(), None, event_message_count=1, mode="generic")
        content = state["current"]["content"]
        self.assertIn("调用工具", content)
        self.assertNotIn("pwsh", content)

    def test_digest_keeps_tool_name_and_key_args(self):
        state = build_state(self._assistant(), None, event_message_count=1, mode="digest")
        content = state["current"]["content"]
        self.assertIn("[tool-call] pwsh |", content)
        self.assertIn("command=python -m unittest", content)
        self.assertNotIn('{"command"', content)      # 原始 JSON 不再出现

    def test_digest_tool_result_carries_status(self):
        message = {"role": "tool", "content": "Traceback: boom",
                   "error": {"code": "FS_EDIT_NOT_FOUND"},
                   "ts": "2026-10-20T09:00:00+08:00"}
        state = build_state(message, None, event_message_count=1, mode="digest")
        self.assertTrue(state["current"]["content"].startswith(
            "[tool-result] error=FS_EDIT_NOT_FOUND"))

    def test_digest_ok_status(self):
        message = {"role": "tool", "content": "45 passed",
                   "ts": "2026-10-20T09:00:00+08:00"}
        state = build_state(message, None, event_message_count=1, mode="digest")
        self.assertTrue(state["current"]["content"].startswith("[tool-result] ok 9B | 45 passed"))


class TestDecisionProtocol(unittest.TestCase):
    """研究脚手架直连 /decisions 的协议：请求体与应答解析（不进产品）。"""

    def _config(self, base="https://api.example/v1"):
        return ApiConfig(base_url=base, key="k" * 10)

    def test_decisions_url_variants(self):
        self.assertEqual(self._config("https://api.example").decisions_url,
                         "https://api.example/v1/decisions")
        self.assertEqual(self._config("https://api.example/v1").decisions_url,
                         "https://api.example/v1/decisions")
        self.assertEqual(self._config("https://api.example/v1/decisions").decisions_url,
                         "https://api.example/v1/decisions")
        self.assertEqual(self._config("https://api.example/v1/systemone").decisions_url,
                         "https://api.example/v1/systemone")

    def test_payload_shape(self):
        transport = DecisionTransport(self._config(), think_budget=512)
        payload = transport._payload({"current": {"role": "user", "content": "x"}}, think=False)
        self.assertEqual(payload["think_mode"], "never")
        self.assertEqual(payload["think_budget"], 512)
        self.assertFalse(payload["none_check"])
        self.assertFalse(payload["allow_none"])
        question = payload["questions"]["boundary"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(set(question["criteria"]), set(CRITERIA))

        think_payload = transport._payload({}, think=True)
        self.assertEqual(think_payload["think_mode"], "always")

    def test_parse_answer(self):
        answer = {"choice": "SPLIT", "confidence": 0.98,
                  "probabilities": {"CONTINUE": 0.01, "SPLIT": 0.99}}
        self.assertEqual(parse_choice_answer(answer), ("SPLIT", 0.99))

    def test_parse_answer_falls_back_to_confidence(self):
        answer = {"choice": "CONTINUE", "confidence": 0.8, "probabilities": {}}
        self.assertEqual(parse_choice_answer(answer), ("CONTINUE", 0.2))

    def test_parse_answer_rejects_none(self):
        with self.assertRaises(JudgeError):
            parse_choice_answer({"choice": None, "probabilities": {}})

    def test_parse_answer_rejects_unknown_label(self):
        with self.assertRaises(JudgeError):
            parse_choice_answer({"choice": "MAYBE", "probabilities": {}})


class TestShellTransport(unittest.TestCase):
    """产品路径：模型调用走外壳代理，内核只发 llm.request。"""

    class _Client:
        def __init__(self, content):
            self.content = content
            self.calls = []

        def request(self, messages, **kwargs):
            from llm import LlmResult
            self.calls.append({"messages": messages, **kwargs})
            return LlmResult(request_id="llm_1", content=self.content,
                             model="fake-model", usage={})

    def test_judge_uses_system_prompt_and_json(self):
        client = self._Client('{"decision":"SPLIT","confidence":0.9}')
        result = ShellTransport(client).judge(
            {"current": {"role": "user", "content": "换个任务"}})
        self.assertEqual(result.decision, "SPLIT")
        self.assertEqual(result.p_split, 0.9)
        call = client.calls[0]
        self.assertEqual(call["purpose"], "jev")
        self.assertEqual(call["response_format"], {"type": "json_object"})
        self.assertFalse(call["think"])
        self.assertEqual(call["messages"][0]["role"], "system")
        self.assertIn("CONTINUE", call["messages"][0]["content"])
        self.assertEqual(call["messages"][1]["role"], "user")

    def test_think_flag_passed_and_stage(self):
        client = self._Client('{"decision":"CONTINUE","confidence":0.8}')
        result = ShellTransport(client).judge({}, think=True)
        self.assertTrue(client.calls[0]["think"])
        self.assertEqual(result.stage, "g2")
        self.assertEqual(result.p_split, 0.2)

    def test_llm_error_becomes_judge_error(self):
        class Broken:
            def request(self, messages, **kwargs):
                from llm import LlmError
                raise LlmError("timeout")

        with self.assertRaises(JudgeError):
            ShellTransport(Broken()).judge({})

    def test_build_judge_messages_carries_state_as_json(self):
        messages = build_judge_messages({"current": {"role": "user", "content": "x"}})
        self.assertEqual(json.loads(messages[1]["content"])["current"]["content"], "x")


class TestGate(unittest.TestCase):
    def _run(self, decide, role="user", think=True, **cfg):
        transport = FakeTransport(decision_fn=decide)
        seg = Segmenter(transport=transport,
                        config=SegmentConfig(use_autothink=think, **cfg))
        messages = [msg("m1", 0, "user", "开始"),
                    msg("m1b", 1, "assistant", "好的"),
                    msg("m2", 2, role, "继续做")]
        return seg.run(messages), transport

    @staticmethod
    def _by_content(mapping):
        """按当前消息内容返回分数；mapping: {片段: (fast, think)}。"""
        def decide(state, is_think):
            content = state["current"]["content"]
            for key, (fast, deep) in mapping.items():
                if key in content:
                    return deep if is_think else fast
            return 0.05
        return decide

    def test_low_confidence_continues(self):
        result, transport = self._run(self._by_content({"继续做": (0.1, 0.1)}))
        decision = result.decisions[-1]
        self.assertEqual(decision.decision, "CONTINUE")
        self.assertEqual(decision.source, "jev")
        self.assertFalse(decision.needs_review)

    def test_high_confidence_splits(self):
        result, _ = self._run(self._by_content({"继续做": (0.9, 0.9)}))
        self.assertEqual(result.decisions[-1].decision, "SPLIT")

    def test_assistant_needs_higher_threshold(self):
        decide = self._by_content({"继续做": (0.80, 0.80)})
        result_user, _ = self._run(decide, role="user", think=False)
        result_assistant, _ = self._run(decide, role="assistant", think=False)
        self.assertEqual(result_user.decisions[-1].decision, "SPLIT")
        self.assertEqual(result_assistant.decisions[-1].decision, "CONTINUE")
        self.assertTrue(result_assistant.decisions[-1].needs_review)

    def test_gray_band_goes_to_g2_and_resolves(self):
        decide = self._by_content({"继续做": (0.5, 0.9)})
        result, transport = self._run(decide, think=True)
        decision = result.decisions[-1]
        self.assertEqual(decision.stage, "g2")
        self.assertEqual(decision.source, "escalated")
        self.assertEqual(decision.decision, "SPLIT")
        # assistant 应答走 reply 规则不调模型；只有最后一条 user 过 G1 与 G2
        self.assertEqual([call["think"] for call in transport.calls], [False, True])

    def test_gray_band_still_gray_marks_review(self):
        decide = self._by_content({"继续做": (0.5, 0.5)})
        result, _ = self._run(decide, think=True)
        decision = result.decisions[-1]
        self.assertEqual(decision.stage, "g3")
        self.assertTrue(decision.needs_review)
        self.assertEqual(decision.decision, "CONTINUE")

    def test_transport_failure_falls_back(self):
        class Broken:
            def judge(self, state, think=False):
                raise JudgeError("timeout")

        seg = Segmenter(transport=Broken())
        result = seg.run([msg("m1", 0, "user", "开始"), msg("m2", 1, "user", "继续")])
        decision = result.decisions[-1]
        self.assertEqual(decision.source, "fallback")
        self.assertTrue(decision.needs_review)


class TestSoftGap(unittest.TestCase):
    """长间隔只作软先验：交模型判，不直接切。"""

    def _messages(self):
        return [msg("m1", 0, "user", "先把边框改细", "2026-10-20T09:00:00+08:00"),
                msg("m2", 1, "user", "再把阴影去掉", "2026-10-20T12:30:00+08:00")]

    def test_long_gap_asks_model_and_continues(self):
        transport = FakeTransport(scores=[0.05])
        seg = Segmenter(transport=transport,
                        config=SegmentConfig(use_autothink=False))
        result = seg.run(self._messages())
        self.assertEqual(len(result.events), 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("交模型判断", result.decisions[1].reason)
        self.assertEqual(result.decisions[1].decision, "CONTINUE")

    def test_long_gap_splits_when_model_says_split(self):
        transport = FakeTransport(scores=[0.92])
        seg = Segmenter(transport=transport,
                        config=SegmentConfig(use_autothink=False))
        result = seg.run(self._messages())
        self.assertEqual(len(result.events), 2)
        self.assertEqual(result.decisions[1].decision, "SPLIT")
        self.assertEqual(result.decisions[1].role, "user")

    def test_gap_hard_mode_skips_model(self):
        transport = FakeTransport()
        seg = Segmenter(transport=transport,
                        config=SegmentConfig(use_autothink=False, gap_soft=False))
        result = seg.run(self._messages())
        self.assertEqual(len(result.events), 2)
        self.assertEqual(len(transport.calls), 0)


class TestTokenBudget(unittest.TestCase):
    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("中文四字"), 4)
        self.assertEqual(estimate_tokens("abcde"), 2)      # ceil(5/4)

    def test_event_accumulates_and_splits_on_token_budget(self):
        messages = [
            msg("m1", 0, "user", "字" * 30),
            msg("m2", 1, "assistant", "字" * 30),
            msg("m3", 2, "user", "字" * 30),
        ]
        config = SegmentConfig(use_autothink=False, max_event_tokens=50)
        result = Segmenter(transport=FakeTransport(), config=config).run(messages)
        self.assertEqual(len(result.events), 2)
        self.assertTrue(any(d.boundary_type == "tokens" for d in result.decisions))
        self.assertTrue(all(event.tokens > 0 for event in result.events))

    def test_default_budget_is_128000(self):
        self.assertEqual(SegmentConfig().max_event_tokens, 128000)
        self.assertEqual(SegmentConfig().max_messages_per_event, 1000)
        self.assertEqual(SegmentConfig().token_scope, "dialog")

    def test_dialog_scope_ignores_tool_outputs(self):
        messages = [
            msg("m1", 0, "user", "跑一下测试"),
            msg("m2", 1, "tool", "日" * 5000),
            msg("m3", 2, "assistant", "测试通过"),
        ]
        config = SegmentConfig(use_autothink=False, max_event_tokens=100,
                               token_scope="dialog", max_messages_per_event=999)
        result = Segmenter(transport=FakeTransport(), config=config).run(messages)
        self.assertEqual(len(result.events), 1)
        self.assertFalse(any(d.boundary_type == "tokens" for d in result.decisions))

    def test_all_scope_counts_tool_outputs(self):
        messages = [
            msg("m1", 0, "user", "跑一下测试"),
            msg("m2", 1, "tool", "日" * 5000),
            msg("m3", 2, "assistant", "测试通过"),
        ]
        config = SegmentConfig(use_autothink=False, max_event_tokens=100,
                               token_scope="all", max_messages_per_event=999)
        result = Segmenter(transport=FakeTransport(), config=config).run(messages)
        # 超预算的判定落在 assistant 上，且后面没有 user → 吸附逻辑合并回当前事件
        self.assertEqual(len(result.events), 1)
        self.assertTrue(any(d.boundary_type == "tokens" and d.decision == "CONTINUE"
                            for d in result.decisions))
        self.assertTrue(any("无后续 user" in d.reason for d in result.decisions))


class TestSnapToUser(unittest.TestCase):
    """b：判定点不是 user 时，边界顺延到下一条 user。"""

    @staticmethod
    def _conv():
        return [
            msg("m1", 0, "user", "开始任务A", "2026-10-20T09:00:00+08:00"),
            msg("m2", 1, "assistant", "回答A", "2026-10-20T09:30:00+08:00"),  # 超过 reply 窗口
            msg("m3", 2, "tool", "工具输出", "2026-10-20T09:31:00+08:00"),
            msg("m4", 3, "user", "新任务B", "2026-10-20T09:32:00+08:00"),
        ]

    @staticmethod
    def _decide(state, think):
        return 0.9 if state["current"]["content"] in ("回答A", "新任务B") else 0.05

    def test_split_on_assistant_is_deferred_to_next_user(self):
        transport = FakeTransport(decision_fn=self._decide)
        config = SegmentConfig(use_autothink=False, snap_to_user=True)
        result = Segmenter(transport=transport, config=config).run(self._conv())

        self.assertEqual(result.boundaries(), [3])
        assistant = result.decisions[1]
        self.assertEqual(assistant.decision, "CONTINUE")
        self.assertIn("边界吸附", assistant.reason)
        self.assertEqual(result.decisions[2].boundary_type, "snap_wait")
        self.assertEqual(result.decisions[3].boundary_type, "snap")
        self.assertEqual(len(result.events), 2)

    def test_pending_snap_does_not_call_model_again(self):
        transport = FakeTransport(decision_fn=self._decide)
        config = SegmentConfig(use_autothink=False, snap_to_user=True)
        Segmenter(transport=transport, config=config).run(self._conv())
        self.assertEqual(len(transport.calls), 1)      # 只有 assistant 那一次

    def test_no_later_user_merges_into_current_event(self):
        messages = self._conv()[:2]
        transport = FakeTransport(decision_fn=self._decide)
        config = SegmentConfig(use_autothink=False, snap_to_user=True)
        result = Segmenter(transport=transport, config=config).run(messages)

        self.assertEqual(result.boundaries(), [])
        self.assertEqual(len(result.events), 1)
        self.assertIn("无后续 user", result.decisions[1].reason)

    def test_snap_disabled_keeps_old_behaviour(self):
        transport = FakeTransport(decision_fn=self._decide)
        config = SegmentConfig(use_autothink=False, snap_to_user=False)
        result = Segmenter(transport=transport, config=config).run(self._conv())

        self.assertIn(1, result.boundaries())          # assistant 自己开了新事件
        self.assertEqual(result.events[1].message_ids[0], "m2")


class TestAssembly(unittest.TestCase):
    def test_fixture_conversations(self):
        conversations = load_conversations(FIXTURE)
        segmenter = Segmenter(transport=FakeTransport())
        for row in conversations:
            result = segmenter.run(row["messages"])
            self.assertEqual(result.boundaries(), row["expected_boundaries"], row["id"])

    def test_system_messages_are_dropped(self):
        conversations = {row["id"]: row for row in load_conversations(FIXTURE)}
        row = conversations["conv-marker"]
        result = Segmenter(transport=FakeTransport()).run(row["messages"])
        covered = [mid for event in result.events for mid in event.message_ids]
        self.assertNotIn("m_300", covered)
        self.assertEqual(len(covered), len(row["messages"]) - 1)

    def test_validate_rejects_gap(self):
        bad = SegmentationResult(
            events=[Event(order=1, message_ids=["m1"], seqs=[0], closed=True),
                    Event(order=2, message_ids=["m3"], seqs=[2], closed=True)],
            decisions=[],
        )
        with self.assertRaises(AssertionError):
            validate(bad, [msg("m1", 0, "user", "a"), msg("m2", 1, "user", "b"),
                           msg("m3", 2, "user", "c")])

    def test_summary_placeholder(self):
        title, summary = summarize_event([msg("m1", 0, "user", "把边框调细"),
                                          msg("m2", 1, "assistant", "已改成 1px")])
        self.assertIn("边框", title)
        self.assertIn("2 条消息", summary)


class TestScoresAndEnv(unittest.TestCase):
    def test_boundary_scores(self):
        self.assertEqual(boundary_scores([5], [5])["f1"], 1.0)
        self.assertEqual(boundary_scores([5], [5, 9])["recall"], 0.5)
        self.assertEqual(boundary_scores([5, 9], [5])["precision"], 0.5)
        self.assertEqual(boundary_scores([], [])["f1"], 1.0)

    def test_parse_env_file(self):
        import tempfile
        import shutil
        import uuid

        base = TESTS_DIR.parent / ".tmp" / "env_test"
        base.mkdir(parents=True, exist_ok=True)
        folder = base / uuid.uuid4().hex
        folder.mkdir()
        try:
            target = folder / ".env"
            target.write_text("STARTLUX_API=https://example.invalid/v1\n"
                              "STARTLUX_KEY='abc'\n", encoding="utf-8")
            values = _parse_env_file(target)
            self.assertEqual(values["STARTLUX_API"], "https://example.invalid/v1")
            self.assertEqual(values["STARTLUX_KEY"], "abc")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_masked_key(self):
        cfg = ApiConfig(base_url="https://x/v1", key="0123456789")
        self.assertIn("0123", cfg.masked_key)
        self.assertNotIn("6789", cfg.masked_key)


if __name__ == "__main__":
    unittest.main()

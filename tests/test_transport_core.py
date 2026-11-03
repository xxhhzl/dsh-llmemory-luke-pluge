# -*- coding: utf-8 -*-
"""传输层核心单元测试。

覆盖范围：消息队列投递语义（含满队列退避）、错误帧、tools.schema 下发、
stdout 输出泵（drain）。改动 base.py / tools.py / error.py / main.py 后必须跑通。

运行（必须用项目 .venv，端到端用例会以 sys.executable 拉起 src/main.py）：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import commands
import config
import error
import frame
import main
import tools

# 端到端用例用的公共信封字段（fixture 已移除，统一在此构造）
ENVELOPE = {
    "request_id": "c-103",
    "session_id": "s_20261020_1",
    "agent_id": "lead#0",
}


def take_all(q):
    """把队列取空，返回帧字符串列表。"""
    frames = []
    while True:
        item = q.recv()
        if item == "":
            break
        frames.append(item)
    return frames


def frame_json(**overrides):
    """拼一帧合法信封（默认 library.query：已知但未注册），供端到端用例复用。"""
    frame = {
        "protocol_version": config.PROTOCOL_VERSION,
        "plugin_version": config.PLUGIN_VERSION,
        "type": "library.query",
        "request_id": "c-1",
        "job_id": None,
        "session_id": "s-1",
        "agent_id": "lead#0",
        "ts": "2026-10-26T14:56:00+08:00",
        "data": {},
    }
    frame.update(overrides)
    return json.dumps(frame, ensure_ascii=False)


def _schema_queue():
    """调一次 get_tools_schema，返回装着应答帧的队列。"""
    q = base.message_queue(5)
    tools.get_tools_schema(ENVELOPE, q)
    return q


def _commands_queue():
    """调一次 get_commands，返回装着应答帧的队列。"""
    q = base.message_queue(5)
    commands.get_commands(ENVELOPE, q)
    return q


class QueueCase(unittest.TestCase):
    """把重试超时/间隔压小，避免整套用例真等 5 秒；用后还原。"""

    def setUp(self):
        self._saved_timeout = base.DEFAULT_SEND_TIMEOUT
        self._saved_interval = base.RETRY_INTERVAL
        base.DEFAULT_SEND_TIMEOUT = 0.2
        base.RETRY_INTERVAL = 0.01

    def tearDown(self):
        base.DEFAULT_SEND_TIMEOUT = self._saved_timeout
        base.RETRY_INTERVAL = self._saved_interval


class TestConstants(unittest.TestCase):
    def test_defaults_are_module_level(self):
        self.assertEqual(base.DEFAULT_SEND_TIMEOUT, 5.0)
        self.assertEqual(base.RETRY_INTERVAL, 0.5)

    def test_get_cur_time_shape(self):
        pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$"
        self.assertRegex(base.get_cur_time(), pattern)


class TestQueueBasics(QueueCase):
    def test_send_recv_roundtrip(self):
        q = base.message_queue(3)
        self.assertTrue(q.send("a"))
        self.assertTrue(q.send("b"))
        self.assertEqual(take_all(q), ["a", "b"])

    def test_recv_on_empty_returns_empty_string(self):
        self.assertEqual(base.message_queue(3).recv(), "")

    def test_send_returns_false_when_full(self):
        q = base.message_queue(1)
        self.assertTrue(q.send("a"))
        self.assertFalse(q.send("b"))
        self.assertEqual(q._q.qsize(), 1)

    def test_send_blocking_empty_queue_enqueues_once(self):
        q = base.message_queue(5)
        self.assertTrue(q.send_blocking("x"))
        self.assertEqual(take_all(q), ["x"])
        self.assertEqual(q._q.qsize(), 0)


class TestSendBlockingBackoff(QueueCase):
    def test_full_queue_gives_up_without_duplicating(self):
        q = base.message_queue(1)
        q.send("occupied")
        start = time.monotonic()
        self.assertFalse(q.send_blocking("x", timeout=0.2, interval=0.01))
        elapsed = time.monotonic() - start
        # 等满超时区间才放弃，且没有塞进重复帧
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(q._q.qsize(), 1)
        self.assertEqual(take_all(q), ["occupied"])

    def test_timeout_zero_tries_once_immediately(self):
        q = base.message_queue(1)
        q.send("occupied")
        start = time.monotonic()
        self.assertFalse(q.send_blocking("x", timeout=0))
        self.assertLess(time.monotonic() - start, 0.1)
        self.assertEqual(q._q.qsize(), 1)

    def test_retry_succeeds_after_consumer_frees_slot(self):
        q = base.message_queue(1)
        q.send("occupied")
        timer = threading.Timer(0.1, q.recv)
        timer.start()
        try:
            self.assertTrue(q.send_blocking("x", timeout=2.0, interval=0.01))
        finally:
            timer.cancel()
        self.assertEqual(take_all(q), ["x"])

    def test_default_timeout_comes_from_module_constant(self):
        # setUp 已把常量压到 0.2s：不显式传参也应在这个量级返回
        q = base.message_queue(1)
        q.send("occupied")
        start = time.monotonic()
        self.assertFalse(q.send_blocking("x"))
        self.assertLess(time.monotonic() - start, 1.0)


class TestToolsSchema(QueueCase):
    def test_schema_frame_pushed_once(self):
        q = base.message_queue(5)
        tools.get_tools_schema(ENVELOPE, q)
        frames = take_all(q)
        self.assertEqual(len(frames), 1)
        frame = json.loads(frames[0])
        self.assertEqual(frame["type"], "tools.schema")
        self.assertEqual(frame["request_id"], ENVELOPE["request_id"])
        self.assertEqual(frame["session_id"], ENVELOPE["session_id"])
        self.assertEqual(frame["agent_id"], ENVELOPE["agent_id"])
        self.assertIsNone(frame["job_id"])
        self.assertIsInstance(frame["data"], list)
        self.assertEqual(len(frame["data"]), len(tools.tools))
        self.assertEqual(frame["data"][0]["name"], "memory_search")

    def test_schema_frame_carries_versions(self):
        q = base.message_queue(5)
        tools.get_tools_schema(ENVELOPE, q)
        raw = take_all(q)[0]
        frame = json.loads(raw)
        self.assertEqual(frame["protocol_version"], config.PROTOCOL_VERSION)
        self.assertEqual(frame["plugin_version"], config.PLUGIN_VERSION)
        # 中文不得被 \uXXXX 转义
        self.assertNotIn("\\u", raw)
        self.assertIn("检索长期记忆", raw)
        self.assertNotIn("\n", raw)

    def test_tool_definition_shape(self):
        """符合 Transmission §3.1：required / additionalProperties 在 parameters 内。"""
        entry = json.loads(take_all(_schema_queue())[0])["data"][0]
        self.assertEqual(set(entry), {"name", "description", "strict", "parameters"})
        params = entry["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["required"], ["query"])
        self.assertIs(params["additionalProperties"], False)
        self.assertEqual(set(params["properties"]),
                         {"query", "limit", "kind", "scope", "min_score"})
        # pydantic 的 title 噪声应被去掉
        for spec in params["properties"].values():
            self.assertNotIn("title", spec)
        self.assertEqual(params["properties"]["limit"]["minimum"], 1)
        self.assertEqual(params["properties"]["limit"]["maximum"], 100)
        self.assertEqual(params["properties"]["limit"]["default"], 5)

    def test_schema_not_duplicated_when_queue_full(self):
        q = base.message_queue(1)
        q.send("occupied")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            tools.get_tools_schema(ENVELOPE, q)
        self.assertEqual(q._q.qsize(), 1)
        self.assertEqual(take_all(q), ["occupied"])
        self.assertIn("-32603", stderr.getvalue())


class TestErrorFrame(QueueCase):
    def test_error_frame_shape(self):
        q = base.message_queue(5)
        self.assertTrue(error.send_error_message(ENVELOPE, q, -32600, "非法请求", "缺必填字段"))
        frames = take_all(q)
        self.assertEqual(len(frames), 1)
        frame = json.loads(frames[0])
        self.assertEqual(frame["type"], "error")
        self.assertEqual(frame["request_id"], ENVELOPE["request_id"])
        self.assertEqual(frame["data"]["code"], -32600)
        self.assertEqual(frame["data"]["message"], "非法请求")
        self.assertIsNone(frame["data"]["retry_after_ms"])
        self.assertIsNone(frame["job_id"])
        self.assertEqual(frame["protocol_version"], config.PROTOCOL_VERSION)
        self.assertEqual(frame["plugin_version"], config.PLUGIN_VERSION)

    def test_error_wait_false_returns_immediately_when_full(self):
        q = base.message_queue(1)
        q.send("occupied")
        stderr = io.StringIO()
        start = time.monotonic()
        with redirect_stderr(stderr):
            ok = error.send_error_message(ENVELOPE, q, -32603, "内核内部错误", "队列已满", wait=False)
        elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.1)
        self.assertEqual(q._q.qsize(), 1)

    def test_error_wait_true_times_out(self):
        q = base.message_queue(1)
        q.send("occupied")
        stderr = io.StringIO()
        start = time.monotonic()
        with redirect_stderr(stderr):
            ok = error.send_error_message(ENVELOPE, q, -32605, "队列满", "重试超时")
        elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertEqual(q._q.qsize(), 1)
        self.assertIn("投递失败", stderr.getvalue())


class TestBuildFrame(unittest.TestCase):
    def test_inherits_envelope_fields(self):
        raw = config.build_frame("notify", {"op": "upsert"},
                                 {"request_id": "c-9", "job_id": "j-1",
                                  "session_id": "s-9", "agent_id": "a#1"})
        frame = json.loads(raw)
        self.assertEqual(frame["type"], "notify")
        self.assertEqual(frame["request_id"], "c-9")
        self.assertEqual(frame["job_id"], "j-1")
        self.assertEqual(frame["session_id"], "s-9")
        self.assertEqual(frame["agent_id"], "a#1")
        self.assertEqual(frame["protocol_version"], config.PROTOCOL_VERSION)
        self.assertEqual(frame["data"], {"op": "upsert"})

    def test_missing_envelope_falls_back_to_empty_strings(self):
        frame = json.loads(config.build_frame("health", {}))
        self.assertEqual(frame["request_id"], "")
        self.assertEqual(frame["session_id"], "")
        self.assertEqual(frame["agent_id"], "")
        self.assertIsNone(frame["job_id"])

    def test_rejects_invalid_data_type(self):
        # data 只接受对象或数组，标量应在建帧时就被拦下
        with self.assertRaises(Exception):
            config.build_frame("health", "not-a-dict")


class TestDrain(unittest.TestCase):
    def test_drain_writes_all_frames_and_empties_queue(self):
        q = base.message_queue(10)
        for i in range(3):
            q.send('{"n": %d}' % i)
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.drain(q)
        self.assertEqual(buf.getvalue(), '{"n": 0}\n{"n": 1}\n{"n": 2}\n')
        self.assertEqual(q._q.qsize(), 0)

    def test_drain_on_empty_queue_writes_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main.drain(base.message_queue(1))
        self.assertEqual(buf.getvalue(), "")


class TestEnvelope(unittest.TestCase):
    def test_valid_frame_returns_plain_dict(self):
        ok, data, field = config.check_json_format(frame_json())
        self.assertTrue(ok)
        self.assertEqual(field, "")
        self.assertIsInstance(data, dict)
        self.assertEqual(data["type"], "library.query")
        self.assertEqual(data["protocol_version"], config.PROTOCOL_VERSION)
        self.assertIsNone(data["job_id"])

    def test_array_payload_is_accepted(self):
        ok, data, _ = config.check_json_format(
            frame_json(type="tools.calls", data=[{"call_id": "call_77"}]))
        self.assertTrue(ok)
        self.assertIsInstance(data["data"], list)

    def test_broken_json_returns_false(self):
        ok, detail, field = config.check_json_format('{"type": ')
        self.assertFalse(ok)
        self.assertEqual(field, "")
        self.assertIsInstance(detail, str)
        # detail 只保留首条错误的说明，不带 pydantic 长报告与官网链接
        self.assertIn("Invalid JSON", detail)
        self.assertNotIn("errors.pydantic.dev", detail)
        self.assertLess(len(detail), 200)

    def test_missing_field_returns_false(self):
        broken = json.loads(frame_json())
        del broken["session_id"]
        ok, detail, field = config.check_json_format(json.dumps(broken))
        self.assertFalse(ok)
        self.assertEqual(field, "session_id")
        self.assertIn("session_id", detail)
        self.assertIn("Field required", detail)
        self.assertNotIn("errors.pydantic.dev", detail)

    def test_unknown_type_is_reported_by_field(self):
        ok, detail, field = config.check_json_format(frame_json(type="tools.nope"))
        self.assertFalse(ok)
        # 调用方据此回 -32601 而不是 -32700
        self.assertEqual(field, "type")
        self.assertIn("type", detail)


class MainProcessCase(unittest.TestCase):
    """端到端公共设施：把 stdin 文本喂给 src/main.py 子进程。"""

    MAIN = Path(__file__).resolve().parents[1] / "src" / "main.py"

    def run_main(self, stdin_text):
        return subprocess.run(
            [sys.executable, str(self.MAIN)],
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )


class TestMainProcess(MainProcessCase):
    """端到端：把帧喂给 src/main.py 子进程，验证不崩且按协议回错误帧。"""

    def test_valid_frame_does_not_crash(self):
        proc = self.run_main(frame_json())
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))

    def test_broken_json_returns_32700_and_survives(self):
        proc = self.run_main('{"type": \n')
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        self.assertIn('"code":-32700', out)
        self.assertIn('"protocol_version":"1.0"', out)
        self.assertNotIn("errors.pydantic.dev", out)

    def test_incompatible_version_returns_32600_and_survives(self):
        proc = self.run_main(frame_json(protocol_version="2.0"))
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        self.assertIn('"code":-32600', out)
        self.assertIn("本插件 1.0，收到 2.0", out)

    def test_multiple_frames_processed_in_one_run(self):
        stdin_text = "\n".join([
            '{"broken": ',
            frame_json(),                          # 已知但未注册 -> -32601
            frame_json(protocol_version="9.9"),    # 版本不兼容   -> -32600
        ]) + "\n"
        proc = self.run_main(stdin_text)
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(out.count('"code":'), 3)
        self.assertIn('"code":-32700', out)
        self.assertIn('"code":-32600', out)
        self.assertEqual(out.count('"code":-32601'), 1)

    def test_schema_request_is_answered(self):
        """外壳拉取：发 type=tools.schema 的请求，应回同 type 的 schema 帧。"""
        proc = self.run_main(frame_json(type="tools.schema", data={}) + "\n")
        lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line]
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(len(lines), 1)
        frame = json.loads(lines[0])
        self.assertEqual(frame["type"], "tools.schema")
        self.assertEqual(frame["request_id"], "c-1")
        self.assertEqual(frame["session_id"], "s-1")
        self.assertIsInstance(frame["data"], list)
        self.assertEqual(frame["data"][0]["name"], "memory_search")

    def test_unknown_type_returns_32601(self):
        proc = self.run_main(frame_json(type="tools.nope") + "\n")
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        self.assertIn('"code":-32601', out)

    def test_known_but_unregistered_type_returns_32601(self):
        proc = self.run_main(frame_json(type="library.query", data={}) + "\n")
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        self.assertIn('"code":-32601', out)
        self.assertIn("未注册的 type", out)


class TestCommands(QueueCase):
    def test_commands_frame_shape(self):
        q = base.message_queue(5)
        commands.get_commands(ENVELOPE, q)
        frames = take_all(q)
        self.assertEqual(len(frames), 1)
        frame = json.loads(frames[0])
        self.assertEqual(frame["type"], "commands")
        self.assertEqual(frame["request_id"], ENVELOPE["request_id"])
        self.assertEqual(frame["session_id"], ENVELOPE["session_id"])
        self.assertEqual(frame["protocol_version"], config.PROTOCOL_VERSION)
        self.assertIsInstance(frame["data"], list)
        self.assertGreaterEqual(len(frame["data"]), 1)

    def test_memory_command_definition(self):
        frame = json.loads(take_all(_commands_queue())[0])
        spec = next(item for item in frame["data"] if item["name"] == "memory")
        self.assertEqual(spec["aliases"], ["mem"])
        self.assertEqual(spec["category"], "context")
        self.assertFalse(spec["requires_confirmation"])
        self.assertEqual(spec["usage"], "/memory [--full] [--mark-end]")
        self.assertEqual([arg["name"] for arg in spec["arguments"]], ["full", "mark_end"])
        self.assertEqual([arg["flag"] for arg in spec["arguments"]], ["--full", "--mark-end"])
        self.assertEqual({arg["type"] for arg in spec["arguments"]}, {"boolean"})
        self.assertEqual(set(spec), {"name", "aliases", "description", "usage",
                                     "arguments", "requires_confirmation", "category"})

    def test_commands_not_duplicated_when_queue_full(self):
        q = base.message_queue(1)
        q.send("occupied")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            commands.get_commands(ENVELOPE, q)
        self.assertEqual(q._q.qsize(), 1)
        self.assertIn("-32603", stderr.getvalue())


class TestCommandRegistry(unittest.TestCase):
    def test_rejects_bad_name(self):
        with self.assertRaises(ValueError):
            commands.command(name="Bad Name", description="x")

    def test_rejects_duplicate_name(self):
        with self.assertRaises(ValueError):
            commands.command(name="memory", description="dup")

    def test_rejects_bad_category(self):
        with self.assertRaises(ValueError):
            commands.command(name="tmp_ok_name", description="x", category="nope")

    def test_rejects_bad_argument_type(self):
        with self.assertRaises(ValueError):
            commands.command(name="tmp_ok_name", description="x",
                             arguments=[{"name": "a", "type": "float"}])


class TestMainProcessCommands(MainProcessCase):
    def test_commands_request_is_answered(self):
        proc = self.run_main(frame_json(type="commands", data={}) + "\n")
        lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line]
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(len(lines), 1)
        response = json.loads(lines[0])
        self.assertEqual(response["type"], "commands")
        self.assertEqual(response["request_id"], "c-1")
        self.assertEqual(response["data"][0]["name"], "memory")


class TestFramePush(QueueCase):
    def test_push_enqueues_single_frame(self):
        q = base.message_queue(5)
        self.assertTrue(frame.push(q, ENVELOPE, "health", {}))
        frames = take_all(q)
        self.assertEqual(len(frames), 1)
        payload = json.loads(frames[0])
        self.assertEqual(payload["type"], "health")
        self.assertEqual(payload["request_id"], ENVELOPE["request_id"])

    def test_push_reports_build_failure_as_error_frame(self):
        q = base.message_queue(5)
        # data 只接受对象或数组，标量应在建帧时失败
        self.assertFalse(frame.push(q, ENVELOPE, "health", "not-a-dict"))
        frames = take_all(q)
        self.assertEqual(len(frames), 1)
        self.assertEqual(json.loads(frames[0])["data"]["code"], frame.CODE_INTERNAL)

    def test_push_degrades_when_queue_full(self):
        q = base.message_queue(1)
        q.send("occupied")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertFalse(frame.push(q, ENVELOPE, "notify", {"op": "upsert"}))
        self.assertEqual(q._q.qsize(), 1)
        self.assertIn(str(frame.CODE_INTERNAL), stderr.getvalue())


class ConfigCase(QueueCase):
    """保存/还原 config 模块的运行期状态，避免用例互相污染。"""

    def setUp(self):
        super().setUp()
        self._saved_config = copy.deepcopy(config._config)
        self._saved_revision = config.get_revision()

    def tearDown(self):
        config._config = self._saved_config
        config._revision = self._saved_revision
        config.set_reload_hook(None)
        super().tearDown()


class TestConfigRoutes(ConfigCase):
    def test_get_returns_config_and_revision(self):
        q = base.message_queue(5)
        config.config_get(ENVELOPE, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["type"], "config.get")
        self.assertEqual(reply["data"]["revision"], config.get_revision())
        self.assertIn("jev", reply["data"]["config"])
        self.assertIn("on_uncertain", reply["data"]["config"]["jev"])

    def test_split_defaults_use_token_budget(self):
        split = config.get_config()["split"]
        self.assertEqual(split["max_event_tokens"], 128000)
        self.assertEqual(split["max_messages"], 1000)   # 仅紧急兜底

    def test_set_applies_patch_and_bumps_revision(self):
        before = config.get_revision()
        q = base.message_queue(5)
        config.config_set({"data": {"patch": {"search": {"limit_max": 30}}},
                           **ENVELOPE}, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["type"], "config.set")
        self.assertEqual(reply["data"]["applied"], ["search.limit_max"])
        self.assertEqual(reply["data"]["revision"], before + 1)
        self.assertIsNone(reply["data"]["reload_ms"])
        self.assertEqual(config.get_config()["search"]["limit_max"], 30)

    def test_unknown_key_is_rejected(self):
        before = config.get_revision()
        q = base.message_queue(5)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            config.config_set({"data": {"patch": {"jev": {"accept_confidance": 0.5}}},
                               **ENVELOPE}, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["data"]["code"], -32602)
        self.assertIn("未知配置项", reply["data"]["detail"])
        self.assertEqual(config.get_revision(), before)
        self.assertNotEqual(config.get_config()["search"]["limit_max"], 0.5)

    def test_partial_patch_is_all_or_nothing(self):
        before = config.get_config()
        q = base.message_queue(5)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            config.config_set({"data": {"patch": {
                "search": {"limit_max": 30},
                "split": {"max_messages": 0},        # 越界
            }}, **ENVELOPE}, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["data"]["code"], -32602)
        self.assertEqual(config.get_config(), before)

    def test_wrong_type_is_rejected(self):
        q = base.message_queue(5)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            config.config_set({"data": {"patch": {"split": {"max_messages": "40"}}},
                               **ENVELOPE}, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["data"]["code"], -32602)
        self.assertIn("整数", reply["data"]["detail"])

    def test_reload_reports_elapsed_ms(self):
        q = base.message_queue(5)
        config.config_set({"data": {"patch": {"log": {"level": "debug"}}, "reload": True},
                           **ENVELOPE}, q)
        reply = json.loads(take_all(q)[0])
        self.assertIsInstance(reply["data"]["reload_ms"], int)
        self.assertGreaterEqual(reply["data"]["reload_ms"], 0)

    def test_reload_failure_keeps_old_config(self):
        before = config.get_config()

        def boom():
            raise RuntimeError("index rebuild failed")

        config.set_reload_hook(boom)
        q = base.message_queue(5)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            config.config_set({"data": {"patch": {"log": {"level": "warn"}},
                                        "reload": True}, **ENVELOPE}, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["data"]["code"], -32603)
        self.assertEqual(config.get_config(), before)


class TestHealthShutdown(ConfigCase):
    def setUp(self):
        super().setUp()
        base.shutdown_requested = False

    def tearDown(self):
        base.shutdown_requested = False
        super().tearDown()

    def test_health_reports_runtime_state(self):
        q = base.message_queue(5)
        base.health(ENVELOPE, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["type"], "health")
        self.assertTrue(reply["data"]["ok"])
        self.assertEqual(reply["data"]["revision"], config.get_revision())
        self.assertEqual(reply["data"]["queue_depth"], 0)
        self.assertIsInstance(reply["data"]["db_ok"], bool)

    def test_queue_depth_counts_pending_frames(self):
        q = base.message_queue(5)
        q.send("a")
        q.send("b")
        self.assertEqual(q.depth(), 2)

    def test_shutdown_rejects_bad_drain_ms(self):
        q = base.message_queue(5)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            base.shutdown({"data": {"drain_ms": 99999}, **ENVELOPE}, q)
        reply = json.loads(take_all(q)[0])
        self.assertEqual(reply["data"]["code"], -32602)
        self.assertFalse(base.shutdown_requested)

    def test_shutdown_sets_flag(self):
        q = base.message_queue(5)
        base.shutdown({"data": {"drain_ms": 100}, **ENVELOPE}, q)
        self.assertTrue(base.shutdown_requested)
        self.assertEqual(base.shutdown_drain_ms, 100)
        self.assertEqual(take_all(q), [])      # shutdown 不应答


class TestMainProcessOS(ConfigCase):
    """端到端：config / health / shutdown 三条控制链路。"""

    MAIN = Path(__file__).resolve().parents[1] / "src" / "main.py"

    def run_main(self, stdin_text):
        return subprocess.run(
            [sys.executable, str(self.MAIN)],
            input=stdin_text.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )

    def test_config_get_is_answered(self):
        proc = self.run_main(frame_json(type="config.get", data={}) + "\n")
        lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line]
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        reply = json.loads(lines[0])
        self.assertEqual(reply["type"], "config.get")
        self.assertIn("config", reply["data"])

    def test_health_is_answered(self):
        proc = self.run_main(frame_json(type="health", data={}) + "\n")
        lines = [line for line in proc.stdout.decode("utf-8").splitlines() if line]
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        reply = json.loads(lines[0])
        self.assertEqual(reply["type"], "health")
        self.assertTrue(reply["data"]["ok"])

    def test_config_set_unknown_key_returns_32602(self):
        proc = self.run_main(frame_json(type="config.set",
                                        data={"patch": {"jev": {"nope": 1}}}) + "\n")
        out = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn('"code":-32602', out)

    def test_shutdown_exits_cleanly(self):
        proc = self.run_main(frame_json(type="shutdown", data={"drain_ms": 0}) + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stdout.decode("utf-8").strip(), "")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""外壳契约测试：按 JS 外壳（src/js/index.js）的启动顺序与内核对话。

沙箱里 Node 无法用管道抓子进程输出（spawn EPERM），所以进程级验证放在
Python 侧：这里逐帧复刻外壳的动作，断言它读到的字段真的存在——
health → config.set → tools.schema → tools.calls → ingest.request → shutdown。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BASE_TMP = ROOT / ".tmp" / "test_shell_contract"
S1 = "s_shell"


class KernelProcess:
    """最小帧客户端：一行一帧。"""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "main.py")],
            cwd=str(workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.seq = 0
        self.pending: dict[str, dict] = {}
        self.unsolicited: list[dict] = []

    def send(self, type_: str, data=None, *, request_id: str = "", session_id=S1):
        self.seq += 1
        rid = request_id or f"req_{self.seq}"
        frame = {
            "protocol_version": "1.0", "plugin_version": "0.1.0", "type": type_,
            "request_id": rid, "job_id": None, "session_id": session_id,
            "agent_id": "shell", "ts": "2026-11-03T12:00:00+08:00",
            "data": data if data is not None else {},
        }
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return rid

    def read_until(self, request_id: str, timeout_frames: int = 50) -> dict:
        for _ in range(timeout_frames):
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError("内核提前关闭 stdout")
            frame = json.loads(line)
            if frame.get("request_id") == request_id:
                return frame
            self.unsolicited.append(frame)
        raise AssertionError(f"没等到 {request_id} 的应答")

    def request(self, type_: str, data=None, **kwargs) -> dict:
        rid = self.send(type_, data, **kwargs)
        return self.read_until(rid)

    def _answer_llm_error(self, frame: dict):
        """冒充外壳：回一条模型不可用，内核应走降级路径而不是卡住。"""
        payload = frame.get("data") or {}
        self.proc.stdin.write(json.dumps({
            "protocol_version": "1.0", "plugin_version": "0.1.0",
            "type": "llm.result", "request_id": frame.get("request_id", ""),
            "job_id": None, "session_id": S1, "agent_id": "shell",
            "ts": "2026-11-03T12:00:00+08:00",
            "data": {"request_id": payload.get("request_id", ""),
                     "error": {"code": 1004, "message": "测试：模型不可用"}},
        }, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def close(self):
        try:
            if self.proc.poll() is None:
                self.send("shutdown", {"drain_ms": 500})
                self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        finally:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream and stream.close()
                except Exception:
                    pass


class ShellContractCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.kernel = KernelProcess(self._dir)

    def tearDown(self):
        self.kernel.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestStartupSequence(ShellContractCase):
    def test_health(self):
        frame = self.kernel.request("health", {})
        self.assertEqual(frame["type"], "health")
        data = frame["data"]
        self.assertTrue(data["ok"])
        # 外壳读这些字段做诊断
        self.assertIn("queue_depth", data)
        self.assertIn("queue_dropped", data)
        self.assertIn("db_ok", data)

    def test_config_set_redirects_db(self):
        db_path = (self._dir / "custom.db").as_posix()
        frame = self.kernel.request("config.set",
                                    {"patch": {"store": {"db_path": db_path}}})
        self.assertEqual(frame["type"], "config.set")
        self.assertEqual(frame["data"]["applied"], ["store.db_path"])
        # 库是懒建的：一次检索会触发建库
        self.kernel.request("tools.calls", {"calls": [
            {"call_id": "c1", "name": "memory_search", "arguments": {"query": "预热"}},
        ]})
        self.assertTrue(Path(db_path).exists(), "配置生效后库应落在新路径")

    def test_tools_schema_shape(self):
        frame = self.kernel.request("tools.schema", {})
        schemas = frame["data"]
        self.assertEqual(len(schemas), 4)
        by_name = {item["name"]: item for item in schemas}
        self.assertEqual(set(by_name), {"memory_search", "memory_get", "memory_recent", "memory_report"})
        for item in schemas:
            self.assertIn("description", item)
            params = item["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("properties", params)
            self.assertIn("required", params)
            # 外壳的 convertParameters 依赖这两个结构
            for name, spec in params["properties"].items():
                self.assertIsInstance(spec, dict, f"{item['name']}.{name}")

    def test_admin_tables_frame(self):
        frame = self.kernel.request("admin", {"op": "tables"})
        self.assertEqual(frame["type"], "admin")
        data = frame["data"]
        self.assertTrue(data["ok"])
        names = {item["name"] for item in data["result"]["tables"]}
        self.assertEqual(names, {"event", "message", "memory", "experience",
                                 "cognition", "insight", "self_memory", "digest"})

    def test_admin_rejects_bad_op(self):
        frame = self.kernel.request("admin", {"op": "nope"})
        self.assertFalse(frame["data"]["ok"])
        self.assertIn("不支持的 op", frame["data"]["error"])

    def test_core_get_frame(self):
        frame = self.kernel.request("core.get", {})
        self.assertEqual(frame["type"], "core")
        self.assertTrue(frame["data"]["enabled"])
        self.assertIn("version", frame["data"])
        self.assertIn("notes_version", frame["data"])

    def test_stats_frame(self):
        frame = self.kernel.request("stats", {})
        self.assertEqual(frame["type"], "stats")
        data = frame["data"]
        for key in ("db_path", "messages", "events", "experiences", "cognitions"):
            self.assertIn(key, data, f"设置页会读 stats.{key}")

    def test_tool_call_roundtrip(self):
        frame = self.kernel.request("tools.calls", {"calls": [
            {"call_id": "c1", "name": "memory_search",
             "arguments": {"query": "测试", "limit": 3}},
        ]})
        results = frame["data"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["call_id"], "c1")
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["data"]["count"], 0)

    def test_unknown_tool_reports_code(self):
        frame = self.kernel.request("tools.calls", {"calls": [
            {"call_id": "c1", "name": "nope", "arguments": {}},
        ]})
        self.assertFalse(frame["data"][0]["ok"])
        self.assertEqual(frame["data"][0]["error"]["code"], -32601)


class TestIngestSequence(ShellContractCase):
    def test_ingest_receipt_then_completed(self):
        """外壳在 ingest 后要同时看到回执与完成帧。"""
        rid = self.kernel.send("ingest.request", {
            "messages": [
                {"msg_id": "m1", "seq": 0, "role": "user", "content": "你好"},
                {"msg_id": "m2", "seq": 1, "role": "assistant", "content": "在"},
            ],
            "cursor": {"last_seq": -1},
        })
        receipt = self.kernel.read_until(rid)
        self.assertEqual(receipt["type"], "ingest.receipt")
        self.assertTrue(receipt["data"]["accepted"])
        self.assertEqual(receipt["data"]["new_messages"], 2)

        # 等完成帧：期间要像外壳一样应答 llm.request（这里回错误，验证降级）
        completed = None
        for _ in range(200):
            line = self.kernel.proc.stdout.readline()
            self.assertTrue(line, "内核提前退出")
            frame = json.loads(line)
            if frame.get("type") == "llm.request":
                self.kernel._answer_llm_error(frame)
                continue
            if frame.get("type") == "ingest.completed":
                completed = frame
                break
        self.assertIsNotNone(completed, "应收到 ingest.completed")
        summary = completed["data"]["summary"]
        for key in ("messages", "duplicates", "seq_conflicts",
                    "events_created", "events_resumed", "summaries", "failed"):
            self.assertIn(key, summary, f"外壳会读 summary.{key}")


class TestMalformedInput(unittest.TestCase):
    """坏输入不能让内核退出：非法 UTF-8 字节、坏 JSON、未知 type。"""

    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "main.py")],
            cwd=str(self._dir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)

    def tearDown(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
        finally:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream and stream.close()
                except Exception:
                    pass
            shutil.rmtree(self._dir, ignore_errors=True)

    def _write(self, payload: bytes):
        self.proc.stdin.write(payload)
        self.proc.stdin.flush()

    def _health_frame(self, request_id: str = "req_ok") -> dict:
        return {
            "protocol_version": "1.0", "plugin_version": "0.1.0",
            "type": "health", "request_id": request_id, "job_id": None,
            "session_id": S1, "agent_id": "shell",
            "ts": "2026-11-03T12:00:00+08:00", "data": {},
        }

    def test_invalid_utf8_byte_does_not_kill_kernel(self):
        self._write(b"\xff\xfe not utf8\n")
        self._write(json.dumps(self._health_frame()).encode("utf-8") + b"\n")
        first = self.proc.stdout.readline()
        self.assertTrue(first, "内核被一个坏字节打死了")
        self.assertEqual(json.loads(first)["type"], "error", "坏行应回错误帧")
        line = self.proc.stdout.readline()
        self.assertTrue(line, "错误帧之后内核不该退出")
        frame = json.loads(line)
        self.assertEqual(frame["type"], "health")
        self.assertEqual(frame["request_id"], "req_ok")

    def test_truncated_utf8_sequence_does_not_kill_kernel(self):
        self._write(b'{"half": "\xe4\xb8')      # 半个汉字，后面直接换行
        self._write(b'"}\n')
        self._write(json.dumps(self._health_frame("req_2")).encode() + b"\n")
        frame = json.loads(self.proc.stdout.readline())
        self.assertEqual(frame["type"], "error", "坏帧应回错误帧")
        frame2 = json.loads(self.proc.stdout.readline())
        self.assertEqual(frame2["type"], "health")
        self.assertEqual(frame2["request_id"], "req_2")

    def test_unknown_type_keeps_request_id(self):
        # type 不在枚举内：错误帧必须带回 request_id，否则外壳会白等 30s
        frame = dict(self._health_frame("req_bad"), type="nope.type")
        self._write(json.dumps(frame).encode("utf-8") + b"\n")
        error = json.loads(self.proc.stdout.readline())
        self.assertEqual(error["type"], "error")
        self.assertEqual(error["request_id"], "req_bad", "错误帧要能配对")
        self.assertEqual(error["data"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()

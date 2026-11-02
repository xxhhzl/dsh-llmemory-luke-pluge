# -*- coding: utf-8 -*-
"""外壳代理客户端（src/llm.py）单测：上行帧、应答配对、超时、错误。

不联网：直接读写 message_queue，模拟外壳的 llm.result。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import llm


def result_frame(request_id: str, **payload) -> dict:
    data = {"request_id": request_id}
    data.update(payload)
    return {"type": "llm.result", "request_id": "", "data": data}


class LlmCase(unittest.TestCase):
    def setUp(self):
        self.q = base.message_queue()
        self.client = llm.LlmClient(self.q, timeout=0.5)

    def _last_frame(self) -> dict:
        raw = self.q.recv()
        self.assertTrue(raw, "队列里应有 llm.request")
        return json.loads(raw)


class TestSubmit(LlmCase):
    def test_submit_emits_llm_request(self):
        future = self.client.submit(
            [{"role": "user", "content": "hi"}], purpose="summary",
            response_format={"type": "json_object"}, think=True, timeout_ms=1234)
        frame = self._last_frame()
        self.assertEqual(frame["type"], "llm.request")
        data = frame["data"]
        self.assertEqual(data["purpose"], "summary")
        self.assertEqual(data["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(data["response_format"], {"type": "json_object"})
        self.assertTrue(data["think"])
        self.assertEqual(data["timeout_ms"], 1234)
        self.assertEqual(data["request_id"], future.request_id)
        self.assertEqual(self.client.pending, 1)

    def test_submit_defaults(self):
        self.client.submit([{"role": "user", "content": "x"}])
        data = self._last_frame()["data"]
        self.assertEqual(data["purpose"], "")
        self.assertIsNone(data["response_format"])
        self.assertFalse(data["think"])
        self.assertEqual(data["timeout_ms"], 500)   # 客户端 timeout=0.5s

    def test_queue_full_raises_and_clears_pending(self):
        q = base.message_queue(max_size=1)
        q.send("占位")
        client = llm.LlmClient(q)
        original = base.DEFAULT_SEND_TIMEOUT
        base.DEFAULT_SEND_TIMEOUT = 0.01
        try:
            with self.assertRaises(llm.LlmError):
                client.submit([{"role": "user", "content": "x"}])
        finally:
            base.DEFAULT_SEND_TIMEOUT = original
        self.assertEqual(client.pending, 0)


class TestResolve(LlmCase):
    def test_resolve_completes_future(self):
        future = self.client.submit([{"role": "user", "content": "x"}])
        self._last_frame()
        ok = self.client.on_result(result_frame(
            future.request_id, content="hello", model="m", usage={"p": 1}))
        self.assertTrue(ok)
        result = future.result(timeout=1)
        self.assertEqual(result.content, "hello")
        self.assertEqual(result.model, "m")
        self.assertEqual(result.usage, {"p": 1})
        self.assertEqual(self.client.pending, 0)

    def test_unmatched_result_returns_false(self):
        self.assertFalse(self.client.on_result(result_frame("llm_999", content="x")))

    def test_error_payload_raises(self):
        future = self.client.submit([{"role": "user", "content": "x"}])
        self._last_frame()
        self.client.on_result(result_frame(
            future.request_id,
            error={"code": 1004, "message": "模型不可用", "detail": "quota"}))
        with self.assertRaises(llm.LlmError) as ctx:
            future.result(timeout=1)
        self.assertIn("1004", str(ctx.exception))
        self.assertIn("quota", str(ctx.exception))

    def test_missing_content_raises(self):
        future = self.client.submit([{"role": "user", "content": "x"}])
        self._last_frame()
        self.client.on_result(result_frame(future.request_id))
        with self.assertRaises(llm.LlmError):
            future.result(timeout=1)

    def test_timeout_raises_and_clears_pending(self):
        with self.assertRaises(llm.LlmError):
            self.client.request([{"role": "user", "content": "x"}], timeout=0.05)
        self.assertEqual(self.client.pending, 0)

    def test_request_roundtrip(self):
        holder = {}

        def worker():
            raw = ""
            deadline = time.monotonic() + 2
            while not raw and time.monotonic() < deadline:
                raw = self.q.recv()
                if not raw:
                    time.sleep(0.01)
            frame = json.loads(raw)
            holder["request_id"] = frame["data"]["request_id"]
            self.client.on_result(result_frame(holder["request_id"], content="ok"))

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            result = self.client.request([{"role": "user", "content": "x"}], timeout=2)
        finally:
            thread.join(timeout=2)
        self.assertEqual(result.content, "ok")
        self.assertEqual(result.request_id, holder["request_id"])


class TestSingleton(unittest.TestCase):
    def test_bind_and_get_client(self):
        client = llm.bind(base.message_queue(), timeout=0.2)
        self.assertIs(llm.get_client(), client)

    def test_on_result_routes_to_bound_client(self):
        client = llm.bind(base.message_queue(), timeout=0.2)
        future = client.submit([{"role": "user", "content": "x"}])
        client._q.recv()
        self.assertTrue(llm.on_result(result_frame(future.request_id, content="ok")))
        self.assertEqual(future.result(timeout=1).content, "ok")


if __name__ == "__main__":
    unittest.main()

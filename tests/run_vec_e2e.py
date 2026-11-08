# -*- coding: utf-8 -*-
"""端到端验证：kernel → embed.request → 模拟外壳 → 本地 Ollama → vec0 → 检索。

不依赖真实模型：只验证 embedding 链路与向量检索，LLM 代答返回固定串。

用法（需本机 Ollama 已 `ollama pull bge-m3`）：
    python tests/run_vec_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from database import Cognition, Database, Experience        # noqa: E402
from shell_sim import ShellSim                              # noqa: E402

WORK = ROOT / ".tmp" / "e2e_vec"
DB = WORK / "memory.db"
EMBED_URL = "http://127.0.0.1:11434/v1/embeddings"


def embed(texts):
    body = json.dumps({"model": "bge-m3", "input": texts}).encode("utf-8")
    request = urllib.request.Request(EMBED_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [item["embedding"] for item in data["data"]]


class VecShell(ShellSim):
    """额外代答 embed.request。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.embed_calls = 0
        self.embed_errors = []

    def _read_loop(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("type") == "embed.request":
                threading.Thread(target=self._handle_embed, args=(frame,),
                                 daemon=True).start()
                continue
            if frame.get("type") == "llm.request":
                threading.Thread(target=self._handle_llm, args=(frame,),
                                 daemon=True).start()
                continue
            with self._cond:
                self._frames.append(frame)
                self._cond.notify_all()

    def _handle_embed(self, frame):
        payload = frame.get("data") or {}
        request_id = payload.get("request_id") or ""
        try:
            vectors = embed(payload.get("input") or [])
            self.embed_calls += 1
            self.send("embed.result", {"request_id": request_id, "vectors": vectors,
                                       "model": "bge-m3",
                                       "dim": len(vectors[0]) if vectors else 0},
                      request_id=frame.get("request_id") or "")
        except Exception as exc:
            self.embed_errors.append(str(exc))
            self.send("embed.result", {"request_id": request_id,
                                       "error": {"message": str(exc)}},
                      request_id=frame.get("request_id") or "")


def iso():
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    seed = Database(DB)
    experience = seed.insert_experience(Experience(
        context="导出 drawio 前", content="先把所有形状的线宽统一再导出",
        kind="technique", importance=3, confidence=0.9, source_event_id=1,
        cues=("drawio", "线宽"), created_at=iso()))
    cognition = seed.insert_cognition(Cognition(
        subject="用户", content="偏好中文回复", kind="preference", importance=3,
        confidence=0.9, source_event_id=1, cues=("中文",), created_at=iso()))
    seed.close()
    print(f"预置：experience#{experience} cognition#{cognition}")

    os.environ["DSH_LONGMEMORY_DB"] = str(DB)
    shell = VecShell(WORK, model="stub", session_id="e2e-vec")
    shell.start()
    try:
        shell.send("health", {})
        print("health:", "ok" if shell.wait("health", timeout=30) else "超时")

        shell.send("admin", {"op": "vec.reindex"})
        started = shell.wait("admin", timeout=30)
        print("reindex:", json.dumps((started or {}).get("data", {}), ensure_ascii=False))

        status = None
        for _ in range(20):
            shell.send("admin", {"op": "vec.status"})
            frame = shell.wait("admin", timeout=30)
            status = (frame or {}).get("data", {}).get("result")
            if status and status.get("counts", {}).get("experience") and \
                    status.get("counts", {}).get("cognition"):
                break
            time.sleep(0.5)
        print("vec.status:", json.dumps(status, ensure_ascii=False))
        assert status and status["enabled"], "向量索引不可用"

        # 语义查询：与正文零词面重叠，只有向量通道能捞到
        query_vector = embed(["边框粗细保持一致"])[0]
        shell.send("tools.calls", {"calls": [{
            "call_id": "c1", "name": "memory_search",
            "arguments": {"query": "borderWidth"},
            "query_vector": query_vector,
        }]})
        result = shell.wait("tools.result", timeout=60)
        entry = ((result or {}).get("data") or [{}])[0]
        items = ((entry.get("data") or {}).get("items") or [])
        print(f"memory_search(borderWidth + vec) -> {len(items)} 条")
        for item in items:
            print(f"  [{item['kind']}#{item['id']}] {item['text'][:50]} "
                  f"score={item['score']} assoc={item['meta'].get('assoc')}")
        kinds = {(item["kind"], item["id"]) for item in items}
        assert ("experience", experience) in kinds, "向量召回没捞到经验"
        assert ("cognition", cognition) in kinds, "向量召回没捞到认知"

        # 设置链路：写 key → 读掩码 → 库里是明文（本地库）
        shell.send("admin", {"op": "settings.set", "key": "embedding", "value": {
            "baseUrl": EMBED_URL.rsplit("/embeddings", 1)[0], "model": "bge-m3",
            "apiKey": "sk-e2e-123456"}})
        shell.wait("admin", timeout=30)
        shell.send("admin", {"op": "settings.get"})
        frame = shell.wait("admin", timeout=30)
        masked = frame["data"]["result"]["items"]["embedding"]["apiKey"]
        print("settings.get apiKey:", masked)
        assert masked.startswith("…"), "读取必须脱敏"
        raw = Database(DB)
        try:
            stored = raw.get_setting("embedding")
        finally:
            raw.close()
        print("app_setting 明文 key 长度:", len(stored.get("apiKey") or ""),
              "| baseUrl:", stored.get("baseUrl"))
        assert stored["apiKey"] == "sk-e2e-123456", "本地库应保存完整 key"

        print(f"embed.request 次数 {shell.embed_calls}；错误 {shell.embed_errors}")
        assert not shell.embed_errors, shell.embed_errors
        print("E2E OK")
        return 0
    finally:
        shell.close()


if __name__ == "__main__":
    raise SystemExit(main())

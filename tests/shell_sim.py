# -*- coding: utf-8 -*-
"""极简外壳模拟器：跑 `src/main.py` 子进程，代答 llm.request。

用途：在不接真实 JS 外壳的情况下端到端验证内核（ingest → 摘要 → 落库 → 检索）。
模型调用直接打本地 workbuddy 的 OpenAI 兼容接口，密钥从环境变量或
`~/.dsh/.credentials.yaml` 读取（不落日志、不打印）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8788/v1"
DEFAULT_MODEL = "glm-5.3"


def load_api_key() -> str:
    key = os.environ.get("WORKBUDDY_API_KEY", "").strip()
    if key:
        return key
    path = Path.home() / ".dsh" / ".credentials.yaml"
    if not path.exists():
        return ""
    in_refs = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "refs:":
            in_refs = True
            continue
        if in_refs and line and not line.startswith((" ", "\t")):
            break
        if in_refs and "WORKBUDDY_API_KEY:" in line:
            return line.split("WORKBUDDY_API_KEY:", 1)[1].strip().strip('"').strip("'")
    return ""


class ShellSim:
    '''最小外壳：发帧、收帧、代答 llm.request。'''

    def __init__(self, workdir: Path, *, model: str = DEFAULT_MODEL,
                 api_key: str | None = None, base_url: str = DEFAULT_BASE_URL,
                 session_id: str = "sim-session", agent_id: str = "sim"):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.api_key = api_key if api_key is not None else load_api_key()
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.agent_id = agent_id
        self.model_calls = 0
        self.model_errors: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr = None
        self._frames: list[dict] = []
        self._cond = threading.Condition()
        self._seq = 0
        self._lock = threading.Lock()

    # ---- 生命周期 ----
    def start(self):
        self._stderr = open(self.workdir / "kernel.stderr.log", "w", encoding="utf-8")
        self._proc = subprocess.Popen(
            [sys.executable, str(ROOT / "src" / "main.py")],
            cwd=str(self.workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr, text=True, encoding="utf-8", bufsize=1)
        self._reader = threading.Thread(target=self._read_loop, name="shell-reader",
                                        daemon=True)
        self._reader.start()
        return self

    def close(self, timeout: float = 5.0):
        if self._proc is None:
            return
        try:
            self.send("shutdown", {"drain_ms": int(timeout * 1000)})
            self._proc.wait(timeout=timeout)
        except Exception:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()
        finally:
            for stream in (self._proc.stdin, self._proc.stdout):
                try:
                    stream.close()
                except Exception:
                    pass
            if self._stderr:
                self._stderr.close()
            self._proc = None

    # ---- 收发 ----
    def send(self, frame_type: str, data, request_id: str | None = None,
             job_id: str | None = None):
        with self._lock:
            self._seq += 1
            rid = request_id or f"req_{self._seq:04d}"
        frame = {
            "protocol_version": "1.0", "plugin_version": "0.1.0",
            "type": frame_type, "request_id": rid, "job_id": job_id,
            "session_id": self.session_id, "agent_id": self.agent_id,
            "ts": datetime.now().astimezone().isoformat(),
            "data": data,
        }
        self._proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()
        return rid

    def wait(self, frame_type: str, timeout: float = 60.0) -> dict | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for index, frame in enumerate(self._frames):
                    if frame.get("type") == frame_type:
                        return self._frames.pop(index)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def _read_loop(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("type") == "llm.request":
                threading.Thread(target=self._handle_llm, args=(frame,),
                                 daemon=True).start()
                continue
            with self._cond:
                self._frames.append(frame)
                self._cond.notify_all()

    # ---- 模型代理 ----
    def _handle_llm(self, frame: dict):
        payload = frame.get("data") or {}
        request_id = payload.get("request_id") or ""
        try:
            content = self.chat(payload.get("messages") or [])
            result = {"request_id": request_id, "content": content,
                      "model": self.model, "usage": {}, "error": None}
        except Exception as exc:
            self.model_errors.append(str(exc))
            result = {"request_id": request_id, "content": "",
                      "error": {"code": 1004, "message": "model_error",
                                "detail": str(exc)}}
        self.send("llm.result", result, request_id=frame.get("request_id") or "")

    def chat(self, messages: list) -> str:
        body = json.dumps({"model": self.model, "messages": messages,
                           "temperature": 0}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(request, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        self.model_calls += 1
        return payload["choices"][0]["message"].get("content") or ""

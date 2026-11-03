# -*- coding: utf-8 -*-
"""模型调用的外壳代理客户端（内核侧）。

内核**不持有** provider / model / key：所有模型调用打成 `llm.request` 帧交给外壳，
外壳调用模型后按同一 `request_id` 回 `llm.result`。本模块只做三件事：

1. 构造并投递上行 `llm.request`（`submit` 非阻塞，返回 Future）；
2. 待应答表：`llm.result` 到达时按 `request_id` 唤醒等待方（`on_result`）；
3. 超时与错误的统一出口（`LlmError`）。

线程约束：`request()` 会阻塞等待，**只能在 worker 线程调用**；主循环用
`submit()` 拿 Future，由 `on_result` 异步唤醒，避免读 stdin 被阻塞。
"""
from __future__ import annotations

import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field

import base
import config

DEFAULT_TIMEOUT = 120.0

# 与 docs/Transmission.md §3.11 错误码表一致：模型不可用
CODE_MODEL_UNAVAILABLE = 1004


class LlmError(RuntimeError):
    '''
    模型调用失败：超时、外壳回错、队列满、应答缺字段。

    :param code: 外壳返回的业务错误码（如 1004 模型不可用）；None 表示本地错误。
                 调用方据此判断是否值得重试。
    '''

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class LlmResult:
    '''一次模型调用的结果；content 是模型原文，解析由调用方负责。'''
    request_id: str
    content: str
    model: str = ""
    usage: dict = field(default_factory=dict)


class LlmClient:
    '''把模型调用代理给外壳：上行 llm.request，下行 llm.result。'''

    def __init__(self, q: base.message_queue, *, timeout: float = DEFAULT_TIMEOUT):
        self._q = q
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._pending: dict[str, Future] = {}
        self._seq = 0

    # ---- 上行 ----

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"llm_{self._seq:06d}"

    def submit(self, messages, *, purpose: str = "",
               envelope: dict | None = None,
               response_format: dict | None = None,
               timeout_ms: int | None = None,
               model_hint: str | None = None,
               think: bool = False) -> Future:
        '''
        投递一次 llm.request，立即返回 Future（不阻塞）。

        :param messages: [{"role": "system|user|assistant", "content": str}]
        :param purpose: 调用用途，jev / summary，便于外壳选模型与限流
        :param envelope: 入站信封，用于继承 session_id / agent_id / job_id
        :param response_format: 例 {"type": "json_object"}；None 表示不限
        :param timeout_ms: 外壳侧超时；缺省用本客户端 timeout
        :param model_hint: 外壳侧模型提示，内核不强制
        :param think: 是否请求深思（G2）
        '''
        request_id = self._next_id()
        future: Future = Future()
        future.request_id = request_id
        with self._lock:
            self._pending[request_id] = future

        payload = {
            "request_id": request_id,
            "purpose": purpose,
            "messages": list(messages),
            "response_format": response_format,
            "timeout_ms": int(timeout_ms if timeout_ms is not None
                             else self.timeout * 1000),
            "think": bool(think),
        }
        if model_hint:
            payload["model_hint"] = model_hint

        try:
            frame = config.build_frame("llm.request", payload, envelope or {})
        except Exception as exc:
            self._drop(request_id)
            raise LlmError(f"llm.request 构造失败: {exc}") from exc

        if not self._q.send_blocking(frame):
            self._drop(request_id)
            raise LlmError("消息队列已满，llm.request 未能投递")
        return future

    def request(self, messages, *, timeout: float | None = None, **kwargs) -> LlmResult:
        '''投递并等待结果；只能在 worker 线程调用（主循环会被阻塞）。'''
        future = self.submit(messages, **kwargs)
        wait = self.timeout if timeout is None else float(timeout)
        try:
            return future.result(timeout=wait)
        except FutureTimeout:
            raise LlmError(
                f"llm.request 超时（{wait:.0f}s，request_id={future.request_id}）") from None
        finally:
            self._drop(future.request_id)

    def _drop(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    # ---- 下行 ----

    def on_result(self, data: dict, q: base.message_queue | None = None) -> bool:
        '''
        llm.result 处理器：按 data.request_id 唤醒等待方。

        :return: 命中待应答表返回 True（已交付结果或异常）；否则 False
        '''
        payload = data.get("data") or {}
        request_id = payload.get("request_id") or data.get("request_id") or ""
        with self._lock:
            future = self._pending.pop(request_id, None)

        if future is None:
            base.log.warning("llm.result.unmatched", request_id=request_id)
            return False
        if future.done():
            return False

        error = payload.get("error")
        if error:
            code = None
            if isinstance(error, dict):
                message = error.get("message") or "模型调用失败"
                detail = error.get("detail") or ""
                text = f"{message}: {detail}".rstrip(": ")
                raw_code = error.get("code")
                if raw_code is not None:
                    text = f"[{raw_code}] {text}"
                    try:
                        code = int(raw_code)
                    except (TypeError, ValueError):
                        code = None
            else:
                text = str(error)
            future.set_exception(LlmError(text, code=code))
            return True

        content = payload.get("content")
        if content is None:
            future.set_exception(LlmError("llm.result 缺少 content"))
            return True

        future.set_result(LlmResult(
            request_id=request_id,
            content=str(content),
            model=payload.get("model") or "",
            usage=payload.get("usage") or {},
        ))
        return True

    @property
    def pending(self) -> int:
        '''待应答的调用数（health / 排障用）。'''
        with self._lock:
            return len(self._pending)


# ---- 进程内单例：main() 绑定队列，路由与业务模块按需取用 ----

_client: LlmClient | None = None
_client_lock = threading.Lock()


def bind(q: base.message_queue, *, timeout: float = DEFAULT_TIMEOUT) -> LlmClient:
    '''绑定消息队列（main 在启动时调用一次）。'''
    global _client
    with _client_lock:
        _client = LlmClient(q, timeout=timeout)
        return _client


def get_client() -> LlmClient:
    if _client is None:
        raise RuntimeError("LLM 客户端未初始化：main() 需要先调用 llm.bind(queue)")
    return _client


def on_result(data: dict, q: base.message_queue | None = None) -> bool:
    '''路由入口：把 llm.result 交给当前客户端。'''
    return get_client().on_result(data, q)

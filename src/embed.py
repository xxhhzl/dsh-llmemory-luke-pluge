# -*- coding: utf-8 -*-
"""Embedding 的外壳代理客户端（内核侧）。

内核不持有端点与 key：文本打成 `embed.request` 帧交给外壳，外壳调用
OpenAI 兼容 `/v1/embeddings`（或 Ollama `/api/embed`）后按 `request_id`
回 `embed.result`。

只在 worker 线程使用（写入索引 / 回填）；读路径的 query 向量由外壳在
`tools.calls` 里直接带上，避免阻塞主循环。
"""
from __future__ import annotations

import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

import base
import config
import database

DEFAULT_TIMEOUT = 60.0


class EmbedError(RuntimeError):
    def __init__(self, message: str, *, code=None):
        super().__init__(message)
        self.code = code


class EmbedClient:
    def __init__(self, q: base.message_queue, *, timeout: float = DEFAULT_TIMEOUT):
        self._q = q
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._pending: dict[str, Future] = {}
        self._seq = 0

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"emb_{self._seq:06d}"

    def request(self, texts, *, timeout: float | None = None,
                envelope: dict | None = None) -> list:
        request_id = self._next_id()
        future: Future = Future()
        with self._lock:
            self._pending[request_id] = future
        payload = {"request_id": request_id, "input": list(texts),
                   "timeout_ms": int((self.timeout if timeout is None else timeout) * 1000)}
        frame = config.build_frame("embed.request", payload, envelope or {})
        if not self._q.send_blocking(frame):
            self._drop(request_id)
            raise EmbedError("消息队列已满，embed.request 未能投递")
        wait = self.timeout if timeout is None else float(timeout)
        try:
            return future.result(timeout=wait)
        except FutureTimeout:
            raise EmbedError(f"embed.request 超时（{wait:.0f}s）") from None
        finally:
            self._drop(request_id)

    def _drop(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def on_result(self, data: dict, q=None) -> bool:
        payload = data.get("data") or {}
        request_id = payload.get("request_id") or data.get("request_id") or ""
        with self._lock:
            future = self._pending.pop(request_id, None)
        if future is None:
            base.log.warning("embed.result.unmatched", request_id=request_id)
            return False
        error = payload.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            future.set_exception(EmbedError(message or "embedding 调用失败"))
            return True
        vectors = payload.get("vectors")
        if not isinstance(vectors, list):
            future.set_exception(EmbedError("embed.result 缺少 vectors"))
            return True
        future.set_result(vectors)
        return True


_client: EmbedClient | None = None
_client_lock = threading.Lock()


def bind(q: base.message_queue, *, timeout: float = DEFAULT_TIMEOUT) -> EmbedClient:
    global _client
    with _client_lock:
        _client = EmbedClient(q, timeout=timeout)
        return _client


def get_client() -> EmbedClient:
    if _client is None:
        raise RuntimeError("Embedding 客户端未初始化：main() 需要先调用 embed.bind(queue)")
    return _client


def on_result(data: dict, q=None) -> bool:
    return get_client().on_result(data, q)


def text_for(kind: str, row) -> str:
    '''条目 → 嵌入文本（与 docs/core_memory 的口径一致）。'''
    cues = " ".join(getattr(row, "cues", ()) or ())
    if kind == "experience":
        return " ".join(x for x in (getattr(row, "context", "") or "",
                                    row.content, cues) if x)
    if kind == "cognition":
        return " ".join(x for x in (getattr(row, "subject", "") or "",
                                    row.content, cues) if x)
    return " ".join(x for x in (row.content, cues) if x)


def index_rows(db: database.Database, items: list, *, timeout: float | None = None,
               on_error=None) -> int:
    '''
    items: [(kind, row)]；批量 embedding 后写入 vec 表。

    :param on_error: 失败时回调（收到异常）。调用方（admin 的重建索引）靠它
                     把失败原因带回界面——只返回 0 的话，「全部失败」和
                     「没有条目」分不开，界面会假报「完成」。
    :return: 实际写入条数
    '''
    if not getattr(db, "_vec_enabled", False) or not items:
        return 0
    texts = [text_for(kind, row) for kind, row in items]
    try:
        vectors = get_client().request(texts, timeout=timeout)
    except Exception as exc:
        base.log.warning("embed.index_failed", error=repr(exc))
        if on_error is not None:
            on_error(exc)
        return 0
    if len(vectors) != len(texts):
        exc = EmbedError(f"embedding 返回 {len(vectors)} 条，期望 {len(texts)} 条")
        base.log.warning("embed.index_count_mismatch",
                         got=len(vectors), want=len(texts))
        if on_error is not None:
            on_error(exc)
        return 0
    written = 0
    for (kind, row), vector in zip(items, vectors):
        try:
            if db.vec_upsert(kind, row.id, vector, getattr(row, "origin_project", None)):
                written += 1
        except Exception as exc:
            base.log.warning("embed.vec_write_failed", kind=kind, id=row.id,
                             error=repr(exc))
    return written


def index_event(db: database.Database, event_id: int) -> int:
    items = []
    for kind in ("experience", "cognition"):
        for row in db.list_l3_by_event(kind, event_id):
            if getattr(row, "status", "active") == "active":
                items.append((kind, row))
    return index_rows(db, items)

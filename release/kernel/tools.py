# -*- coding: utf-8 -*-
"""工具：memory_search / memory_get / memory_recent。

工具是声明式的：schema 供外壳拉取（tools.schema），执行走 tools.calls → tools.result。
检索细节在 retrieval.py，本模块只做**参数校验、上下文注入、结果整形**。

两条约定（2026-11-03 定稿）：
- 参数在**执行时**用 args 模型校验一次，非法值回 -32602——schema 只是给模型看的，
  不能当防线（旧实现直接 `entry[1](**args)`，非法 scope 会静默返回 0 条）；
- 范围是**数据的固有属性**，检索侧只决定「本次要不要扩大」：`scope` 缺省取
  `config.search.default_scope`，`origin_project` 由外壳经信封透传，缺失视为未知。
"""
from __future__ import annotations

import inspect
import threading
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from functools import wraps

import base
import config
import database
import frame
import retrieval

# 工具注册表：name -> (schema, func, args_class)
tools = dict()

# 引用无法解析（docs/Transmission.md 的错误码）
CODE_NOT_FOUND = 1003


class ToolError(Exception):
    '''工具层的业务错误：带错误码，由 tools_call 转成 tools.result 的 error。'''

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def tool(args_class, strict=True, additionalProperties=False):
    def decorator(func):
        schema = args_class.model_json_schema()
        # 去掉 pydantic 自动补的 title，只保留对模型有意义的约束
        properties = {
            name: {key: value for key, value in spec.items() if key != "title"}
            for name, spec in schema.get("properties", {}).items()
        }
        j = {
            "name": func.__name__,
            "description": func.__doc__.strip(),
            "strict": strict,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": schema.get("required", []),
                "additionalProperties": additionalProperties,
            },
        }
        if func.__name__ in tools:
            raise ValueError(f"工具重复注册: {func.__name__}")
        tools[func.__name__] = (j, func, args_class)

        @wraps(func)
        def wr(*args, **kwargs):
            return func(*args, **kwargs)

        return wr
    return decorator


_db_cache: dict = {}
_db_lock = threading.Lock()


def _db() -> database.Database:
    '''
    数据库入口（进程内复用连接）。

    旧实现每次工具调用都 `Database()`：新建连接 + `executescript` 全部 DDL +
    关闭时 `wal_checkpoint(TRUNCATE)`，检索热路径上每次都在做建表校验与截断。
    这里按库路径缓存一个连接；测试可直接替换 `tools._db`。
    '''
    from pathlib import Path
    import config
    path = str(Path((config.get_config().get("store") or {}).get("db_path")
                    or "memory.db").resolve())
    with _db_lock:
        db = _db_cache.get(path)
        if db is None:
            db = database.Database(path)
            _db_cache[path] = db
        return db


def reset_db_cache():
    '''关闭并清空缓存（测试与 shutdown 用）。'''
    with _db_lock:
        for db in _db_cache.values():
            try:
                db.close()
            except Exception:
                pass
        _db_cache.clear()


def _search_defaults() -> dict:
    '''检索相关的运行期配置（每次调用现读，config.set 立即生效）。'''
    return config.get_config().get("search", {})


# ---------------- 工具定义 ----------------

class memory_search_args(BaseModel):
    query: str = Field(description=(
        "检索关键词：用名词或短语，多个关键词用空格分隔（默认要求同时出现，"
        "缺一个会自动放宽）。不要写整句提问——整句里只有一两个词真正能命中"))
    limit: int = Field(default=5, ge=1, le=100,
                       description="返回条数，默认 5；上限受配置 search.limit_max（默认 20）约束")
    kind: Literal["event", "experience", "cognition"] | None = Field(
        default=None,
        description="限定类型：event=事件摘要、experience=做法、cognition=事实/偏好；省略=三类都搜")
    scope: Literal["project", "all"] | None = Field(
        default=None,
        description="project=本项目+通用；all=跨项目；省略取配置默认值（当前 all）")
    min_score: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="融合分下限（0–1），低于它的不返回；一般省略")


@tool(memory_search_args)
def memory_search(query, limit=5, kind=None, scope=None, min_score=None,
                  *, session_id=None, origin_project=None):
    '''在长期记忆里找线索：返回事件摘要、经验、认知（按相关性 × 记忆强度排序）。先用它定位拿 id，再用 memory_get 读正文；问「刚才那段时间」用 memory_recent。'''
    search_cfg = _search_defaults()
    effective_limit = max(1, min(int(limit), int(search_cfg.get("limit_max", 20))))
    effective_scope = scope or search_cfg.get("default_scope", "all")
    if effective_scope not in ("project", "all"):
        raise ToolError(-32602, f"scope 只能是 project / all，收到 {effective_scope!r}")
    threshold = float(search_cfg.get("min_score", 0.0) if min_score is None else min_score)
    kinds = ((kind,) if kind else ("event", "experience", "cognition"))
    db = _db()          # 共享连接：不能用 with，退出时会把它关掉
    hits = retrieval.search(db, query, scope=effective_scope,
                            origin_project=origin_project,
                            limit=effective_limit, kinds=kinds)
    items = [hit.to_dict() for hit in hits if hit.score >= threshold]
    return {"items": items, "count": len(items),
            "scope": effective_scope, "limit": effective_limit}


class memory_get_args(BaseModel):
    id: int = Field(description="条目 id（来自 memory_search 返回的 items[].id）")
    kind: Literal["event", "experience", "cognition"] = Field(
        default="event",
        description="id 的类型，必须与来源一致：event / experience / cognition")


@tool(memory_get_args)
def memory_get(id, kind="event"):
    '''按 id 读一条完整记忆：event 返回事件行 + 全部正文消息；经验/认知返回整行。'''
    db = _db()          # 共享连接，勿关闭
    out = retrieval.get(db, kind, int(id))
    if not out:
        raise ToolError(CODE_NOT_FOUND, f"找不到 {kind} #{id}")
    return out


class memory_recent_args(BaseModel):
    from_at: str = Field(description=(
        "窗口起点，ISO 8601 且带时区，例如 2026-11-04T08:30:00+08:00（内部归一到 UTC）"))
    to_at: str = Field(description=(
        "窗口终点，ISO 8601 且带时区，例如 2026-11-04T09:45:00+08:00（含边界，内部归一到 UTC）"))
    session_id: str | None = Field(
        default=None, description="会话 id；省略=信封里的当前会话")
    limit: int = Field(default=10, ge=1, le=50,
                       description="返回条数：事件占大头，经验/认知各最多 3 条")


@tool(memory_recent_args)
def memory_recent(from_at, to_at, session_id=None, limit=10,
                  *, origin_project=None):
    '''按时间区间回放：那段时间发生了什么（事件摘要）+ 沉淀了什么（经验/认知）。'''
    db = _db()          # 共享连接，勿关闭
    hits = retrieval.recent(db, session_id or "", from_at=from_at,
                            to_at=to_at, limit=limit,
                            origin_project=origin_project)
    return {"items": [hit.to_dict() for hit in hits], "count": len(hits)}


class memory_report_args(BaseModel):
    id: int = Field(description="要上报的条目 id（来自 memory_search 的 items[].id）")
    kind: Literal["event", "experience", "cognition"] = Field(
        default="event",
        description="id 的类型，必须与来源一致：event / experience / cognition")
    reason: str = Field(description="哪里不对：与事实不符、已过时、张冠李戴等，一句话")


@tool(memory_report_args)
def memory_report(id, kind="event", reason=""):
    '''上报一条记忆有问题：事件置「待复核」，经验/认知置为待复核（不再参与排序靠前）。只标状态，不改内容。'''
    db = _db()
    note = (reason or "").strip()[:120] or "reported"
    if kind == "event":
        if not db.set_event_review(int(id), True, note):
            raise ToolError(CODE_NOT_FOUND, f"找不到 event #{id}")
        return {"kind": "event", "id": int(id), "needs_review": True,
                "reason": note}
    if kind in ("experience", "cognition"):
        # 错条目：置 invalid 并摘掉索引，避免继续被召回；正文保留可追溯
        with db.transaction() as conn:
            cur = conn.execute(
                f"UPDATE {kind} SET status = 'invalid', invalid_reason = ? "
                f"WHERE id = ?", (note, int(id)))
            if cur.rowcount != 1:
                raise ToolError(CODE_NOT_FOUND, f"找不到 {kind} #{id}")
            conn.execute(
                "DELETE FROM memory_fts WHERE ref_kind = ? AND ref_id = ?",
                (kind, int(id)))
        return {"kind": kind, "id": int(id), "status": "invalid",
                "reason": note}
    raise ToolError(-32602, f"未知 kind: {kind!r}")


# ---------------- 运行状态 ----------------

def stats(data, q: base.message_queue):
    '''
    stats：给外壳的设置页用——库路径、条数、当前会话最近事件。
    只报告现状，不改内容、不计使用次数。
    '''
    db = _db()
    session_id = data.get("session_id") or ""
    latest = None
    if session_id:
        events = db.list_events(session_id, limit=1)
        if events:
            event = events[0]
            latest = {"id": event.id, "title": event.title,
                      "summary": event.summary, "created_at": event.created_at,
                      "needs_review": bool(event.needs_review)}
    frame.push(q, data, "stats", {
        "db_path": str(db.path),
        # 外壳用它显示“内壳版本”：改完不重启会一直是旧值，便于发现未生效
        "plugin_version": config.PLUGIN_VERSION,
        "protocol_version": config.PROTOCOL_VERSION,
        "messages": db.count_messages(),
        "events": db.count_events(),
        "experiences": db.count_l3("experience"),
        "cognitions": db.count_l3("cognition"),
        "session_id": session_id,
        "latest_event": latest,
    })


# ---------------- 下发与执行 ----------------

def get_tools_schema(data, q: base.message_queue):
    frame.push(q, data, "tools.schema",
               [schema for (schema, func, args) in tools.values()],
               detail="消息队列已满，tools.schema 未能下发")


def _error(call_id, message: str, code: int = -32602) -> dict:
    return {"call_id": call_id, "ok": False,
            "error": {"code": code, "message": message}}


def _merge_context(func, validated, context: dict) -> dict:
    '''
    合并「调用参数」与「信封上下文」：只注入函数确实接收的字段，
    且调用方显式给了值时不覆盖（None 视为未给）。
    '''
    kwargs = validated.model_dump()
    accepted = inspect.signature(func).parameters
    for key, value in context.items():
        if key in accepted and kwargs.get(key) is None:
            kwargs[key] = value
    return kwargs


def tools_call(data, q: base.message_queue):
    '''
    执行外壳下发的工具调用，按 call_id 回 tools.result。

    上下文注入：信封里的 `session_id` / `origin_project`（后者可由外壳放在
    data 或信封里）传给工具——否则「默认只看当前项目」永远无从实现。
    '''
    payload = data.get("data") or {}
    calls = payload.get("calls")
    if not isinstance(calls, list):
        from error import send_error_message
        send_error_message(data, q, -32602, "参数不合法", "calls 必须是数组")
        return

    context = {
        "session_id": payload.get("session_id") or data.get("session_id"),
        "origin_project": (payload.get("origin_project")
                           or data.get("origin_project")),
    }

    results = []
    for call in calls:
        call_id = call.get("call_id") or ""
        name = call.get("name") or ""
        args = call.get("arguments") or {}
        entry = tools.get(name)
        if entry is None:
            results.append(_error(call_id, f"未知工具: {name}", -32601))
            continue
        if not isinstance(args, dict):
            results.append(_error(call_id, "arguments 必须是对象"))
            continue
        schema, func, args_class = entry
        try:
            validated = args_class.model_validate(args)
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first.get("loc", ())) or "arguments"
            results.append(_error(call_id,
                                  f"参数不合法: {field} {first.get('msg', '')}".strip()))
            continue
        try:
            output = func(**_merge_context(func, validated, context))
        except ToolError as exc:
            results.append(_error(call_id, str(exc), exc.code))
            continue
        except TypeError as exc:
            results.append(_error(call_id, f"参数错误: {exc}"))
            continue
        except ValueError as exc:          # 参数语义错误（时间格式/顺序等）
            results.append(_error(call_id, f"参数不合法: {exc}"))
            continue
        except Exception as exc:            # 工具内部失败不拖垮整批
            results.append(_error(call_id, f"执行失败: {exc}", -32603))
            continue
        results.append({"call_id": call_id, "ok": True, "data": output})

    frame.push(q, data, "tools.result", results,
               detail="消息队列已满，tools.result 未能下发")

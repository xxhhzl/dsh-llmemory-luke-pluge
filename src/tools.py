# -*- coding: utf-8 -*-
"""工具：memory_search / memory_get / memory_recent。

工具是声明式的：schema 供外壳拉取（tools.schema），执行走 tools.calls → tools.result。
检索细节在 retrieval.py，本模块只做参数校验、数据库生命周期与结果整形。
"""
from __future__ import annotations

from pydantic import BaseModel, Field
from functools import wraps

import base
import database
import frame
import retrieval

# 工具注册表：name -> (schema, func)
tools = dict()


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
        tools.setdefault(func.__name__, (j, func))

        @wraps(func)
        def wr(*args, **kwargs):
            return func(*args, **kwargs)

        return wr
    return decorator


def _db() -> database.Database:
    '''数据库入口；测试可替换为临时库。'''
    return database.Database()


# ---------------- 工具定义 ----------------

class memory_search_args(BaseModel):
    query: str = Field(description="检索语句")
    limit: int = Field(default=5, ge=1, le=20, description="返回条数")
    kind: str | None = Field(
        default=None,
        description="限定类型：event / experience / cognition；省略表示全部")
    scope: str = Field(default="all",
                       description="project=只看本项目与通用；all=跨项目")


@tool(memory_search_args)
def memory_search(query, limit=5, kind=None, scope="all"):
    '''检索长期记忆：事件摘要、经验、认知，按相关性 × 记忆强度排序。'''
    kinds = (kind,) if kind else ("event", "experience", "cognition")
    with _db() as db:
        hits = retrieval.search(db, query, scope=scope, limit=limit, kinds=kinds)
    return {"items": [hit.to_dict() for hit in hits], "count": len(hits)}


class memory_get_args(BaseModel):
    id: int = Field(description="条目 id")
    kind: str = Field(default="event",
                      description="event / experience / cognition")


@tool(memory_get_args)
def memory_get(id, kind="event"):
    '''按 id 取一条记忆：event 返回事件与全部正文消息，l3 返回整行。'''
    with _db() as db:
        return retrieval.get(db, kind, int(id))


class memory_recent_args(BaseModel):
    session_id: str = Field(description="会话 id")
    from_at: str = Field(description="起始时间（ISO8601，含）")
    to_at: str = Field(description="结束时间（ISO8601，含）")
    limit: int = Field(default=10, ge=1, le=50, description="返回条数")


@tool(memory_recent_args)
def memory_recent(session_id, from_at, to_at, limit=10):
    '''按时间区间取事件摘要与经验/认知，用于「刚才那段时间聊了什么」。'''
    with _db() as db:
        hits = retrieval.recent(db, session_id, from_at=from_at, to_at=to_at,
                                limit=limit)
    return {"items": [hit.to_dict() for hit in hits], "count": len(hits)}


# ---------------- 下发与执行 ----------------

def get_tools_schema(data, q: base.message_queue):
    frame.push(q, data, "tools.schema",
               [schema for (schema, func) in tools.values()],
               detail="消息队列已满，tools.schema 未能下发")


def _error(call_id, message: str, code: int = -32602) -> dict:
    return {"call_id": call_id, "ok": False,
            "error": {"code": code, "message": message}}


def tools_call(data, q: base.message_queue):
    '''执行外壳下发的工具调用，按 call_id 回 tools.result。'''
    payload = data.get("data") or {}
    calls = payload.get("calls")
    if not isinstance(calls, list):
        from error import send_error_message
        send_error_message(data, q, -32602, "参数不合法", "calls 必须是数组")
        return

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
        try:
            output = entry[1](**args)
        except TypeError as exc:
            results.append(_error(call_id, f"参数错误: {exc}"))
            continue
        except Exception as exc:            # 工具内部失败不拖垮整批
            results.append(_error(call_id, f"执行失败: {exc}", -32603))
            continue
        results.append({"call_id": call_id, "ok": True, "data": output})

    frame.push(q, data, "tools.result", results,
               detail="消息队列已满，tools.result 未能下发")

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

import difflib
import inspect
import re
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
    kind: Literal["event", "experience", "cognition", "insight"] | None = Field(
        default=None,
        description=("限定类型：event=事件摘要、experience=做法、cognition=事实/偏好、"
                     "insight=多条经验归纳出的规则；省略=四类都搜"))
    scope: Literal["project", "all"] | None = Field(
        default=None,
        description="project=本项目+通用；all=跨项目；省略取配置默认值（当前 all）")
    min_score: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="融合分下限（0–1），低于它的不返回；一般省略")


@tool(memory_search_args)
def memory_search(query, limit=5, kind=None, scope=None, min_score=None,
                  *, session_id=None, origin_project=None, query_vector=None):
    '''在长期记忆里找线索（事件摘要 / 经验 / 认知 / 洞见，按相关性 × 记忆强度排序）。

何时用：用户提到「之前 / 上次 / 我们讨论过 / 你说过 / 偏好 / 约定」，或这次任务要沿用既有决定、项目约定、踩过的坑时；不确定记忆里有没有，也值得先查一次。
何时不用：只是定位当前代码或文件用 grep / read；本次对话里已经给出的信息不必再查。
用法：先拿到 id、摘要和 meta.excerpt（命中片段）；回答需要具体名称、命令、参数、数字或 URL 时，必须对分数最高的 1–3 条调 memory_get 读原文，只确认「有没有/是不是」可停在摘要。时间线问题（「之前让你做什么」）优先用 memory_recent。
读结果：score 是相关性 × 记忆强度的融合分，meta.use_count 是使用热度；记忆是「当时记下的」，与当前事实冲突时以当前为准。按 next_action_hint 决定是否换工具或继续深挖。'''
    search_cfg = _search_defaults()
    effective_limit = max(1, min(int(limit), int(search_cfg.get("limit_max", 20))))
    effective_scope = scope or search_cfg.get("default_scope", "all")
    if effective_scope not in ("project", "all"):
        raise ToolError(-32602, f"scope 只能是 project / all，收到 {effective_scope!r}")
    threshold = float(search_cfg.get("min_score", 0.0) if min_score is None else min_score)
    kinds = ((kind,) if kind else ("event", "experience", "cognition", "insight"))
    db = _db()          # 共享连接：不能用 with，退出时会把它关掉
    hits = retrieval.search(db, query, scope=effective_scope,
                            origin_project=origin_project,
                            limit=effective_limit, kinds=kinds,
                            query_vector=query_vector)
    items = [hit.to_dict() for hit in hits if hit.score >= threshold]
    if not items:
        next_hint = ("没有命中。若问的是「之前/上次/那段时间做过什么」，"
                     "改用 memory_recent 并省略 session_id 跨会话回放；"
                     "也可换更具体的名词重试 memory_search。")
    elif any(item["kind"] == "event" for item in items):
        next_hint = ("事件条目的 text 是摘要，meta.excerpt 是命中片段。"
                     "答案需要具体名称/命令/数字/URL 时，对最相关的 1–3 条事件调 "
                     "memory_get(kind='event') 读原文；正文截断时用返回的 "
                     "next_msg_offset 续读。若问题其实按时间线提问，改用 memory_recent。")
    else:
        next_hint = ("这些经验/认知/洞见已含完整正文。若需要它们来自哪次任务的细节，"
                     "用返回的 source_event_id 对 memory_get(kind='event') 深挖；"
                     "若问题按时间线提问，改用 memory_recent。")
    # 可观测：让调用方分清「真的没有」「被 limit 切了」「被 scope 滤了」
    return {"items": items, "count": len(items),
            "scope": effective_scope, "scope_applied": effective_scope,
            "limit": effective_limit,
            # 截断发生在 min_score 过滤之前，用原始命中数判断更准
            "has_more": len(hits) >= effective_limit or len(items) < len(hits),
            "next_action_hint": next_hint,
            "filters": {"kind": kind or "all", "min_score": threshold},
            "total": None,      # FTS 只取 top-50，给不出全局总数，如实置空
            }


class memory_get_args(BaseModel):
    id: int = Field(description="条目 id（来自 memory_search 返回的 items[].id）")
    kind: Literal["event", "experience", "cognition", "insight"] = Field(
        default="event",
        description="id 的类型，必须与来源一致：event / experience / cognition / insight")
    max_chars: int = Field(
        default=8000, ge=0, le=100000,
        description="事件正文单次字符预算，默认 8000；0=一次返回全部正文；"
                    "截断时用 next_msg_offset 续读，不必调大")
    msg_offset: int = Field(
        default=0, ge=0,
        description="正文消息起点；续读时原样传上一页返回的 next_msg_offset")


@tool(memory_get_args)
def memory_get(id, kind="event", max_chars=8000, msg_offset=0):
    '''按 id 读一条完整记忆：event 返回事件行 + 正文消息；经验/认知/洞见返回整行。

id 来自 memory_search 的 items[].id（先搜再读，不要猜 id）；看原文、核对细节时用它。答案需要具体名称、命令、数字或 URL 时必须读原文。正文默认限 8000 字符；truncated=true 时用 next_msg_offset 续读，可逐页读完而不撑爆上下文。'''
    db = _db()          # 共享连接，勿关闭
    out = retrieval.get(db, kind, int(id), max_chars=int(max_chars),
                        msg_offset=int(msg_offset))
    if not out:
        raise ToolError(CODE_NOT_FOUND, f"找不到 {kind} #{id}")
    return out


class memory_recent_args(BaseModel):
    from_at: str = Field(description=(
        "窗口起点，ISO 8601 且带时区，例如 2026-11-04T08:30:00+08:00（内部归一到 UTC）"))
    to_at: str = Field(description=(
        "窗口终点，ISO 8601 且带时区，例如 2026-11-04T09:45:00+08:00（含边界，内部归一到 UTC）"))
    session_id: str | None = Field(
        default=None,
        description="会话 id；**省略=跨会话回放全部事件**（「我之前/下午让你做什么」"
                    "这类问题就该省略它）；只在明确要限定当前会话时才传")
    limit: int = Field(default=10, ge=1, le=50,
                       description="返回条数：事件占大头，经验/认知各最多 3 条")


@tool(memory_recent_args)
def memory_recent(from_at, to_at, session_id=None, limit=10,
                  *, origin_project=None):
    '''按时间区间回放：那段时间发生了什么（事件摘要）+ 沉淀了什么（经验/认知）。

何时用：用户问「昨天 / 上周 / 那段时间 / 我之前让你做什么」做过什么——这类问题**靠时间线回忆而不是关键词**，应优先用它而不是 memory_search；`session_id` 省略时会跨会话找（别的会话里的事也能找到）。需要按主题找而不是按时间找时用 memory_search。'''
    db = _db()          # 共享连接，勿关闭
    # session_id 缺省为 None = 跨会话；信封里的当前会话不再默认回填，
    # 否则「我之前让你做什么」会被会话边界挡住（这正是 0.3.4 要修的缺口）
    hits = retrieval.recent(db, session_id or None, from_at=from_at,
                            to_at=to_at, limit=limit,
                            origin_project=origin_project)
    return {"items": [hit.to_dict() for hit in hits], "count": len(hits)}


class memory_report_args(BaseModel):
    id: int = Field(description="要上报的条目 id（来自 memory_search 的 items[].id）")
    kind: Literal["event", "experience", "cognition", "insight"] = Field(
        default="event",
        description="id 的类型，必须与来源一致：event / experience / cognition / insight")
    reason: str = Field(description="哪里不对：与事实不符、已过时、张冠李戴等，一句话")


@tool(memory_report_args)
def memory_report(id, kind="event", reason=""):
    '''（兼容别名）等价于 memory_feedback(signal="wrong")：报告一条记忆有问题。'''
    note = (reason or "").strip()[:120] or "reported"
    result = memory_feedback([int(id)], kind=kind, signal="wrong", note=note)
    if not result["items"][0]["ok"]:
        raise ToolError(CODE_NOT_FOUND, f"找不到 {kind} #{int(id)}")
    if kind == "event":
        return {"kind": "event", "id": int(id), "needs_review": True,
                "reason": note}
    return {"kind": kind, "id": int(id), "status": "invalid", "reason": note}


class memory_feedback_args(BaseModel):
    ids: list[int] = Field(description=(
        "要标记的条目 id，可一次传多条（来自 memory_search 的 items[].id）"))
    kind: Literal["event", "experience", "cognition", "insight"] = Field(
        default="cognition",
        description="这些 id 的类型，必须与来源一致：event / experience / cognition / insight")
    signal: Literal["useful", "useless", "outdated", "wrong"] = Field(
        description=("useful=这次真帮上了（加权）；useless=召回了但没用（降权）；"
                     "outdated=内容已过时（退出召回）；wrong=与事实不符（置无效）"))
    note: str | None = Field(
        default=None, description="为什么这么标，≤120 字，便于审计")


@tool(memory_feedback_args)
def memory_feedback(ids, kind="cognition", signal="useful", note=None, *,
                    session_id=None, origin_project=None):
    '''给召回的记忆打分或改状态：useful/useless 只调权，outdated/wrong 会让它退出召回。'''
    targets = [int(item) for item in (ids or [])]
    if not targets:
        raise ToolError(-32602, "ids 不能为空")
    if kind not in ("event", "experience", "cognition", "insight"):
        raise ToolError(-32602, f"未知 kind: {kind!r}")
    if signal not in ("useful", "useless", "outdated", "wrong"):
        raise ToolError(-32602, f"未知 signal: {signal!r}")

    db = _db()
    note_text = (note or "").strip()[:120]
    now = base.get_cur_time()
    items = []
    with db.transaction() as conn:
        for item_id in targets:
            row = conn.execute(f"SELECT id FROM {kind} WHERE id = ?",
                               (item_id,)).fetchone()
            if row is None:
                items.append({"id": item_id, "ok": False, "reason": "not_found"})
                continue
            if kind == "event":
                # 事件表没有 feedback_score：用 cnt 表达有用/没用；
                # outdated/wrong 走统一失效入口（置 valid_to + 摘索引）
                if signal == "useful":
                    conn.execute("UPDATE event SET cnt = cnt + 1, "
                                 "last_used_at = ? WHERE id = ?", (now, item_id))
                elif signal == "useless":
                    conn.execute("UPDATE event SET cnt = MAX(cnt - 1, 0) "
                                 "WHERE id = ?", (item_id,))
                elif not db.retire_event(item_id, note_text or signal):
                    # 已经失效过：幂等返回成功，不把「重复上报」当成找不到
                    exists = conn.execute(
                        "SELECT 1 FROM event WHERE id = ?", (item_id,)).fetchone()
                    if exists is None:
                        items.append({"id": item_id, "ok": False,
                                      "reason": "not_found"})
                        continue
                    items.append({"id": item_id, "ok": True, "signal": signal,
                                  "note": "already_retired"})
                    continue
                items.append({"id": item_id, "ok": True, "signal": signal})
                continue

            if signal == "useful":
                conn.execute(
                    f"UPDATE {kind} SET use_count = use_count + 1, "
                    f"feedback_score = feedback_score + 1, last_used_at = ? "
                    f"WHERE id = ?", (now, item_id))
            elif signal == "useless":
                conn.execute(
                    f"UPDATE {kind} SET feedback_score = feedback_score - 1 "
                    f"WHERE id = ?", (item_id,))
            elif signal == "outdated":
                # 过时：置 valid_to 并摘掉 FTS / 向量（同事务内完成）
                if not db.mark_l3_outdated(kind, item_id, note_text or "outdated"):
                    items.append({"id": item_id, "ok": False,
                                  "reason": "not_active"})
                    continue
            else:
                # 与事实不符：置 invalid，同样摘掉 FTS / 向量
                if not db.invalidate_l3(kind, item_id, note_text or "wrong"):
                    items.append({"id": item_id, "ok": False,
                                  "reason": "not_active"})
                    continue
            items.append({"id": item_id, "ok": True, "signal": signal})

    updated = sum(1 for item in items if item["ok"])
    base.log.info("feedback", kind=kind, signal=signal, updated=updated)
    return {"kind": kind, "signal": signal, "updated": updated, "items": items}


class memory_self_write_args(BaseModel):
    content: str = Field(description=(
        "一句话自我认知（第一人称，≤300 字），写你自己的教训/行为准则/边界/目标；"
        "不要写用户或项目的事实——那些由系统自动提取"))
    kind: Literal["identity", "behavior", "limitation", "goal"] = Field(
        default="behavior",
        description="identity=身份、behavior=行为准则、limitation=能力边界、goal=目标")
    reason: str | None = Field(
        default=None, description="为什么写这条（可选，≤120 字，便于日后审计）")


@tool(memory_self_write_args)
def memory_self_write(content, kind="behavior", reason=None, *,
                      session_id=None, origin_project=None):
    '''给自己写一条自我认知：只影响你自己下一轮看到的动态上下文 <self_notes>，不会改用户/项目事实。'''
    text = str(content or "").strip()
    if not text:
        raise ToolError(-32602, "content 不能为空")
    if len(text) > 300:
        raise ToolError(-32602, "content 超过 300 字")
    if kind not in ("identity", "behavior", "limitation", "goal"):
        raise ToolError(-32602, f"未知 kind: {kind!r}")

    db = _db()
    rows = db.list_self_memory(source="agent", limit=1000)
    limit = int(config.get_config().get("core", {}).get("max_self_notes", 10))
    if len(rows) >= limit:
        raise ToolError(-32602, f"自述已达上限 {limit} 条，请让用户在设置页清理")
    revision = max([int(row.revision or 1) for row in rows], default=0) + 1
    new_id = db.insert_self_memory(database.SelfMemory(
        source="agent", kind=kind, content=text, revision=revision,
        origin_session=session_id, created_at=base.get_cur_time()))
    base.log.info("self.write", id=new_id, kind=kind, revision=revision)
    return {"id": new_id, "kind": kind, "revision": revision,
            "limit": limit, "content": text,
            "reason": (str(reason or "").strip()[:120])}


# ---------------- 写入：经验 / 认知 / 洞见 ----------------

_NOTE_MERGE_SIM = 0.90      # 向量余弦 ≥ 此值：判为重复，不新增
_NOTE_HINT_SIM = 0.82       # ≥ 此值：返回候选，由模型自己判断
_NOTE_LITERAL_HINT = 0.80   # 没有向量时的字面兜底：只提示，不拦
# 写入比读取严一档：读取时判定重复只是少显示一条，写入时误判就再也写不进去了


def _l3_record(db, kind: str, item_id: int):
    '''按 kind 取一条 l3 记录；不存在返回 None。'''
    if kind == "experience":
        return db.get_experience(item_id)
    if kind == "cognition":
        return db.get_cognition(item_id)
    return db.get_insight(item_id)


class memory_note_args(BaseModel):
    content: str = Field(description=(
        "一句话，第一人称，≤300 字。experience 写做法/技巧/坑；"
        "cognition 写关于用户、项目、工具的稳定事实；insight 写归纳出的通用规则"))
    kind: Literal["experience", "cognition", "insight"] = Field(
        default="experience",
        description="experience=怎么做；cognition=是什么（必须给 subject）；insight=归纳层规则")
    subject: str | None = Field(
        default=None,
        description="仅 cognition 必填，≤40 字；写清这是谁的事（用户 / 项目名 / 工具名 / 助手自己）")
    confidence: float = Field(default=0.55, ge=0.0, le=1.0,
                              description="你有多确定；被验证过一次再往上调")
    importance: int = Field(default=3, ge=1, le=5,
                            description="1–5；≥4 才会进每轮固定注入")
    cues: list[str] | None = Field(
        default=None, description="检索线索，2–5 个短词，后续靠它被联想起来")
    source_event_ids: list[int] | None = Field(
        default=None, description="这条由哪次事件得出；没有就不传")
    reason: str | None = Field(default=None, description="为什么写这条，≤120 字，便于审计")


def _norm_text(text) -> str:
    '''主体/正文归一：去空白、转小写；只用于「是不是同一主体」的比较。'''
    return re.sub(r"\s+", "", str(text or "")).lower()


def _subject_compatible(new_subject, row_subject) -> bool:
    '''同一主体：写法相同或互为包含（与 `find_l3_candidates` 的双向包含一致）。'''
    left, right = _norm_text(new_subject), _norm_text(row_subject)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def _project_compatible(new_project, row_project) -> bool:
    '''
    适用范围兼容：目标全局只和全局比；目标项目可与「同项目 + 全局」比。

    不同项目的同名事实是两件事（用户实测：「项目甲：默认中文回复」与
    「项目乙：默认中文回复」被旧的跨项目查重误判成 duplicate）。
    '''
    row_project = row_project or None
    if new_project is None:
        return row_project is None
    return row_project in (None, new_project)


def _note_candidate_ok(kind: str, row, subject: str,
                       origin_project: str | None) -> bool:
    '''新条目能否与这条旧条目比较：同 kind、范围兼容，认知还要同主体。'''
    if not _project_compatible(origin_project,
                               getattr(row, "origin_project", None)):
        return False
    if kind == "cognition":
        return _subject_compatible(subject, getattr(row, "subject", ""))
    return True


@tool(memory_note_args)
def memory_note(content, kind="experience", subject=None, confidence=0.55,
                importance=3, cues=None, source_event_ids=None, reason=None,
                *, session_id=None, origin_project=None, query_vector=None):
    '''给自己写一条经验/认知/洞见：写前先查重，同义的不再新增一条。'''
    text = str(content or "").strip()
    if not text:
        raise ToolError(-32602, "content 不能为空")
    if len(text) > 300:
        raise ToolError(-32602, "content 超过 300 字")
    if kind not in ("experience", "cognition", "insight"):
        raise ToolError(-32602, f"未知 kind: {kind!r}")
    subject = str(subject or "").strip()
    if kind == "cognition":
        if not subject:
            raise ToolError(-32602, "cognition 必须给 subject：写清这是谁的事")
        if len(subject) > 40:
            raise ToolError(-32602, "subject 超过 40 字")

    db = _db()
    # 查重：优先向量（中文同义改写字面对不上），没有向量再退回字面。
    # 只在**同 kind、范围兼容**的候选里找；认知还要同主体（0.5.2 修：
    # 旧实现跨三张表、跨项目取全局最高分，会把不同事误判成 duplicate）。
    best = None                      # (kind, id, content, similarity)
    candidates = []
    if query_vector:
        for ref_id, distance in db.vec_search(kind, query_vector, k=8,
                                              scope="all").items():
            row = _l3_record(db, kind, int(ref_id))
            if row is not None and _note_candidate_ok(
                    kind, row, subject, origin_project):
                candidates.append((1.0 - float(distance), row))
    else:
        for hit in retrieval.search(db, text, limit=8, scope="all", touch=False,
                                    kinds=(kind,)):
            row = _l3_record(db, kind, hit.id)
            if row is None or not _note_candidate_ok(
                    kind, row, subject, origin_project):
                continue
            ratio = difflib.SequenceMatcher(None, text, row.content or "").ratio()
            candidates.append((ratio, row))
    for similarity, row in candidates:
        if best is None or similarity > best[3]:
            best = (kind, row.id, row.content or "", similarity)

    if best is not None and best[3] >= _NOTE_MERGE_SIM:
        base.log.info("note.duplicate", kind=best[0], id=best[1],
                      similarity=round(best[3], 3))
        return {"action": "duplicate", "kind": best[0], "id": best[1],
                "similarity": round(best[3], 3), "content": best[2],
                "hint": "已有同义条目，未新增。要改正文请到设置页处理，或用 memory_feedback 标掉旧的"}
    if best is not None and best[3] >= _NOTE_HINT_SIM:
        return {"action": "candidate", "kind": best[0], "id": best[1],
                "similarity": round(best[3], 3), "content": best[2],
                "hint": "疑似重复，未新增。确认是两件事再改写后重试"}
    if best is not None and not query_vector and best[3] >= _NOTE_LITERAL_HINT:
        return {"action": "candidate", "kind": best[0], "id": best[1],
                "similarity": round(best[3], 3), "content": best[2],
                "hint": "字面相近（未算向量），未新增。确认不同再改写后重试"}

    # 三张表的 source_event_id 是 NOT NULL：手工写入没有来源事件时记 0
    event_id = int(source_event_ids[0]) if source_event_ids else 0
    cue_tuple = tuple(str(c).strip() for c in (cues or []) if str(c).strip())
    now = base.get_cur_time()
    if kind == "experience":
        new_id = db.insert_experience(database.Experience(
            context=None, content=text, kind="technique",
            confidence=float(confidence), importance=int(importance),
            origin_session=session_id, origin_project=origin_project,
            scope="project", source_event_id=event_id,
            cues=cue_tuple, created_at=now))
    elif kind == "cognition":
        new_id = db.insert_cognition(database.Cognition(
            subject=subject, content=text, kind="fact",
            confidence=float(confidence), importance=int(importance),
            origin_session=session_id, origin_project=origin_project,
            source_event_id=event_id, cues=cue_tuple, created_at=now))
    else:
        new_id = db.insert_insight(database.Insight(
            content=text, scope="global", confidence=float(confidence),
            importance=int(importance), origin_session=session_id,
            origin_project=origin_project, cues=cue_tuple, created_at=now))
    # 插入不会自动进全文索引与向量表：不补这两步，新条目搜不到
    db.index_fts(kind, new_id,
                 title=(subject if kind == "cognition" else ""),
                 body=text, origin_project=origin_project,
                 session_id=session_id)
    if query_vector:
        db.vec_upsert(kind, new_id, query_vector,
                      origin_project=origin_project)
    base.log.info("note.insert", kind=kind, id=new_id)
    return {"action": "created", "kind": kind, "id": new_id,
            "content": text, "reason": str(reason or "").strip()[:120]}


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
        "insights": db.count_l3("insight"),
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
    self_written = False
    for call in calls:
        call_id = call.get("call_id") or ""
        name = call.get("name") or ""
        args = call.get("arguments") or {}
        context["query_vector"] = call.get("query_vector")
        if name == "memory_recent":
            # memory_recent 的 None 是有语义的：跨会话回放。
            # 通用 _merge_context 会把 None 替换成信封 session_id，
            # 因此该工具在这里消费信封值，不让它二次注入。
            context["session_id"] = args.get("session_id")
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
        if name == "memory_self_write":
            self_written = True

    frame.push(q, data, "tools.result", results,
               detail="消息队列已满，tools.result 未能下发")
    if self_written:
        # 自述变化 → 通知外壳刷新 core.get（下一轮动态上下文生效）
        frame.push(q, data, "notify", {"kind": "core"})

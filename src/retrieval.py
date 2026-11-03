# -*- coding: utf-8 -*-
"""记忆检索：主流 IR（FTS + RRF + 精排）叠加人类记忆的激活模型。

三段流水线：
1. 召回：FTS5（trigram）取 top-N；<3 字符查询退回 LIKE；可选联想扩展；
2. 融合：单路时用名次折减，双路以上用 RRF(k=60)；
3. 精排：final = α·rel + (1-α)·Strength，Strength 里含 ACT-R 激活、
   重要性、确信度、情境匹配与联想来源。

设计见 DEEPSEEK 开发经验与 docs/database.md 的 l3 章节。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import base
import config
import database

RRF_K = 60
DEFAULT_ALPHA = 0.6
DEFAULT_EXPLORE_RATIO = 0.1
DEFAULT_MAX_PER_CLUSTER = 2
RECALL_LIMIT = 50
MAX_ASSOCIATIONS = 20      # 联想扩展的总量上限（防止 50 次命中各触发一次查询）


@dataclass(frozen=True)
class Hit:
    kind: str                 # event / experience / cognition
    id: int
    title: str
    text: str
    score: float
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "title": self.title,
                "text": self.text, "score": round(self.score, 4), "meta": self.meta}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def activation(use_count: int, last_used_at: str | None, created_at: str | None,
               now: datetime | None = None) -> float:
    '''ACT-R 基础激活近似：ln(1+次数) − 0.5·ln(1+距今天数)。'''
    now = now or _now()
    anchor = _parse(last_used_at) or _parse(created_at) or now
    days = max(0.0, (now - anchor).total_seconds() / 86400.0)
    return math.log1p(max(0, int(use_count))) - 0.5 * math.log1p(days)


def _minmax(values: list) -> list:
    if not values:
        return []
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return [0.5 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _cluster_of(kind: str, row) -> tuple:
    if kind == "event":
        return ("event", row.id)
    return ("l3", getattr(row, "source_event_id", None) or row.id)


def _project_match(origin_project: str | None, current: str | None) -> float:
    if origin_project is None:
        return 0.5
    return 1.0 if origin_project == current else 0.0


def _eligible(row, origin_project: str | None, scope: str, now: datetime) -> bool:
    if getattr(row, "status", "active") != "active":
        return False
    valid_to = _parse(getattr(row, "valid_to", None))
    if valid_to is not None and valid_to <= now:
        return False
    if scope == "all":
        return True
    origin = getattr(row, "origin_project", None)
    return origin is None or origin == origin_project


def _fetch(db: database.Database, kind: str, row_id: int):
    if kind == "event":
        return db.get_event(row_id)
    if kind == "experience":
        return db.get_experience(row_id)
    if kind == "cognition":
        return db.get_cognition(row_id)
    return None


def _title(row, kind: str) -> str:
    if kind == "event":
        return row.title or ""
    return getattr(row, "subject", "") or getattr(row, "context", "") or ""


def _text(row, kind: str) -> str:
    if kind == "event":
        return row.summary or ""
    return row.content or ""


def _use_count(record, kind: str) -> int:
    '''
    使用次数的字段名随类型不同：事件表是 `cnt`，l3 两表是 `use_count`。
    漏了这层映射，事件的 ACT-R 激活会恒为 0——热度排序与探索位全部失效。
    '''
    field = "cnt" if kind == "event" else "use_count"
    return int(getattr(record, field, 0) or 0)


def _last_used(record, kind: str) -> str | None:
    '''最近一次使用时间；事件与 l3 现在都有该列（老库事件为 NULL 时退化到 created_at）。'''
    return getattr(record, "last_used_at", None)


def _meta(row, kind: str, extra: dict | None = None) -> dict:
    meta = {
        "importance": getattr(row, "importance", None),
        "confidence": getattr(row, "confidence", None),
        "origin_project": getattr(row, "origin_project", None),
        "use_count": _use_count(row, kind),
    }
    if kind == "event":
        meta["session_id"] = row.session_id
        meta.pop("importance", None)
        meta.pop("confidence", None)
    if extra:
        meta.update(extra)
    return meta


def _weights() -> dict:
    search = config.get_config().get("search", {})
    return {
        "alpha": float(search.get("alpha", DEFAULT_ALPHA)),
        "w_activation": float(search.get("w_activation", 0.45)),
        "w_importance": float(search.get("w_importance", 0.25)),
        "w_confidence": float(search.get("w_confidence", 0.15)),
        "w_project": float(search.get("w_project", 0.10)),
        "explore_ratio": float(search.get("explore_ratio", DEFAULT_EXPLORE_RATIO)),
        "max_per_cluster": int(search.get("max_per_cluster", DEFAULT_MAX_PER_CLUSTER)),
        "rrf_k": int(search.get("rrf_k", RRF_K)),
        "cross_project_penalty": float(search.get("cross_project_penalty", 0.0)),
    }


def _candidates(db: database.Database, query: str, origin_project: str | None,
                scope: str, kinds: Iterable[str], now: datetime) -> dict:
    '''
    召回：{ (kind, id): {"row":..., "rel":..., "assoc":...} }

    取数走批量 `get_many`：旧实现每条命中各查一次库，一次检索实测上百条 SQL。
    '''
    found = {}
    hits = [item for item in db.search_fts(
                query, limit=RECALL_LIMIT, origin_project=origin_project,
                restrict_project=(scope == "project"))
            if item["ref_kind"] in kinds]
    grouped: dict[str, list] = {}
    for rank, item in enumerate(hits):
        grouped.setdefault(item["ref_kind"], []).append((rank, int(item["ref_id"])))

    associated = 0
    for kind, entries in grouped.items():
        rows = db.get_many(kind, [row_id for _, row_id in entries])
        for rank, row_id in entries:
            row = rows.get(row_id)
            if row is None or not _eligible(row, origin_project, scope, now):
                continue
            found[(kind, row_id)] = {"row": row, "rel": 1.0 / (1.0 + rank),
                                     "assoc": 1.0}
            # 联想扩展：同 subject / 同 cues 的 l3 以 0.5 分进候选。
            # 有总量上限：否则 50 条命中各查一次，延迟随命中数线性增长。
            if kind == "event" or associated >= MAX_ASSOCIATIONS:
                continue
            for other in db.find_l3_candidates(
                    kind, subject=getattr(row, "subject", None),
                    query=_first_cue(row), origin_project=origin_project, limit=3):
                associated += 1
                key = (kind, other.id)
                if key in found or not _eligible(other, origin_project, scope, now):
                    continue
                found[key] = {"row": other, "rel": 0.5, "assoc": 0.5}
                if associated >= MAX_ASSOCIATIONS:
                    break
    return found


def _first_cue(row) -> str | None:
    cues = getattr(row, "cues", ()) or ()
    return str(cues[0])[:20] if cues else None


def _score(found: dict, weights: dict, origin_project: str | None) -> list:
    '''
    精排：final = α·rel + (1−α)·strength。

    归一化按 **kind 分组**做：事件用 cnt、l3 用 use_count，两类量纲不同，
    混在一张量尺上归一会让一类的变化压缩另一类的区间。
    '''
    rows = list(found.items())
    act_norm: dict = {}
    rel_norm: dict = {}
    by_kind: dict = {}
    for key, entry in rows:
        by_kind.setdefault(key[0], []).append((key, entry))

    for kind, entries in by_kind.items():
        acts = [activation(_use_count(entry["row"], kind),
                           _last_used(entry["row"], kind),
                           getattr(entry["row"], "created_at", None))
                for _, entry in entries]
        for (key, _), value in zip(entries, _minmax(acts)):
            act_norm[key] = value
        for (key, _), value in zip(entries, _minmax([entry["rel"] for _, entry in entries])):
            rel_norm[key] = value

    scored = []
    for key, entry in rows:
        kind, row_id = key
        record = entry["row"]
        # 事件没有 importance/confidence：固定中性 0.5，避免用 l3 的默认值白送分
        if kind == "event":
            importance = 0.5
            confidence = 0.5
        else:
            importance = (getattr(record, "importance", 3) - 1) / 4
            confidence = float(getattr(record, "confidence", 0.8) or 0)
        match = _project_match(getattr(record, "origin_project", None),
                               origin_project)
        if match == 0.0 and origin_project is not None:
            # 跨项目：按配置给负向衰减（默认 0，A/B 有证据再打开）
            match = -weights["cross_project_penalty"]
        strength = (weights["w_activation"] * act_norm[key]
                    + weights["w_importance"] * importance
                    + weights["w_confidence"] * confidence
                    + weights["w_project"] * match
                    + 0.05 * entry["assoc"])
        total = weights["alpha"] * rel_norm[key] + (1 - weights["alpha"]) * strength
        scored.append((total, kind, row_id, record, entry["assoc"]))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return scored


def _select(scored: list, limit: int, weights: dict) -> list:
    max_per_cluster = max(1, weights["max_per_cluster"])
    explore = int(round(limit * max(0.0, min(1.0, weights["explore_ratio"]))))
    base_limit = max(1, limit - explore) if explore else limit

    selected, counts, keys = [], {}, set()

    def add(item) -> bool:
        key = (item[1], item[2])
        if key in keys or len(selected) >= limit:
            return False
        cluster = _cluster_of(item[1], item[3])
        if counts.get(cluster, 0) >= max_per_cluster:
            return False
        selected.append(item)
        counts[cluster] = counts.get(cluster, 0) + 1
        keys.add(key)
        return True

    for item in scored:
        if len(selected) >= base_limit:
            break
        add(item)

    # 探索位：给从未被使用过的条目曝光机会，且同样受簇上限约束
    if explore:
        for item in scored:
            if len(selected) >= limit:
                break
            if (item[1], item[2]) in keys:
                continue
            if _use_count(item[3], item[1]) != 0:
                continue
            add(item)
    return selected


def search(db: database.Database, query: str, *, origin_project: str | None = None,
           scope: str | None = None, limit: int = 10,
           kinds: Iterable[str] = ("event", "experience", "cognition"),
           touch: bool = True) -> list:
    '''
    记忆检索：返回按综合分排序的 Hit 列表。

    :param scope: project / all；None 表示取 config.search.default_scope
                  （范围是数据的固有属性，检索侧只决定本次要不要扩大）
    :raise ValueError: scope 取值非法（由外层转 -32602）
    '''
    query = (query or "").strip()
    if not query or limit <= 0:
        return []
    if scope is None:
        scope = config.get_config().get("search", {}).get("default_scope", "all")
    if scope not in ("project", "all"):
        raise ValueError(f"scope 只能是 project / all，收到 {scope!r}")
    weights = _weights()
    now = _now()
    found = _candidates(db, query, origin_project, scope, set(kinds), now)
    if not found:
        return []
    scored = _score(found, weights, origin_project)
    selected = _select(scored, limit, weights)

    hits = []
    for total, kind, row_id, record, assoc in selected:
        hits.append(Hit(kind=kind, id=row_id, title=_title(record, kind),
                        text=_text(record, kind), score=total,
                        meta=_meta(record, kind, {"assoc": assoc})))
    if touch:
        _touch(db, hits)
    return hits


def _touch(db: database.Database, hits: list) -> None:
    '''只对真正返回（将被注入）的条目计数，体现「提取练习」。'''
    events = [hit.id for hit in hits if hit.kind == "event"]
    experiences = [hit.id for hit in hits if hit.kind == "experience"]
    cognitions = [hit.id for hit in hits if hit.kind == "cognition"]
    if events:
        db.bump_events(events)
    if experiences:
        db.touch_experiences(experiences)
    if cognitions:
        db.touch_cognitions(cognitions)


def recent(db: database.Database, session_id: str, *, from_at: str, to_at: str,
           limit: int = 20, origin_project: str | None = None,
           touch: bool = False) -> list:
    '''
    按时间区间取事件摘要与 l3，供 memory_recent 使用。

    语义（定稿）：
    - event：发生区间与 [from_at, to_at] 相交即返回，按结束时间倒序；
    - experience / cognition：按**入库时间** created_at 落在区间内返回——
      这是「那段时间沉淀了什么」的口径，不是「事情发生在何时」；
    - 两者**独立配额**：事件占 limit − l3 配额，l3 最多 3 条。
      旧实现混排后按固定分排序再截断，事件恒 1.0 > l3 恒 0.5，事件一多 l3 就全灭。

    :raise ValueError: 时间无法解析或 from > to（由 tools 层转 -32602）
    '''
    start, end = _parse(from_at), _parse(to_at)
    if start is None or end is None:
        raise ValueError("from_at / to_at 必须是 ISO 8601 时间")
    if start > end:
        raise ValueError("from_at 不得晚于 to_at")

    limit = max(1, int(limit))
    l3_quota = min(3, limit)
    event_quota = max(1, limit - l3_quota)

    hits = []
    for span in db.list_events_overlapping(session_id, from_at, to_at,
                                           limit=event_quota):
        hits.append(Hit(kind="event", id=span.event.id, title=span.event.title,
                        text=span.event.summary or "", score=1.0,
                        meta={"span": {"from": span.first_at, "to": span.last_at},
                              "session_id": session_id,
                              "use_count": int(span.event.cnt or 0)}))

    for kind, rows in (
            ("experience", db.list_experiences(origin_project=origin_project, limit=l3_quota,
                                               created_from=from_at, created_to=to_at)),
            ("cognition", db.list_cognitions(origin_project=origin_project, limit=l3_quota,
                                             created_from=from_at, created_to=to_at))):
        for row in rows:
            hits.append(Hit(kind=kind, id=row.id, title=_title(row, kind),
                            text=_text(row, kind), score=0.5,
                            meta=_meta(row, kind, {"created_at": row.created_at})))

    if touch:
        _touch(db, hits)
    return hits[:limit]


def get(db: database.Database, kind: str, item_id: int, *, touch: bool = True) -> dict:
    '''再认：按 id 直取。event 返回事件 + 正文消息，l3 返回整行。'''
    if kind == "event":
        event = db.get_event(item_id)
        if event is None:
            return {}
        messages = [{"msg_id": row.message_id, "seq": row.seq, "role": row.role,
                     "content": row.content, "occur_at": row.occur_at}
                    for row in db.list_event_messages(item_id)]
        if touch:
            db.bump_events([item_id])
        return {"kind": "event", "event": event.to_dict(), "messages": messages}
    if kind in ("experience", "cognition"):
        row = _fetch(db, kind, item_id)
        if row is None:
            return {}
        if touch:
            (db.touch_experiences if kind == "experience" else db.touch_cognitions)([item_id])
        return {"kind": kind, "item": {
            "id": row.id, "content": row.content, "kind": row.kind,
            "confidence": row.confidence, "importance": row.importance,
            "origin_session": row.origin_session,
            "origin_project": row.origin_project,
            "source_event_id": row.source_event_id,
            "status": row.status, "valid_to": row.valid_to,
            "use_count": row.use_count, "last_used_at": row.last_used_at,
            "created_at": row.created_at,
        }}
    return {}

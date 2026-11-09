# -*- coding: utf-8 -*-
"""记忆检索：FTS 召回 + 名次折减 + 精排，叠加人类记忆的激活模型。

三段流水线：
1. 召回：FTS5（trigram）取 top-N；<3 字符查询退回 LIKE；可选联想扩展；
2. 融合：单路按名次折减 rel = 1/(1+rank)（v2 加向量通道后再引入 RRF(k=60)）；
3. 精排：final = α·rel + (1-α)·Strength，Strength 里含 ACT-R 激活、
   重要性、确信度、情境匹配与联想来源。

设计见 DEEPSEEK 开发经验与 docs/database.md 的 l3 章节。
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import base
import config
import database

DEFAULT_ALPHA = 0.6
DEFAULT_EXPLORE_RATIO = 0.1
DEFAULT_MAX_PER_CLUSTER = 2
DEFAULT_FEEDBACK_WEIGHT = 0.25   # 反馈分在 strength 里的权重（有界，±3 满分）
DEFAULT_DEDUP_SIM = 0.85   # l3 之间余弦 ≥ 此值：判为同义，只留分高的
                           # 实测（2026-11-09，148 条 cognition）：
                           #   同义组两两 0.915–0.983；随机对 p99 仅 0.77
                           # 取 0.85 既覆盖同义组，又几乎不误杀
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
    '''
    组内归一到 [0, 1]。

    单个元素直接给 1.0：它既是最大值也是最小值，给 0.5 会让"只命中一条事件"
    永远输给"命中两条经验"里的第一名（跨 kind 融合被系统性拉偏）。
    多个元素但全等时没有相对信息，给 0.5 保持中性。
    '''
    if not values:
        return []
    if len(values) == 1:
        return [1.0]
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
    if kind == "insight":
        return db.get_insight(row_id)
    return None


def _title(row, kind: str) -> str:
    if kind == "event":
        return row.title or ""
    if kind == "insight":
        return "归纳洞见"
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
        "created_at": getattr(row, "created_at", None),
    }
    if kind == "event":
        meta["session_id"] = row.session_id
        meta.pop("importance", None)
        meta.pop("confidence", None)
    elif kind in ("experience", "insight"):
        meta["scope"] = getattr(row, "scope", "project")
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
        "w_feedback": float(search.get("w_feedback", DEFAULT_FEEDBACK_WEIGHT)),
        "explore_ratio": float(search.get("explore_ratio", DEFAULT_EXPLORE_RATIO)),
        "max_per_cluster": int(search.get("max_per_cluster", DEFAULT_MAX_PER_CLUSTER)),
        "cross_project_penalty": float(search.get("cross_project_penalty", 0.0)),
        "dedup_similarity": float(search.get("dedup_similarity", DEFAULT_DEDUP_SIM)),
    }


def _candidates(db: database.Database, query: str, origin_project: str | None,
                scope: str, kinds: Iterable[str], now: datetime,
                query_vector: list | None = None) -> dict:
    '''
    多路召回 + RRF 融合：{ (kind, id): {"row":..., "rel":..., "assoc":...} }

    路：FTS5 BM25 + sqlite-vec KNN（若有 query_vector）+ 结构联想。
    RRF(k=60) 融合，rel 只是相对名次；最终排序仍由 _score 的精排决定。
    '''
    wanted = set(kinds)
    fts_rank: dict = {}
    excerpts: dict = {}
    hits = [item for item in db.search_fts(
                query, limit=RECALL_LIMIT, origin_project=origin_project,
                restrict_project=(scope == "project"))
            if item["ref_kind"] in wanted]
    for rank, item in enumerate(hits):
        fts_rank.setdefault((item["ref_kind"], int(item["ref_id"])), rank)
        if item.get("excerpt"):
            excerpts[(item["ref_kind"], int(item["ref_id"]))] = item["excerpt"]

    vec_rank: dict = {}
    if query_vector and getattr(db, "_vec_enabled", False):
        for kind in ("experience", "cognition", "insight"):
            if kind not in wanted:
                continue
            distances = db.search_vectors(kind, query_vector, k=RECALL_LIMIT,
                                          origin_project=origin_project,
                                          scope=scope)
            for rank, (ref_id, _distance) in enumerate(
                    sorted(distances.items(), key=lambda item: item[1])):
                vec_rank[(kind, ref_id)] = rank

    keys = set(fts_rank) | set(vec_rank)
    grouped: dict[str, list] = {}
    for kind, row_id in keys:
        grouped.setdefault(kind, []).append(row_id)

    found = {}
    for kind, ids in grouped.items():
        rows = db.get_many(kind, ids)
        for row_id in ids:
            row = rows.get(row_id)
            if row is None or not _eligible(row, origin_project, scope, now):
                continue
            score = 0.0
            if (kind, row_id) in fts_rank:
                score += 1.0 / (60 + fts_rank[(kind, row_id)])
            if (kind, row_id) in vec_rank:
                score += 1.0 / (60 + vec_rank[(kind, row_id)])
            found[(kind, row_id)] = {"row": row, "rel": score or 0.001,
                                     "assoc": 1.0,
                                     "excerpt": excerpts.get((kind, row_id))}

    associated = 0
    for (kind, _row_id), entry in list(found.items()):
        row = entry["row"]
        if kind in ("event", "insight") or associated >= MAX_ASSOCIATIONS:
            continue
        for other in db.find_l3_candidates(
                kind, subject=getattr(row, "subject", None),
                query=_first_cue(row), origin_project=origin_project, limit=3):
            associated += 1
            key = (kind, other.id)
            if key in found or not _eligible(other, origin_project, scope, now):
                continue
            found[key] = {"row": other, "rel": 0.01, "assoc": 0.5}
            if associated >= MAX_ASSOCIATIONS:
                break
    return found


def _first_cue(row) -> str | None:
    cues = getattr(row, "cues", ()) or ()
    return str(cues[0])[:20] if cues else None


REVIEW_PENALTY = 0.5          # 待复核 / 低可信条目的强度折减
LOW_CONFIDENCE = 0.5


def _reliability(record, kind: str) -> float:
    '''可信度折减：人工上报待复核的事件、confidence 偏低的 l3 都往后排。'''
    if kind == "event":
        return REVIEW_PENALTY if getattr(record, "needs_review", 0) else 1.0
    try:
        confidence = float(getattr(record, "confidence", 1.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 1.0
    return REVIEW_PENALTY if confidence < LOW_CONFIDENCE else 1.0


FEEDBACK_CAP = 3          # ±3 次反馈到达饱和，继续累计不再放大（防失控）


def _feedback(record) -> float:
    '''
    反馈分 → [−1, 1]。

    只截断不归一化到方差：useful/useless 是人工信号，一次就该有可感知的
    位移；±3 次到顶，避免长期使用后少数条目被反馈分彻底支配。
    '''
    try:
        raw = int(getattr(record, "feedback_score", 0) or 0)
    except (TypeError, ValueError):
        raw = 0
    capped = max(-FEEDBACK_CAP, min(FEEDBACK_CAP, raw))
    return capped / FEEDBACK_CAP


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
                    + weights["w_feedback"] * _feedback(record)
                    + 0.05 * entry["assoc"])
        strength *= _reliability(record, kind)      # 待复核 / 低可信条目降权
        strength = max(0.0, min(1.0, strength))     # 反馈项可能把总分顶出 1
        total = weights["alpha"] * rel_norm[key] + (1 - weights["alpha"]) * strength
        scored.append((total, kind, row_id, record, entry["assoc"],
                       entry.get("excerpt")))
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

    # 回填：簇上限只用来“别让同源条目刷屏”，不该让结果欠填。
    # 前面两轮被上限挡掉的候选，在还有空位时按分数补回来。
    # 重复由 _dedup_semantic 在选完之后统一收掉，这里不做二次限制。
    if len(selected) < min(limit, len(scored)):
        for item in scored:
            if len(selected) >= limit:
                break
            key = (item[1], item[2])
            if key in keys:
                continue
            selected.append(item)
            keys.add(key)
    return selected


def _blob_to_vector(blob) -> list | None:
    '''vec0 存的 float32 blob → 向量；长度不是 4 的倍数时返回 None。'''
    if not blob or len(blob) % 4:
        return None
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _cosine(a, b) -> float:
    '''余弦相似度；任一侧接近零向量时返回 0。'''
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return dot / (na * nb)


def _dedup_semantic(db, selected: list, weights: dict) -> tuple:
    '''
    读取侧语义去重：只对 l3（经验 / 认知 / 洞见）。

    同一件事常被不同事件抽成多条，字面完全不同、向量却几乎一样；
    这里按分数从高到低保留第一条，其余丢弃，并把数量记进 `merged`。
    事件不参与：两次相似的对话是两件事，合并会丢细节。
    '''
    threshold = float(weights.get("dedup_similarity", DEFAULT_DEDUP_SIM))
    if threshold <= 0:
        return selected, {}
    if sum(1 for item in selected if item[1] != "event") < 2:
        return selected, {}

    vectors = {}
    # 只读本次选中的几条：全表读向量是 O(全部条数×维度)，每次检索都付一遍
    for kind in ("experience", "cognition", "insight"):
        wanted = [item[2] for item in selected if item[1] == kind]
        if not wanted:
            continue
        for ref_id, blob in db.get_vec_blobs(kind, wanted).items():
            vector = _blob_to_vector(blob)
            if vector is not None:
                vectors[(kind, int(ref_id))] = vector

    kept, merged = [], {}
    for item in selected:
        key = (item[1], item[2])
        vector = vectors.get(key)
        if vector is not None:
            duplicate_of = None
            for anchor in kept:
                other = vectors.get((anchor[1], anchor[2]))
                if other is None:
                    continue
                if _cosine(vector, other) >= threshold:
                    duplicate_of = (anchor[1], anchor[2])
                    break
            if duplicate_of is not None:
                merged[duplicate_of] = merged.get(duplicate_of, 0) + 1
                continue
        kept.append(item)
    return kept, merged


def _backfill_after_dedup(db, scored: list, selected: list, limit: int,
                          weights: dict) -> list:
    '''
    去重会删条，但空出来的名额应由**不同内容**的候选补上。

    旧实现先选满再删重复，limit=2 只剩 1 条时不会再看后面的候选；
    这里按原分数顺序补位，补入的条目同样要和已保留条目做一次语义比较。
    '''
    if len(selected) >= limit:
        return selected
    threshold = float(weights.get("dedup_similarity", DEFAULT_DEDUP_SIM))
    kept_keys = {(item[1], item[2]) for item in selected}
    remaining = [item for item in scored
                 if (item[1], item[2]) not in kept_keys]
    if not remaining:
        return selected

    vectors = {}
    if threshold > 0:
        for kind in ("experience", "cognition", "insight"):
            wanted = {item[2] for item in selected + remaining
                      if item[1] == kind}
            if not wanted:
                continue
            for ref_id, blob in db.get_vec_blobs(kind, wanted).items():
                vector = _blob_to_vector(blob)
                if vector is not None:
                    vectors[(kind, int(ref_id))] = vector

    for item in remaining:
        if len(selected) >= limit:
            break
        key = (item[1], item[2])
        vector = vectors.get(key)
        if (threshold > 0 and item[1] != "event" and vector is not None
                and any(_cosine(vector, vectors[(anchor[1], anchor[2])]) >= threshold
                        for anchor in selected
                        if anchor[1] != "event"
                        and (anchor[1], anchor[2]) in vectors)):
            continue
        selected.append(item)
    return selected


def search(db: database.Database, query: str, *, origin_project: str | None = None,
           scope: str | None = None, limit: int = 10,
           kinds: Iterable[str] = ("event", "experience", "cognition", "insight"),
           touch: bool = True, query_vector: list | None = None) -> list:
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
    found = _candidates(db, query, origin_project, scope, set(kinds), now,
                        query_vector=query_vector)
    if not found:
        return []
    scored = _score(found, weights, origin_project)
    selected = _select(scored, limit, weights)
    selected, merged_map = _dedup_semantic(db, selected, weights)
    selected = _backfill_after_dedup(db, scored, selected, limit, weights)

    hits = []
    for total, kind, row_id, record, assoc, excerpt in selected:
        meta = _meta(record, kind, {"assoc": assoc})
        merged_count = merged_map.get((kind, row_id))
        if merged_count:
            meta["merged"] = merged_count
        if excerpt:
            # FTS 命中片段：确认「有没有聊到 X」通常不必再读全文。
            # 仅向量/联想召回的条目不伪造摘录。
            meta["excerpt"] = excerpt
        hits.append(Hit(kind=kind, id=row_id, title=_title(record, kind),
                        text=_text(record, kind), score=total, meta=meta))
    if touch:
        _touch(db, hits)
    return hits


def _touch(db: database.Database, hits: list) -> None:
    '''只对真正返回（将被注入）的条目计数，体现「提取练习」。'''
    events = [hit.id for hit in hits if hit.kind == "event"]
    experiences = [hit.id for hit in hits if hit.kind == "experience"]
    cognitions = [hit.id for hit in hits if hit.kind == "cognition"]
    insights = [hit.id for hit in hits if hit.kind == "insight"]
    if events:
        db.bump_events(events)
    if experiences:
        db.touch_experiences(experiences)
    if cognitions:
        db.touch_cognitions(cognitions)
    if insights:
        db.touch_insights(insights)


def recent(db: database.Database, session_id: str | None, *, from_at: str,
           to_at: str, limit: int = 20, origin_project: str | None = None,
           touch: bool = False) -> list:
    '''
    按时间区间取事件摘要与 l3，供 memory_recent 使用。

    语义（定稿）：
    - event：发生区间与 [from_at, to_at] 相交即返回，按结束时间倒序；
    - experience / cognition：按**入库时间** created_at 落在区间内返回——
      这是「那段时间沉淀了什么」的口径，不是「事情发生在何时」；
    - 两者**独立配额**：事件占 limit − l3 配额，l3 最多 3 条。
      旧实现混排后按固定分排序再截断，事件恒 1.0 > l3 恒 0.5，事件一多 l3 就全灭。

    :param session_id: None 表示**跨会话**回放（「我之前让你做什么」这种
                       问题不该被会话边界挡住）；传值则只看该会话。
    :raise ValueError: 时间无法解析或 from > to（由 tools 层转 -32602）
    '''
    start, end = _parse(from_at), _parse(to_at)
    if start is None or end is None:
        raise ValueError("from_at / to_at 必须是 ISO 8601 时间")
    if start > end:
        raise ValueError("from_at 不得晚于 to_at")

    # 库里所有时间列都是 UTC 字符串；窗口也必须归一成 UTC 再比较，
    # 否则模型传 +08:00 时，字符串比较会整体偏 8 小时（漏掉/多收一大段）。
    start_iso, end_iso = base.to_utc_iso(from_at), base.to_utc_iso(to_at)

    limit = max(1, int(limit))
    l3_quota = min(3, limit)
    event_quota = max(1, limit - l3_quota)

    hits = []
    for span in db.list_events_overlapping(session_id, start_iso, end_iso,
                                           limit=event_quota):
        hits.append(Hit(kind="event", id=span.event.id, title=span.event.title,
                        text=span.event.summary or "", score=1.0,
                        meta={"span": {"from": span.first_at, "to": span.last_at},
                              # 跨会话回放时用事件自身的归属会话，不是传入的过滤值
                              "session_id": span.event.session_id,
                              "use_count": int(span.event.cnt or 0)}))

    for kind, rows in (
            ("experience", db.list_experiences(origin_project=origin_project, limit=l3_quota,
                                               created_from=start_iso, created_to=end_iso)),
            ("cognition", db.list_cognitions(origin_project=origin_project, limit=l3_quota,
                                             created_from=start_iso, created_to=end_iso))):
        for row in rows:
            hits.append(Hit(kind=kind, id=row.id, title=_title(row, kind),
                            text=_text(row, kind), score=0.5,
                            meta=_meta(row, kind, {"created_at": row.created_at})))

    if touch:
        _touch(db, hits)
    return hits[:limit]


def get(db: database.Database, kind: str, item_id: int, *, touch: bool = True,
        max_chars: int = 8000, msg_offset: int = 0) -> dict:
    '''
    再认：按 id 直取。event 返回事件 + 正文消息，l3 返回整行。

    事件正文默认最多 8000 字符，按消息顺序整页返回；用 next_msg_offset
    续读，不丢、不重。max_chars=0 保留旧的「全部正文」行为。
    '''
    if kind == "event":
        event = db.get_event(item_id)
        if event is None:
            return {}
        rows = db.list_event_messages(item_id)
        total_chars = sum(len(row.content or "") for row in rows)
        offset = max(0, min(int(msg_offset), len(rows)))
        budget = max(0, int(max_chars))
        messages = []
        used_chars = 0
        next_offset = offset
        for index in range(offset, len(rows)):
            row = rows[index]
            content = row.content or ""
            # 即使首条超长也至少返回一条；下一条从 next_offset 继续。
            # 字符预算按完整消息计算，避免同一消息在分页里重复/漏字。
            if messages and budget and used_chars + len(content) > budget:
                break
            messages.append({"msg_id": row.message_id, "seq": row.seq,
                             "role": row.role, "content": content,
                             "occur_at": row.occur_at})
            used_chars += len(content)
            next_offset = index + 1
            if budget and used_chars >= budget:
                break
        if touch:
            db.bump_events([item_id])
        truncated = next_offset < len(rows)
        return {"kind": "event", "event": event.to_dict(),
                "messages": messages, "msg_offset": offset,
                "next_msg_offset": next_offset if truncated else None,
                "truncated": truncated, "total_chars": total_chars,
                "returned_chars": used_chars, "max_chars": budget}
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
            "scope": getattr(row, "scope", None) if kind == "experience" else None,
            "source_event_id": row.source_event_id,
            "status": row.status, "valid_to": row.valid_to,
            "use_count": row.use_count, "last_used_at": row.last_used_at,
            "created_at": row.created_at,
        }}
    if kind == "insight":
        row = db.get_insight(item_id)
        if row is None:
            return {}
        if touch:
            db.touch_insights([item_id])
        return {"kind": "insight", "item": {
            "id": row.id, "content": row.content, "scope": row.scope,
            "confidence": row.confidence, "importance": row.importance,
            "origin_session": row.origin_session,
            "origin_project": row.origin_project,
            "source_experience_ids": list(row.source_experience_ids),
            "cues": list(row.cues), "status": row.status,
            "use_count": row.use_count, "last_used_at": row.last_used_at,
            "created_at": row.created_at,
        }}
    return {}

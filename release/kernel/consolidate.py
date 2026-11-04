# -*- coding: utf-8 -*-
"""调用 B：把「新条目 × 候选旧条目」交给模型裁决 new / supersede / invalidate。

流程：
1. `gather_candidates`：按主体（认知）或线索（经验）召回 top-k 旧条目；
2. `build_messages`：把「新条目 + 各自候选」拼成一次批量请求；
3. `parse_decisions`：解析并校验动作（target 必须在候选里）；
4. 落库由 `ingest.apply_summary(decisions=...)` 执行，本模块不写库。

失败策略：调用失败或解析失败时全部按 `new` 处理并标记 `needs_review`，
保证摘要链路不被裁决阻塞（宁可多一条重复，也不丢记忆）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import database

MAX_CANDIDATES = 5
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_ACTIONS = ("new", "supersede", "invalidate")

CONSOLIDATE_SYSTEM_PROMPT = """【角色】你是长期记忆的合并裁决器：判断每条新条目是否与已有记忆重复或冲突，决定它如何入库。

【输入】user 消息是一个 JSON 数组，每项含：
index（序号）、table（experience/cognition）、new（新条目）、candidates（候选旧条目，每条有 id/content）。

【动作】
- new：与候选不重复、不冲突，直接新增；
- supersede：新条目取代某条旧条目（旧条目过时、被更新）；
- invalidate：新条目证明某条旧条目不再成立，且没有替代（必须给 reason）。

【规则】
1. 只在候选里明确重复或冲突时才选 supersede / invalidate，其余一律 new。
2. target_id 必须原样取自该条目的 candidates 里的 id，不编造。
3. 语义不同但用词相近的条目保持独立，分别新增。
4. reason 用一句话说明依据（≤60 字符），supersede / invalidate 必填。

【输出】只输出一个 JSON 对象，键与类型固定如下（无 markdown 代码块、无解释文字）：
{"decisions":[{"index":0,"action":"new|supersede|invalidate","target_id":null,"reason":""}]}
decisions 的条数与输入条数一致，index 与输入一一对应。

【示例】输入：[{"index":0,"table":"cognition","new":{"content":"项目用 pnpm"},"candidates":[{"id":7,"content":"项目用 npm"}]}]
输出：{"decisions":[{"index":0,"action":"supersede","target_id":7,"reason":"包管理器由 npm 改为 pnpm"}]}

【边界】候选为空时该条选 new；无法判断时选 new。"""


@dataclass(frozen=True)
class Decisions:
    '''裁决结果：index → {action, target_id, reason}。'''
    items: dict
    needs_review: bool = False
    error: str = ""


def _query_for(item: dict) -> str | None:
    cues = item.get("cues") or ()
    if cues:
        return str(cues[0])[:20]
    content = str(item.get("content") or "")
    return content[:12] or None


def gather_candidates(db: database.Database, items: list, *,
                      origin_project: str | None = None,
                      limit: int = MAX_CANDIDATES) -> list:
    '''为每条新条目召回候选旧条目（同 kind、active、未过期）。'''
    result = []
    for item in items:
        if item["table"] == "cognition":
            rows = db.find_l3_candidates(
                "cognition", subject=item.get("subject"),
                query=_query_for(item), origin_project=origin_project, limit=limit)
        else:
            rows = db.find_l3_candidates(
                "experience", query=_query_for(item),
                origin_project=origin_project, limit=limit)
        result.append(rows)
    return result


def build_messages(items: list, candidates: list) -> list:
    '''拼一次批量裁决请求。'''
    entries = []
    for index, (item, rows) in enumerate(zip(items, candidates)):
        entries.append({
            "index": index,
            "table": item["table"],
            "new": {key: value for key, value in item.items()
                    if key not in ("table", "action", "target_id", "reason")},
            "candidates": [{
                "id": row.id,
                "content": row.content,
                "subject": getattr(row, "subject", None),
                "context": getattr(row, "context", None),
            } for row in rows],
        })
    return [
        {"role": "system", "content": CONSOLIDATE_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"items": entries}, ensure_ascii=False)},
    ]


def _load_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None


def parse_decisions(raw: str, items: list, candidates: list) -> Decisions:
    '''
    解析裁决；index 越界、target 不在候选里、动作非法都降级为 new。

    同一批里多个条目指向同一个 target 时只保留**第一条**（按 index 升序），
    其余降级为 new——旧条目至多一个后继，否则 supersede 链会分叉。
    '''
    data = _load_json(raw)
    if data is None or not isinstance(data.get("decisions"), list):
        return Decisions(items={}, needs_review=True, error="json_parse")

    by_index = {}
    used_targets = set()
    for entry in data["decisions"]:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(items):
            continue
        action = str(entry.get("action") or "new").strip().lower()
        target = entry.get("target_id")
        reason = str(entry.get("reason") or "").strip()
        allowed = {row.id for row in candidates[index]}
        if action not in _ACTIONS:
            action = "new"
        if action in ("supersede", "invalidate"):
            if not isinstance(target, int) or target not in allowed:
                action = "new"
            elif action == "invalidate" and not reason:
                action = "new"
            elif target in used_targets and action == "supersede":
                action = "new"          # 同批重复指向：保留首条，其余新增
        if action in ("supersede", "invalidate"):
            used_targets.add(target)
        if action == "new":
            target, reason = None, ""
        by_index[index] = {"action": action, "target_id": target, "reason": reason}
    return Decisions(items=by_index)


def decide(db: database.Database, items: list, transport, *,
           origin_project: str | None = None,
           limit: int = MAX_CANDIDATES) -> Decisions:
    '''召回候选 + 一次模型调用 + 解析；任何失败都退回全 new。'''
    if not items:
        return Decisions(items={})
    candidates = gather_candidates(db, items, origin_project=origin_project,
                                   limit=limit)
    if not any(candidates):
        return Decisions(items={})          # 无候选，不必花一次调用
    payload = build_messages(items, candidates)
    try:
        raw = transport.complete(payload)
    except Exception as exc:
        return Decisions(items={}, needs_review=True, error=str(exc))
    decisions = parse_decisions(raw, items, candidates)
    return decisions

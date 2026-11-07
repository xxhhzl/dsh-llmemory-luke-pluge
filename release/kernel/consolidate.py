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

【「重复/冲突」的判定标准】
先问：新条目和候选旧条目，是在说**同一件事**吗（同一主体 + 同一断言）？
- 同一主体同一断言，仅措辞不同（「项目用 pnpm」vs「包管理器是 pnpm」）→ 重复：保留信息更丰富的，通常 supersede 新条目并保留旧条目里新条目缺少的细节；
- 同一主体，断言矛盾（「上限 200」vs「上限 400」）→ 冲突：新条目更新（通常 supersede 新条目）；若新条目没有依据只是另一种说法，选 new 并保持独立；
- 不同主体、不同断言，只是用词相近 → 不是重复也不是冲突，选 new；
- 候选为空，或无法确认 → new。

【主体归一】认知条目还要判「它讲的是哪个事物」——**与动作无关，单独判断**：
- 只要新条目的 subject 与某个候选指向同一事物、只是写法不同（「项目」vs「项目 X 环境」vs「X 项目」）→ 输出该候选的 subject 作为规范主体；
  即使 action 是 new（断言不同、不该合并），也照样给 subject；
- 没有同一事物的候选 → 留空；
- 无法确定是否同一事物 → 留空。
subject 只能原样取自「新条目的 subject」或改条目的「候选的 subject」，不编造第三种写法。

【规则】
1. 宁可 new 也不要错误合并：语义不同的条目保持独立，分别新增（错误的 supersede 会丢信息，错误的 new 只是多一条，前者更贵）。
2. target_id 必须原样取自该条目的 candidates 里的 id，不编造。
3. reason 用一句话说明依据（≤60 字符），supersede / invalidate 必填。

【输出】只输出一个 JSON 对象，键与类型固定如下（无 markdown 代码块、无解释文字）：
{"decisions":[{"index":0,"action":"new|supersede|invalidate","target_id":null,"reason":"","subject":null}]}
decisions 的条数与输入条数一致，index 与输入一一对应；subject 只对认知有意义，经验一律给 null。

【示例一】（同一主体同一断言，措辞不同 → supersede）
输入：[{"index":0,"table":"cognition","new":{"subject":"项目","content":"构建工具用 Vite"},"candidates":[{"id":7,"subject":"项目","content":"项目用 Vite 做构建"}]}]
输出：{"decisions":[{"index":0,"action":"supersede","target_id":7,"reason":"同一事实，新条目表述更完整","subject":"项目"}]}

【示例二】（同一主体，断言矛盾且新条目有依据 → supersede）
输入：[{"index":0,"table":"cognition","new":{"subject":"摘要配置","content":"摘要上限 400 字符"},"candidates":[{"id":3,"subject":"摘要配置","content":"摘要上限 200 字符"}]}]
输出：{"decisions":[{"index":0,"action":"supersede","target_id":3,"reason":"上限由 200 改为 400，新值取代旧值","subject":"摘要配置"}]}

【示例三】（用词相近但语义不同 → new）
输入：[{"index":0,"table":"cognition","new":{"subject":"检索","content":"范围过滤已下推到召回层"},"candidates":[{"id":9,"subject":"检索","content":"过滤后的结果按相关性排序"}]}]
输出：{"decisions":[{"index":0,"action":"new","target_id":null,"reason":"","subject":null}]}
说明：都提到「过滤」，但一条说的是过滤的位置（架构事实），一条说的是排序（另一件事），语义不同，保持独立。

【示例四】（无候选 → new，主体保持原样）
输入：[{"index":0,"table":"experience","new":{"content":"导出前统一 strokeWidth"},"candidates":[]}]
输出：{"decisions":[{"index":0,"action":"new","target_id":null,"reason":"","subject":null}]}

【示例五】（同一事物不同写法 → 归一到候选的写法）
输入：[{"index":0,"table":"cognition","new":{"subject":"dsh-longmemory-plugin 项目","content":"构建用 pnpm"},"candidates":[{"id":12,"subject":"dsh-longmemory-plugin","content":"项目用 pnpm 做构建"}]}]
输出：{"decisions":[{"index":0,"action":"supersede","target_id":12,"reason":"同一项目同一事实，写法归一","subject":"dsh-longmemory-plugin"}]}

【示例六】（同一事物、不同断言 → new，但主体仍归一）
输入：[{"index":0,"table":"cognition","new":{"subject":"dsh-longmemory-plugin 项目","content":"lint 用 ruff"},"candidates":[{"id":12,"subject":"dsh-longmemory-plugin","content":"项目用 pnpm 做构建"}]}]
输出：{"decisions":[{"index":0,"action":"new","target_id":null,"reason":"","subject":"dsh-longmemory-plugin"}]}
说明：lint 和构建工具是两件事，不该合并（action=new），但都是关于同一个项目，subject 仍归一到候选的写法。
"""


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


def _canonical_subject(entry: dict, item: dict, rows: list) -> str | None:
    '''认知的规范主体：只能原样取自「候选的 subject」或「新条目自己的 subject」。

    模型偶尔会组合出一个新写法——那等于又造一个变体，直接忽略（保持原样）。
    与自值相同也返回 None（无需改写）。
    '''
    if item.get("table") != "cognition":
        return None
    value = str(entry.get("subject") or "").strip()
    if not value:
        return None
    own = str(item.get("subject") or "").strip()
    if value == own:
        return None
    allowed = {str(getattr(row, "subject", "") or "").strip() for row in rows}
    return value[:40] if value in allowed else None


def parse_decisions(raw: str, items: list, candidates: list) -> Decisions:
    '''
    解析裁决；index 越界、target 不在候选里、动作非法都降级为 new。

    同一批里多个条目指向同一个 target 时只保留**第一条**（按 index 升序），
    其余降级为 new——旧条目至多一个后继，否则 supersede 链会分叉。

    认知条目额外带 `subject`（规范主体）：见 `_canonical_subject`，
    只接受取自候选或新条目原值的写法，其余忽略。
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
        by_index[index] = {"action": action, "target_id": target, "reason": reason,
                           "subject": _canonical_subject(
                               entry, items[index], candidates[index])}
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

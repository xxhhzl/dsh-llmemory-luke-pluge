# -*- coding: utf-8 -*-
"""洞见归纳（归纳层）：从多条具体经验中提炼可迁移的规则。

设计（ExpeL 的两级记忆：insights vs experiences，AAAI-24, arXiv:2308.10144）：
- 经验是「某次怎么做」；洞见是「多条经验共同支持的通用规则」，靠 source_experience_ids
  保留证据链，可追溯「凭什么」；
- 归纳是**显式离线操作**（`induce()`），不挂在 ingest 热路径上：模型调用昂贵，
  且需要同一主题积累到足够样本才值得归纳；
- 洞见不替代源经验（只新增高层规则），源经验的 supersede/invalidate 不受影响。

流程：
1. `clusters()`：按主线索（cues[0]）分组，只保留 ≥ min_support 条的组；
   `semantic_clusters()`：按向量余弦相似度分组（线索不同、说的是同一件事也能成组），
   `all_clusters()` 合并两路——实测首线索极度分散，光靠 cue 攒不出可归纳的簇；
2. `build_messages()`：把一组经验压成一次请求；
3. `parse_insights()`：校验证据 id 必须来自输入、至少 2 条支撑、规则非空；
4. `induce()`：调用模型 → 解析 → 写 insight 表 + FTS。
"""
from __future__ import annotations

import json
import re

import base
import database

MAX_CLUSTER_ROWS = 8          # 一次归纳最多喂多少条经验
MAX_CONTENT = 200
SEMANTIC_THRESHOLD = 0.65     # 余弦相似度阈值（实测 0.65 能把同主题经验分到一组）
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

try:                          # 语义聚类依赖 numpy；缺了只跳过这一路
    import numpy as _np
except Exception:             # pragma: no cover - 环境相关
    _np = None

INDUCTION_SYSTEM_PROMPT = """【角色】你是长期记忆的「经验归纳器」：把多条具体经验提炼成更通用、可迁移的规则。

【输入】user 消息是一个 JSON 对象：
{"experiences":[{"id":1,"kind":"technique","scope":"global","context":"...","content":"..."}]}
id 是经验编号，用于在输出里标注证据。

【规则】
1. 每条洞见必须至少被 **2 条**输入经验支持，并在 source_ids 里原样列出这些 id；
2. 洞见写**一般化的规则**，不是某条经验的复述：去掉具体项目名、版本号、只跑过一次的命令等一次性细节；
3. 输入经验互相矛盾、或只有 1 条能支撑时，不要归纳；
4. 没有值得提炼的规则就输出空数组，不要凑数；
5. scope：global = 换个项目仍成立；project = 只在本项目/本环境内成立。

【输出】只输出一个 JSON 对象（无 markdown 代码块、无解释文字）：
{"insights":[{"content":"≤200 字符的规则","scope":"global|project",
  "confidence":0.8,"cues":["检索线索"],"source_ids":[1,2]}]}

【示例】
输入：{"experiences":[
 {"id":1,"kind":"pitfall","content":"edit 报 old_string not found 时先重新 read 目标文件"},
 {"id":2,"kind":"pitfall","content":"改文档表格前先用 Select-String 定位实际行再构造 old_string"},
 {"id":3,"kind":"technique","content":"批量改文件前逐个 read 确认当前内容"}]}
输出：{"insights":[{"content":"编辑已有文本前先读取当前内容再构造替换串，可避免 old_string 不匹配和改错位置","scope":"global","confidence":0.9,"cues":["edit","old_string","先读后改"],"source_ids":[1,2,3]}]}
"""


class InductionError(RuntimeError):
    pass


def clusters(db: database.Database, *, min_support: int = 3,
             limit: int = 10) -> list:
    '''
    按主线索（cues[0]）分组，返回 [(cue, [Experience, ...]), ...]。

    主线索就是 consolidate 召回用的同一个键；没有 cues 的经验不参与归纳
    （没有共同线索说明它们本来就不是同一类做法）。
    '''
    buckets = {}
    for row in db.list_experiences(limit=500):
        cues = getattr(row, "cues", ()) or ()
        if not cues:
            continue
        key = str(cues[0]).strip().casefold()
        if not key:
            continue
        buckets.setdefault(key, []).append(row)
    groups = [(key, rows) for key, rows in buckets.items()
              if len(rows) >= max(2, int(min_support))]
    groups.sort(key=lambda item: (-len(item[1]), item[0]))
    return groups[:limit]


def build_messages(rows: list) -> list:
    '''把一组经验压成一次归纳请求。'''
    entries = [{
        "id": row.id,
        "kind": row.kind,
        "scope": getattr(row, "scope", "project"),
        "context": row.context or "",
        "content": row.content,
    } for row in rows[:MAX_CLUSTER_ROWS]]
    return [
        {"role": "system", "content": INDUCTION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"experiences": entries},
                                                ensure_ascii=False)},
    ]


def _load_json(raw: str):
    text = (raw or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None


def _clip(text, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit]


def parse_insights(raw: str, rows: list) -> list:
    '''
    解析归纳输出；证据不足或越界的条目直接丢弃（不编造证据、不硬凑）。

    :param rows: 本次输入的经验（用于校验 source_ids）
    '''
    data = _load_json(raw)
    if not isinstance(data, dict) or not isinstance(data.get("insights"), list):
        return []
    allowed = {row.id for row in rows}
    result, seen = [], set()
    for item in data["insights"]:
        if not isinstance(item, dict):
            continue
        content = _clip(item.get("content"), MAX_CONTENT)
        if not content or content in seen:
            continue
        sources = []
        for value in item.get("source_ids") or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number in allowed and number not in sources:
                sources.append(number)
        if len(sources) < 2:            # 至少两条经验支撑才算洞见
            continue
        scope = str(item.get("scope") or "global").strip().lower()
        cues = item.get("cues")
        cues = tuple(str(c).strip()[:20] for c in cues[:10]
                     if str(c).strip()) if isinstance(cues, list) else ()
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        seen.add(content)
        result.append({
            "content": content,
            "scope": scope if scope in ("global", "project") else "global",
            "confidence": max(0.0, min(1.0, confidence)),
            "cues": cues,
            "source_ids": tuple(sources),
        })
    return result


def semantic_clusters(db: database.Database, *, min_support: int = 3,
                      limit: int = 10,
                      threshold: float = SEMANTIC_THRESHOLD) -> list:
    '''
    按经验向量的余弦相似度聚类：线索不同但说的是同一件事的经验也能归到一组。

    为什么需要第二路：`cues[0]` 是模型随手写的第一个线索词，实测 37 条经验
    分散在 35 个不同首线索上（≥3 条的组为 0），光靠 cue 永远攒不出可归纳的簇；
    而向量相似度能稳定找出「OLLAMA_MODELS 相关」「模型槽拆分」这类主题组。
    '''
    if _np is None:
        return []
    rows = {row.id: row for row in db.list_experiences(limit=500)
            if getattr(row, "status", "active") == "active"}
    blobs = db.list_vec_blobs("experience")
    ids = [ref_id for ref_id in sorted(blobs) if ref_id in rows]
    if len(ids) < max(2, int(min_support)):
        return []
    matrix = _np.asarray([_np.frombuffer(blobs[ref_id], dtype=_np.float32)
                          for ref_id in ids], dtype=_np.float32)
    norms = _np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    assigned: set = set()
    groups = []
    for index, seed in enumerate(ids):
        if seed in assigned:
            continue
        similarity = matrix @ matrix[index]
        members = [ref_id for position, ref_id in enumerate(ids)
                   if ref_id not in assigned
                   and float(similarity[position]) >= float(threshold)]
        if len(members) < max(2, int(min_support)):
            continue
        order = {ref_id: position for position, ref_id in enumerate(ids)}
        members.sort(key=lambda ref_id: -float(similarity[order[ref_id]]))
        groups.append((f"语义:{seed}", [rows[ref_id] for ref_id in members]))
        assigned.update(members)
        if len(groups) >= int(limit):
            break
    return groups


def all_clusters(db: database.Database, *, min_support: int = 3,
                 limit: int = 10,
                 threshold: float = SEMANTIC_THRESHOLD) -> list:
    '''两路聚类合并：先 cue 簇，再补语义簇（集合重复的跳过）。'''
    groups = clusters(db, min_support=min_support, limit=limit)
    seen = [frozenset(row.id for row in rows) for _, rows in groups]
    for label, rows in semantic_clusters(db, min_support=min_support,
                                         limit=limit, threshold=threshold):
        key = frozenset(row.id for row in rows)
        if any(key <= existing for existing in seen):
            continue
        seen.append(key)
        groups.append((label, rows))
    groups.sort(key=lambda item: (-len(item[1]), item[0]))
    return groups[:max(1, int(limit))]


def induce(db: database.Database, transport, *, min_support: int = 3,
           max_clusters: int = 5, commit: bool = True,
           threshold: float = SEMANTIC_THRESHOLD, groups: list | None = None,
           progress=None) -> list:
    '''
    跑一轮归纳：每个足够大的簇一次模型调用。

    :param transport: 与 summary.SummaryTransport 兼容（`complete(messages)`）
    :param commit: False 时只返回归纳结果、不落库（用于演练/对比）
    :param groups: 预先算好的簇（维护任务要靠它先知道 total 再报进度）
    :param progress: 每处理完一个簇回调 `progress(done, total, item)`
    :return: [{"cluster": cue, "insights": [...], "ids": [...] 或 "error": ...}]
    '''
    if groups is None:
        groups = all_clusters(db, min_support=min_support,
                              limit=max_clusters, threshold=threshold)
    total = len(groups)
    # 已有洞见正文：多个簇常收出同一句话，重复写会让库和检索结果都很脏
    existing = {row.content for row in db.list_insights(limit=500)
                if getattr(row, "status", "active") == "active"}
    report = []
    for position, (cue, rows) in enumerate(groups, start=1):
        shown = rows[:MAX_CLUSTER_ROWS]      # 证据只能来自真正喂给模型的条目
        payload = build_messages(shown)
        try:
            raw = transport.complete(payload)
        except Exception as exc:         # 单簇失败不影响其余簇
            item = {"cluster": cue, "error": str(exc), "insights": [], "ids": []}
            report.append(item)
            if progress is not None:
                progress(position, total, item)
            continue
        insights = parse_insights(raw, shown)
        ids = []
        if commit:
            for item in insights:
                if item["content"] in existing:      # 已归纳过，跳过
                    continue
                insight = database.Insight(
                    content=item["content"], scope=item["scope"],
                    confidence=item["confidence"], importance=4,
                    origin_session=shown[0].origin_session,
                    origin_project=None,          # 洞见默认全局可见，scope 表达适用性
                    source_experience_ids=item["source_ids"],
                    cues=item["cues"], created_at=base.get_cur_time())
                new_id = db.insert_insight(insight)
                existing.add(item["content"])
                db.index_fts("insight", new_id, "", insight.content,
                             origin_project=None,
                             session_id=shown[0].origin_session)
                ids.append(new_id)
                try:                    # 向量索引：失败不影响洞见落库
                    import embed
                    row = db.get_insight(new_id)
                    if row is not None:
                        embed.index_rows(db, [("insight", row)])
                except Exception:
                    pass
        entry = {"cluster": cue, "insights": insights, "ids": ids}
        report.append(entry)
        if progress is not None:
            progress(position, total, entry)
    return report

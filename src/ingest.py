# -*- coding: utf-8 -*-
"""事件写入链路（调用 A 的落库侧）。

职责：把 `summary.SummaryResult` 变成库里的行——
- `event`：标题 + 摘要（失败时摘要留空，符合 docs/tools.md 的失败口径）；
- `experience` / `cognition`：新条目（A 调用只产出 new，裁决交给调用 B）；
- `memory_fts`：事件摘要与 l3 正文的检索索引；
- `digest`：按最近事件 + 高重要度 l3 重新拼装工作记忆。

幂等：同一 `source_event_id` 下内容相同的条目不会重复插入，
因此调用 B 重跑或事件重放不会产生重复行（无需额外唯一索引）。
"""
from __future__ import annotations

from dataclasses import dataclass

import base
import database
from summary import SummaryResult


@dataclass(frozen=True)
class IngestResult:
    event: database.Event
    experience_ids: tuple = ()
    cognition_ids: tuple = ()
    invalidated: tuple = ()
    fts_rows: int = 0
    digest_revision: int = 0


def _existing_id(db: database.Database, table: str, event_id: int,
                 content: str) -> int | None:
    return db.find_l3_id(table, event_id, content)


def build_experience(item: dict, *, event_id: int, session_id: str,
                     origin_project: str | None) -> database.Experience:
    '''把摘要器产出的经验 dict 变成行模型（不入库）。'''
    return database.Experience(
        context=item.get("context") or None,
        content=item["content"],
        kind=item.get("kind", "technique"),
        confidence=float(item.get("confidence", 0.8)),
        importance=int(item.get("importance", 3)),
        origin_session=session_id,
        origin_project=origin_project,
        source_event_id=event_id,
        cues=tuple(item.get("cues") or ()),
        created_at=base.get_cur_time(),
    )


def build_cognition(item: dict, *, event_id: int, session_id: str,
                    origin_project: str | None) -> database.Cognition:
    '''把摘要器产出的认知 dict 变成行模型（不入库）。'''
    return database.Cognition(
        subject=item["subject"],
        content=item["content"],
        kind=item.get("kind", "fact"),
        confidence=float(item.get("confidence", 0.8)),
        importance=int(item.get("importance", 3)),
        origin_session=session_id,
        origin_project=origin_project,
        source_event_id=event_id,
        cues=tuple(item.get("cues") or ()),
        created_at=base.get_cur_time(),
    )


def _insert_experience(db: database.Database, event_id: int, item: dict,
                       session_id: str, origin_project: str | None) -> int:
    existing = _existing_id(db, "experience", event_id, item["content"])
    if existing is not None:
        return existing
    return db.insert_experience(build_experience(
        item, event_id=event_id, session_id=session_id,
        origin_project=origin_project))


def _insert_cognition(db: database.Database, event_id: int, item: dict,
                      session_id: str, origin_project: str | None) -> int:
    existing = _existing_id(db, "cognition", event_id, item["content"])
    if existing is not None:
        return existing
    return db.insert_cognition(build_cognition(
        item, event_id=event_id, session_id=session_id,
        origin_project=origin_project))


def apply_summary(db: database.Database, *, session_id: str,
                  summary: SummaryResult,
                  event_id: int | None = None,
                  origin_project: str | None = None,
                  related_events=None,
                  digest_limit: int = 5,
                  decisions: dict | None = None) -> IngestResult:
    '''
    把一次摘要结果写入库：事件 + 经验 + 认知 + 全文索引 + digest，同一事务完成。

    :param event_id: 已有事件 id（切分器先建了事件）；None 表示新建
    :param origin_project: 写入 l3 的来源项目（检索时决定可见性）
    :param decisions: 调用 B 的裁决 {index: {action, target_id, reason}}；
                      None 表示全部按 new 处理
    '''
    title = summary.title or "未命名事件"
    summary_text = summary.summary if summary.ok and summary.summary else None
    items = []
    if summary.ok:
        items = [dict(item, table="experience") for item in summary.experiences] + \
                [dict(item, table="cognition") for item in summary.cognitions]
    decisions = decisions or {}

    with db.transaction():
        if event_id is None:
            event = db.create_event(session_id, title, summary=summary_text,
                                    related_events=related_events)
        else:
            event = db.get_event(event_id)
            if event is None:
                raise KeyError(f"event {event_id} 不存在")
            db.set_event_title(event_id, title)
            db.set_event_summary(event_id, summary_text)

        experience_ids, cognition_ids, invalidated, written = [], [], [], []
        for index, item in enumerate(items):
            decision = decisions.get(index) or {"action": "new"}
            item_id, invalidated_id = _apply_item(
                db, event.id, item, decision, session_id, origin_project)
            if item_id:
                written.append((item["table"], item_id, item["content"]))
                if item["table"] == "experience":
                    experience_ids.append(item_id)
                else:
                    cognition_ids.append(item_id)
            if invalidated_id:
                invalidated.append(invalidated_id)

        db.index_fts("event", event.id, title, summary_text or "")
        fts_rows = 1
        for kind, item_id, content in written:
            db.index_fts(kind, item_id, "", content)
            fts_rows += 1

    revision = refresh_digest(db, session_id, limit=digest_limit,
                              origin_project=origin_project)
    refreshed = db.get_event(event.id)
    return IngestResult(event=refreshed or event,
                        experience_ids=tuple(experience_ids),
                        cognition_ids=tuple(cognition_ids),
                        invalidated=tuple(invalidated),
                        fts_rows=fts_rows,
                        digest_revision=revision)


def _apply_item(db: database.Database, event_id: int, item: dict, decision: dict,
                session_id: str, origin_project: str | None) -> tuple:
    '''按裁决写入一条 l3，返回 (新条目 id | None, 被失效的旧 id | None)。'''
    action = (decision or {}).get("action", "new")
    target = (decision or {}).get("target_id")
    reason = (decision or {}).get("reason") or "新事件表明该条目不再成立"

    if item["table"] == "experience":
        row = build_experience(item, event_id=event_id, session_id=session_id,
                               origin_project=origin_project)
        if action == "supersede" and isinstance(target, int):
            return db.supersede_experience(target, row), None
        if action == "invalidate" and isinstance(target, int):
            if db.invalidate_experience(target, reason):
                return None, target
        return _insert_experience(db, event_id, item, session_id, origin_project), None

    row = build_cognition(item, event_id=event_id, session_id=session_id,
                          origin_project=origin_project)
    if action == "supersede" and isinstance(target, int):
        return db.supersede_cognition(target, row), None
    if action == "invalidate" and isinstance(target, int):
        if db.invalidate_cognition(target, reason):
            return None, target
    return _insert_cognition(db, event_id, item, session_id, origin_project), None


def ingest_event(db: database.Database, *, session_id: str,
                 summary: SummaryResult, transport=None,
                 event_id: int | None = None,
                 origin_project: str | None = None,
                 related_events=None, digest_limit: int = 5) -> IngestResult:
    '''
    完整落库流水线：A 的结果 →（可选）调用 B 裁决 → 写库。

    :param transport: 裁决模型（summary.SummaryTransport 兼容）；None 表示跳過裁决
    '''
    decisions = {}
    if transport is not None and summary.ok:
        items = [dict(item, table="experience") for item in summary.experiences] + \
                [dict(item, table="cognition") for item in summary.cognitions]
        if items:
            import consolidate
            decisions = consolidate.decide(
                db, items, transport, origin_project=origin_project).items
    return apply_summary(db, session_id=session_id, summary=summary,
                         event_id=event_id, origin_project=origin_project,
                         related_events=related_events, digest_limit=digest_limit,
                         decisions=decisions)


def refresh_digest(db: database.Database, session_id: str, *,
                   limit: int = 5, origin_project: str | None = None,
                   top_l3: int = 3) -> int:
    '''按「最近 N 个事件 + 高重要度 l3」重拼工作记忆，返回新 revision。'''
    events = db.list_events(session_id, limit=limit)
    experiences = sorted(
        db.list_experiences(origin_project=origin_project, limit=50),
        key=lambda item: (-item.importance, -item.id))[:top_l3]
    cognitions = sorted(
        db.list_cognitions(origin_project=origin_project, limit=50),
        key=lambda item: (-item.importance, -item.id))[:top_l3]

    lines = []
    for event in reversed(events):
        body = f"{event.title}：{event.summary}" if event.summary else event.title
        lines.append(f"[L2 #{event.id}] {body}")
    for item in experiences:
        lines.append(f"[L3 经验] {item.content}")
    for item in cognitions:
        lines.append(f"[L3 认知] {item.content}")

    content = "\n".join(lines) or "（暂无记忆）"
    refs = [event.id for event in reversed(events)]      # 按时间升序存来源
    return db.upsert_digest(session_id, content, refs)

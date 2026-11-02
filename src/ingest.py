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

import queue
import threading
import uuid
from dataclasses import dataclass

import base
import config
import database
import error
import frame
from jev import ShellTransport
from segmenter import SegmentConfig, Segmenter, estimate_tokens
from summary import (
    EventMeta,
    ShellSummaryTransport,
    SummaryResult,
    extract,
)


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
                  decisions: dict | None = None,
                  message_ids: list | None = None) -> IngestResult:
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

        # 把事件覆盖的消息绑定到 event_id（供后续判重与 memory_get 取正文）
        for message_id in message_ids or []:
            db.bind_event(session_id, message_id, event.id)

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
                 related_events=None, digest_limit: int = 5,
                 message_ids: list | None = None) -> IngestResult:
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
                         decisions=decisions, message_ids=message_ids)


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


# ---------------- 异步 worker 与协议入口 ----------------

class IngestWorker:
    """单线程任务队列：切分 → 摘要 → 裁决 → 落库，输出 ingest.completed。"""

    def __init__(self, q, *, db_factory=None, jev_factory=None,
                 summary_factory=None, consolidate_factory=None):
        self._q = q
        self._db_factory = db_factory or database.Database
        self._jev_factory = jev_factory or (lambda: ShellTransport())
        self._summary_factory = summary_factory or (
            lambda: ShellSummaryTransport(purpose="summary"))
        self._consolidate_factory = consolidate_factory or (
            lambda: ShellSummaryTransport(purpose="consolidate"))
        self._jobs = queue.Queue()
        self._thread = None
        self._by_id = {}

    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._loop, name="ingest-worker", daemon=True)
            self._thread.start()
        return self._thread

    def submit(self, job: dict) -> None:
        self._jobs.put(job)

    def _loop(self):
        while True:
            job = self._jobs.get()
            try:
                self.process(job)
            except Exception as exc:      # 单任务失败不拖垮 worker
                base.log.error("ingest.failed", error=repr(exc),
                               job_id=job.get("job_id"))
                error.send_error_message(
                    job.get("envelope") or {}, self._q, -32603,
                    "内核内部错误", f"ingest 失败: {exc!r}")

    def process(self, job: dict) -> dict:
        db = self._db_factory()
        try:
            stored = self._store_messages(db, job)
            result = self._segment(db, job)
            counts = self._summarize_events(db, job, result)
            digest = db.get_digest(job["session_id"])
            content = digest.content if digest else ""
            payload = {
                "summary": {
                    "messages": stored,
                    "events_created": counts["created"],
                    "events_resumed": counts["resumed"],
                    "segments_closed": counts["closed"],
                    "summaries": counts["summaries"],
                },
                "display": (f"已处理 {stored} 条消息：新建 {counts['created']} 个事件、"
                            f"恢复 {counts['resumed']} 个、生成 {counts['summaries']} 条摘要"),
                "digest": content,
                "digest_tokens": estimate_tokens(content),
                "decisions": self._decisions(result),
            }
            frame.push(self._q, job["envelope"], "ingest.completed", payload)
            return payload
        finally:
            db.close()

    def _store_messages(self, db, job) -> int:
        inserted = 0
        now = base.get_cur_time()
        for item in job.get("messages") or []:
            try:
                seq = int(item["seq"])
            except (KeyError, TypeError, ValueError):
                continue
            occur = str(item.get("ts") or now)
            msg_id = str(item.get("msg_id") or "")
            if not msg_id:
                continue
            message = database.Message(
                session_id=job["session_id"], message_id=msg_id,
                owner=job.get("agent_id") or "", seq=seq,
                role=str(item.get("role") or "user"),
                content=str(item.get("content") or ""),
                occur_at=occur, created_at=now)
            if db.insert_message(message):
                inserted += 1
            db.insert_memory(database.Memory(
                session_id=job["session_id"], message_id=msg_id,
                owner=job.get("agent_id") or "", seq=seq,
                occur_at=occur, created_at=now))
        return inserted

    def _segment(self, db, job):
        cfg = config.get_config().get("split", {})
        jev_cfg = config.get_config().get("jev", {})
        rows = db.list_messages(job["session_id"], limit=200000)
        messages = [{"msg_id": row.message_id, "seq": row.seq, "role": row.role,
                     "content": row.content, "ts": row.occur_at} for row in rows]
        self._by_id = {m["msg_id"]: m for m in messages}
        return Segmenter(
            transport=self._jev_factory(),
            config=SegmentConfig(
                gap_limit_minutes=int(cfg.get("gap_minutes", 120)),
                max_event_tokens=int(cfg.get("max_event_tokens", 128000)),
                max_messages_per_event=int(cfg.get("max_messages", 1000)),
                use_autothink=jev_cfg.get("on_uncertain", "escalate") == "escalate",
            ),
        ).run(messages)

    def _summarize_events(self, db, job, result) -> dict:
        created = resumed = summaries = closed = 0
        for event in result.events:
            first = event.message_ids[0] if event.message_ids else None
            bound = db.event_for_message(job["session_id"], first) if first else None
            if bound:
                # 已存在的事件：把后续新消息补绑到同一事件
                for message_id in event.message_ids[1:]:
                    db.bind_event(job["session_id"], message_id, bound)
                resumed += 1
                continue
            msgs = [self._by_id[mid] for mid in event.message_ids if mid in self._by_id]
            if not msgs:
                continue
            meta = EventMeta(event_id=0, session_id=job["session_id"],
                             message_count=len(msgs), first_at=msgs[0]["ts"],
                             last_at=msgs[-1]["ts"], tokens=event.tokens)
            summary = extract(meta, msgs, self._summary_factory())
            ingest_event(db, session_id=job["session_id"], summary=summary,
                         transport=self._consolidate_factory(),
                         origin_project=job.get("origin_project"),
                         message_ids=event.message_ids)
            created += 1
            summaries += 1 if summary.ok else 0
            closed += 1
        return {"created": created, "resumed": resumed,
                "summaries": summaries, "closed": closed}

    @staticmethod
    def _decisions(result) -> list:
        return [{
            "msg_id": decision.message_id,
            "decision": "NEW" if decision.decision == "SPLIT" else "CONTINUE",
            "event_id": None,
            "segment_no": None,
            "seq": decision.seq,
            "p_split": decision.p_split,
            "source": decision.source,
            "needs_review": decision.needs_review,
        } for decision in result.decisions]


_worker = None
_worker_lock = threading.Lock()


def start_worker(q, **kwargs) -> IngestWorker:
    """启动 ingest worker（main 在启动时调用一次）。"""
    global _worker
    with _worker_lock:
        _worker = IngestWorker(q, **kwargs)
        _worker.start()
        return _worker


def get_worker() -> IngestWorker:
    if _worker is None:
        raise RuntimeError("ingest worker 未启动：main() 需要先调用 ingest.start_worker(queue)")
    return _worker


def ingest_request(data, q):
    """ingest.request 处理器：立即回执，把任务交给 worker 异步处理。"""
    payload = data.get("data") or {}
    messages = payload.get("messages") or []
    if not isinstance(messages, list):
        error.send_error_message(data, q, -32602, "参数不合法", "messages 必须是数组")
        return
    cursor = payload.get("cursor") or {}
    try:
        last_seq = int(cursor.get("last_seq") or 0)
    except (TypeError, ValueError):
        last_seq = 0

    def seq_of(item):
        try:
            return int(item.get("seq", -1))
        except (TypeError, ValueError):
            return -1

    new_messages = list(messages) if payload.get("full") else [
        item for item in messages if seq_of(item) > last_seq]
    job_id = data.get("job_id") or f"job_{uuid.uuid4().hex[:8]}"
    envelope = dict(data)
    envelope["job_id"] = job_id

    accepted = bool(new_messages)
    frame.push(q, envelope, "ingest.receipt", {
        "accepted": accepted,
        "new_messages": len(new_messages),
        "display": (f"正在写入 {len(new_messages)} 条新消息…" if accepted
                    else "没有新消息需要处理"),
    })
    if not accepted:
        return
    get_worker().submit({
        "job_id": job_id,
        "session_id": data.get("session_id", ""),
        "agent_id": data.get("agent_id", ""),
        "messages": new_messages,
        "envelope": envelope,
        "origin_project": payload.get("origin_project"),
    })

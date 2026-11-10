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
from dataclasses import dataclass, replace
from datetime import datetime, timezone

import base
import config
import sanitize
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

# 补跑摘要的最小 token 增量：低于它不因「比例达标」而重跑
MIN_RESUMMARY_TOKENS = 50

# 单次切分最多处理的消息条数（增量路径下这是「新消息」上限，全量路径下是兜底）
FULL_SCAN_LIMIT = 200000


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
        scope=item.get("scope") if item.get("scope") in ("global", "project") else "project",
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


def _existing_memories(db: database.Database, messages: list, *,
                       per_kind: int = 5) -> list:
    '''
    取与本事件可能相关的旧记忆（经验/认知/洞见），供调用 A 直接判重。

    与 mem0 的 context lookup、LangMem 的 `existing` 入参对齐：让提取模型
    在写之前就看到旧条目，才能用 action/target_id 合并，而不是盲写后再补救。
    '''
    text = " ".join(str(m.get("content") or "") for m in messages
                    if m.get("role") in ("user", "assistant"))
    text = " ".join(text.split())
    if not text:
        return []
    query = text[-600:]
    try:
        hits = db.search_fts(query, limit=40)
    except Exception as exc:                  # 检索失败不影响提取主链路
        base.log.warning("ingest.existing_lookup_failed", error=repr(exc))
        return []

    ordered: dict = {}
    for hit in hits:
        kind = hit.get("ref_kind")
        if kind not in ("experience", "cognition", "insight"):
            continue
        row_id = int(hit["ref_id"])
        bucket = ordered.setdefault(kind, [])
        if row_id not in bucket:
            bucket.append(row_id)

    result = []
    for kind, ids in ordered.items():
        rows = db.get_many(kind, ids[:per_kind * 2])
        kept = 0
        for row_id in ids:
            row = rows.get(row_id)
            if row is None:
                continue
            item = {"kind": kind, "id": row_id, "content": row.content}
            if kind == "experience":
                item["subkind"] = getattr(row, "kind", "")
                item["scope"] = getattr(row, "scope", None)
            elif kind == "cognition":
                item["subkind"] = getattr(row, "kind", "")
                item["subject"] = getattr(row, "subject", "")
            else:
                item["scope"] = getattr(row, "scope", None)
            result.append(item)
            kept += 1
            if kept >= per_kind:
                break
    return result


def apply_summary(db: database.Database, *, session_id: str,
                  summary: SummaryResult,
                  event_id: int | None = None,
                  origin_project: str | None = None,
                  related_events=None,
                  digest_limit: int = 5,
                  decisions: dict | None = None,
                  message_ids: list | None = None,
                  refresh: bool = True) -> IngestResult:
    '''
    把一次摘要结果写入库：事件 + 经验 + 认知 + 全文索引 + digest，同一事务完成。

    :param event_id: 已有事件 id（切分器先建了事件）；None 表示新建
    :param origin_project: 写入 l3 的来源项目（检索时决定可见性）
    :param decisions: 调用 B 的裁决 {index: {action, target_id, reason}}；
                      None 表示全部按 new 处理
    :param refresh: 是否在本事件后重拼 digest；批处理时传 False，由 job 末尾统一刷
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
                                    related_events=related_events,
                                    origin_project=origin_project)
        else:
            event = db.get_event(event_id)
            if event is None:
                raise KeyError(f"event {event_id} 不存在")
            db.set_event_title(event_id, title)
            # 只在摘要成功时覆盖：失败/空的补跑不能把上一次的好摘要抹成 NULL，
            # 也不能把 summarized_at 往后挪（否则 24h 阈值会长期不补跑）。
            if summary_text:
                db.set_event_summary(event_id, summary_text,
                                     summarized_at=base.get_cur_time())

        # 新事件：摘要成功时记录摘要时间（重跑阈值用）
        if event_id is None and summary_text:
            db.set_event_summary(event.id, summary_text,
                                 summarized_at=base.get_cur_time())
        # 复核状态落库：重启后仍能查到哪些事件待补摘要/待确认（不只存在回执里）
        if (not summary.ok) or summary.needs_review:
            db.set_event_review(event.id, True,
                                "summary_failed" if not summary.ok else "needs_review")
        elif event_id is not None:
            db.set_event_review(event.id, False, None)

        # 把事件覆盖的消息绑定到 event_id（供后续判重与 memory_get 取正文）。
        # strict：已归属其他事件的正文不再改绑——事件闭合后正文不追加。
        for message_id in message_ids or []:
            if not db.bind_event(session_id, message_id, event.id, strict=True):
                bound = db.bound_event_of(session_id, message_id)
                if bound is not None and bound != event.id:
                    base.log.warning("ingest.bind_conflict", session_id=session_id,
                                     message_id=message_id, bound_event=bound,
                                     target_event=event.id)

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

        # 事件行的范围列取事件自身的归属，不是本次 job 的项目：
        # 全局事件（origin_project=NULL）被某个项目续写后，仍应全局可见。
        db.index_fts("event", event.id, title, summary_text or "",
                     origin_project=event.origin_project, session_id=session_id)
        fts_rows = 1
        for kind, item_id, content in written:
            db.index_fts(kind, item_id, "", content,
                         origin_project=origin_project, session_id=session_id)
            fts_rows += 1

    if refresh:
        revision = refresh_digest(db, session_id, limit=digest_limit,
                                  origin_project=origin_project)
    else:
        revision = 0
    refreshed = db.get_event(event.id)
    return IngestResult(event=refreshed or event,
                        experience_ids=tuple(experience_ids),
                        cognition_ids=tuple(cognition_ids),
                        invalidated=tuple(invalidated),
                        fts_rows=fts_rows,
                        digest_revision=revision)


def _apply_item(db: database.Database, event_id: int, item: dict, decision: dict,
                session_id: str, origin_project: str | None) -> tuple:
    '''
    按裁决写入一条 l3，返回 (新条目 id | None, 被失效的旧 id | None)。
    目标行已不是 active（重复指向 / 已被并发取代）时降级为新增并告警——
    绝不因为模型给了一个过期 target 而回滚整个事件。
    '''
    action = (decision or {}).get("action", "new")
    target = (decision or {}).get("target_id")
    reason = (decision or {}).get("reason") or "新事件表明该条目不再成立"

    # 调用 B 给出的规范主体：向候选已有写法收敛，避免同物异名越积越多。
    # 只对认知生效；写法已在 parse_decisions 校验过来源（候选或自值）。
    if item["table"] == "cognition":
        canonical = (decision or {}).get("subject")
        if canonical and canonical != item.get("subject"):
            item = {**item, "subject": canonical}

    if item["table"] == "experience":
        row = build_experience(item, event_id=event_id, session_id=session_id,
                               origin_project=origin_project)
        if action == "supersede" and isinstance(target, int):
            new_id = db.supersede_experience(target, row)
            if new_id is not None:
                return new_id, None
            base.log.warning("ingest.supersede_stale", table="experience",
                             target=target, event_id=event_id)
        if action == "invalidate" and isinstance(target, int):
            if db.invalidate_experience(target, reason):
                return None, target
            base.log.warning("ingest.invalidate_stale", table="experience",
                             target=target, event_id=event_id)
        return _insert_experience(db, event_id, item, session_id, origin_project), None

    row = build_cognition(item, event_id=event_id, session_id=session_id,
                          origin_project=origin_project)
    if action == "supersede" and isinstance(target, int):
        new_id = db.supersede_cognition(target, row)
        if new_id is not None:
            return new_id, None
        base.log.warning("ingest.supersede_stale", table="cognition",
                         target=target, event_id=event_id)
    if action == "invalidate" and isinstance(target, int):
        if db.invalidate_cognition(target, reason):
            return None, target
        base.log.warning("ingest.invalidate_stale", table="cognition",
                         target=target, event_id=event_id)
    return _insert_cognition(db, event_id, item, session_id, origin_project), None


def ingest_event(db: database.Database, *, session_id: str,
                 summary: SummaryResult, transport=None,
                 event_id: int | None = None,
                 origin_project: str | None = None,
                 related_events=None, digest_limit: int = 5,
                 message_ids: list | None = None,
                 refresh: bool = True) -> IngestResult:
    '''
    完整落库流水线：A 的结果 →（可选）调用 B 裁决 → 写库。

    :param transport: 裁决模型（summary.SummaryTransport 兼容）；None 表示跳过裁决
    :param refresh: 是否立即重拼 digest；批处理时由 job 末尾统一刷
    '''
    decisions = {}
    if summary.ok:
        items = [dict(item, table="experience") for item in summary.experiences] + \
                [dict(item, table="cognition") for item in summary.cognitions]
        # 调用 A 若带了 [已有记忆] 并直接给出合并动作，优先采用（target 已按表校验）
        for index, item in enumerate(items):
            action = item.get("action") or "new"
            target = item.get("target_id")
            if action in ("supersede", "invalidate") and isinstance(target, int):
                decisions[index] = {"action": action, "target_id": target,
                                    "reason": item.get("reason") or "",
                                    "subject": None}
        if transport is not None and items:
            import consolidate
            b_decisions = consolidate.decide(
                db, items, transport, origin_project=origin_project).items
            for index, decision in b_decisions.items():
                if index in decisions:
                    # A 的动作保留；B 只补主体归一
                    if decision.get("subject"):
                        decisions[index]["subject"] = decision["subject"]
                else:
                    decisions[index] = decision
    return apply_summary(db, session_id=session_id, summary=summary,
                         event_id=event_id, origin_project=origin_project,
                         related_events=related_events, digest_limit=digest_limit,
                         decisions=decisions, message_ids=message_ids,
                         refresh=refresh)


def promote_experiences(db: database.Database, *, min_use: int = 3,
                        limit: int = 20) -> list[int]:
    '''
    泛化晋升：把被反复使用（`use_count >= min_use`）的项目经验转成**通用**条目。

    对应人类的「技能泛化」：在某个项目里反复验证过的做法，脱离原情境仍然成立。
    实现遵循只追加语义——新行 `origin_project=NULL` 并取代旧行（旧行保留、状态
    转 superseded），不改任何已写正文；全文索引同步更新。

    :return: 新（通用）条目 id 列表
    '''
    promoted = []
    rows = db.list_experiences(limit=200)
    for row in rows:
        if len(promoted) >= limit:
            break
        if row.origin_project is None or int(row.use_count or 0) < min_use:
            continue
        candidate = replace(
            row, id=0, origin_project=None, scope="global", superseded_id=None,
            status="active", valid_to=None, invalid_reason=None,
            use_count=0, last_used_at=None, created_at=base.get_cur_time())
        new_id = db.supersede_experience(row.id, candidate)
        if new_id is None:
            continue
        db.index_fts("experience", new_id, "", candidate.content,
                     origin_project=None, session_id=row.origin_session)
        promoted.append(new_id)
        base.log.info("ingest.experience_promoted", old_id=row.id, new_id=new_id,
                      use_count=row.use_count)
    return promoted


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
    insights = sorted(
        db.list_insights(origin_project=origin_project, limit=50),
        key=lambda item: (-item.importance, -item.id))[:2]      # 洞见优先展示

    lines = []
    for event in reversed(events):
        body = f"{event.title}：{event.summary}" if event.summary else event.title
        lines.append(f"[L2 #{event.id}] {body}")
    for item in insights:
        lines.append(f"[L4 洞见] {item.content}")
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
                # 只回错误类型：异常文本可能带路径/正文片段，不往外壳透
                error.send_error_message(
                    job.get("envelope") or {}, self._q, -32603,
                    "内核内部错误", f"ingest 失败: {type(exc).__name__}")

    def process(self, job: dict) -> dict:
        db = self._db_factory()
        try:
            stored, duplicates, conflicts = self._store_messages(db, job)
            result = self._segment(db, job)
            counts = self._summarize_events(db, job, result)
            # digest 在 job 末尾统一重拼一次（事件级刷新已关掉）
            refresh_digest(db, job["session_id"],
                           origin_project=job.get("origin_project"))
            digest = db.get_digest(job["session_id"])
            content = digest.content if digest else ""
            payload = {
                "summary": {
                    "messages": stored,
                    "duplicates": duplicates,
                    "seq_conflicts": conflicts,
                    "events_created": counts["created"],
                    "events_resumed": counts["resumed"],
                    "segments_closed": counts["closed"],
                    "summaries": counts["summaries"],
                    "failed": counts["failed"],
                },
                "display": (f"已处理 {stored} 条消息"
                            + (f"（跳过重复 {duplicates} 条）" if duplicates else "")
                            + (f"（seq 冲突 {conflicts} 条，正文未写入）" if conflicts else "")
                            + f"：新建 {counts['created']} 个事件、"
                            f"恢复 {counts['resumed']} 个、生成 {counts['summaries']} 条摘要"
                            + (f"、失败 {counts['failed']} 个" if counts["failed"] else "")),
                "digest": content,
                "digest_tokens": estimate_tokens(content),
                "decisions": self._decisions(result),
            }
            frame.push(self._q, job["envelope"], "ingest.completed", payload)
            return payload
        finally:
            db.close()

    def _store_messages(self, db, job) -> tuple:
        '''
        返回 (实际入库条数, 重复跳过条数, seq 冲突条数)。
        整批在**同一事务**内写入，崩溃不留「有正文无引用」的半状态；
        重复按 msg_id 判重不报错，seq 撞车（不同 msg_id 抢同一 seq）单独计数并告警。
        '''
        inserted = duplicates = conflicts = 0
        seen = set()
        now = base.get_cur_time()
        with db.transaction():
            for item in job.get("messages") or []:
                try:
                    seq = int(item["seq"])
                except (KeyError, TypeError, ValueError):
                    continue
                # 归一成 UTC：跨时区（Z / +08:00）混存时字典序比较才不会错位。
                # 兜底值也必须是 UTC，否则非法 ts 会以 +08:00 落库，混进 184 条 UTC 里。
                occur = base.to_utc_iso(str(item.get("ts") or ""),
                                        default=base.to_utc_iso(now))
                msg_id = str(item.get("msg_id") or "")
                if not msg_id:
                    continue
                if msg_id in seen:
                    duplicates += 1
                    continue
                seen.add(msg_id)
                message = database.Message(
                    session_id=job["session_id"], message_id=msg_id,
                    owner=job.get("agent_id") or "", seq=seq,
                    role=str(item.get("role") or "user"),
                    # 入库即脱敏：记忆库比 .env 更容易被整库拷走
                    content=sanitize.redact(str(item.get("content") or "")),
                    occur_at=occur, created_at=now)
                rc = db.insert_message(message)
                if rc == 1:
                    inserted += 1
                elif rc == 0:
                    duplicates += 1
                    continue
                else:                       # -1：seq 已被别的 msg_id 占用
                    conflicts += 1
                    base.log.warning("ingest.seq_conflict",
                                     session_id=job["session_id"],
                                     message_id=msg_id, seq=seq)
                    continue
                db.insert_memory(database.Memory(
                    session_id=job["session_id"], message_id=msg_id,
                    owner=job.get("agent_id") or "", seq=seq,
                    occur_at=occur, created_at=now))
                # 原文索引与正文同事务：摘要常漏掉编号/原话，这些细节要能搜到。
                db.index_message_fts(job["session_id"], msg_id,
                                     message.content)
        return inserted, duplicates, conflicts

    def _segment(self, db, job):
        '''
        切分：默认**增量**——只判定「最后一条已绑定消息之后」的新消息。

        旧实现每次都把整个会话重新拉出来重切，模型调用随会话长度线性叠加，
        累计 O(n²)。增量后已绑定事件的正文不再送模型判定，续写段的上下文
        （已有点数、摘要、上一条消息）由 `open_event` / `prev_message` 补齐。
        '''
        cfg = config.get_config().get("split", {})
        jev_cfg = config.get_config().get("jev", {})
        session = job["session_id"]

        anchor_seq = db.last_bound_seq(session)
        open_event = None
        prev_message = None
        if anchor_seq is None:
            rows = db.list_messages(session, limit=FULL_SCAN_LIMIT)
        else:
            anchor = db.message_at_seq(session, anchor_seq)
            anchor_event = (db.event_for_message(session, anchor.message_id)
                            if anchor else None)
            if anchor is None or anchor_event is None:
                rows = db.list_messages(session, limit=FULL_SCAN_LIMIT)
            else:
                event = db.get_event(anchor_event)
                event_msgs = db.list_event_messages(anchor_event)
                # token 口径必须与 segmenter._message_tokens 一致：
                # dialog 只数 user/assistant，否则 tool 输出会让续写段预算虚高、提前硬切
                scope = cfg.get("token_scope", "dialog")
                open_event = {
                    "event_id": anchor_event,
                    "message_count": len(event_msgs),
                    "tokens": sum(
                        estimate_tokens(row.content) for row in event_msgs
                        if scope == "all" or row.role in ("user", "assistant")),
                    "title": event.title if event else "",
                    "summary": (event.summary or "") if event else "",
                }
                prev_message = {"msg_id": anchor.message_id, "seq": anchor.seq,
                                "role": anchor.role, "content": anchor.content,
                                "ts": anchor.occur_at}
                rows = db.list_messages(session, after_seq=anchor_seq,
                                        limit=FULL_SCAN_LIMIT)

        messages = [{"msg_id": row.message_id, "seq": row.seq, "role": row.role,
                     "content": row.content, "ts": row.occur_at} for row in rows]
        self._by_id = {m["msg_id"]: m for m in messages}
        if prev_message is not None:
            self._by_id.setdefault(prev_message["msg_id"], prev_message)
        return Segmenter(
            transport=self._jev_factory(),
            config=SegmentConfig(
                gap_limit_minutes=int(cfg.get("gap_minutes", 120)),
                max_event_tokens=int(cfg.get("max_event_tokens", 128000)),
                max_messages_per_event=int(cfg.get("max_messages", 1000)),
                use_autothink=jev_cfg.get("on_uncertain", "escalate") == "escalate",
                token_scope=cfg.get("token_scope", "dialog"),
            ),
        ).run(messages, open_event=open_event, prev_message=prev_message)

    def _summarize_events(self, db, job, result) -> dict:
        '''
        逐个事件摘要落库。

        三条保护（2026-11-03 定稿）：
        - **单事件失败不断 job**：已提交的事件留在库里，后面的事件继续处理，
          失败数进 `failed` 并由回执报告（旧实现一处异常整批中断）；
        - **resume 补跑有阈值**：只有新增消息足够多才重跑调用 A，避免边界抖动
          放大模型成本；重跑时传 `previous_summary`（递归摘要）；
        - **digest 在 job 末尾统一刷一次**：N 个事件不再重拼 N 次。
        '''
        created = resumed = summaries = closed = conflicts = failed = 0
        for event in result.events:
            # 增量切分：续写段直接绑到已有事件
            if event.event_id:
                try:
                    added = self._bind_new_messages(db, job, event.message_ids,
                                                    event.event_id)
                    conflicts += added[1]
                    if added[0]:
                        resumed += 1
                        if self._should_resummarize(db, event.event_id, added[0],
                                                    added[2]):
                            self._resummarize(db, job, event.event_id, added[3])
                            summaries += 1
                except Exception as exc:
                    failed += 1
                    base.log.error("ingest.event_failed", event_id=event.event_id,
                                   session_id=job["session_id"], error=repr(exc))
                continue

            first = event.message_ids[0] if event.message_ids else None
            bound = db.event_for_message(job["session_id"], first) if first else None
            if bound:
                try:
                    added = self._bind_new_messages(db, job, event.message_ids[1:], bound)
                    conflicts += added[1]
                    if added[0]:
                        resumed += 1
                        if self._should_resummarize(db, bound, added[0], added[2]):
                            self._resummarize(db, job, bound, added[3])
                            summaries += 1
                except Exception as exc:
                    failed += 1
                    base.log.error("ingest.event_failed", event_id=bound,
                                   session_id=job["session_id"], error=repr(exc))
                continue
            try:
                msgs = [self._by_id[mid] for mid in event.message_ids
                        if mid in self._by_id]
                if not msgs:
                    continue
                meta = EventMeta(event_id=0, session_id=job["session_id"],
                                 message_count=len(msgs), first_at=msgs[0]["ts"],
                                 last_at=msgs[-1]["ts"], tokens=event.tokens)
                summary = extract(meta, msgs, self._summary_factory(),
                                  existing=_existing_memories(db, msgs))
                ing = ingest_event(db, session_id=job["session_id"], summary=summary,
                                   transport=self._consolidate_factory(),
                                   origin_project=job.get("origin_project"),
                                   message_ids=event.message_ids, refresh=False)
                created += 1
                summaries += 1 if summary.ok else 0
                closed += 1
                self._index_event(db, ing.event.id)
            except Exception as exc:
                failed += 1
                base.log.error("ingest.event_failed", session_id=job["session_id"],
                               first_message=first, error=repr(exc))
                continue
        return {"created": created, "resumed": resumed, "summaries": summaries,
                "closed": closed, "conflicts": conflicts, "failed": failed}

    def _index_event(self, db, event_id: int) -> None:
        '''向量索引（失败只降级为纯词法，不影响落库）。'''
        try:
            import embed
            embed.index_event(db, event_id)
        except Exception as exc:
            base.log.warning("ingest.vec_index_failed", event_id=event_id,
                             error=repr(exc))

    def _bind_new_messages(self, db, job, message_ids, bound: int) -> tuple:
        '''
        把一批消息补绑到已存在事件（strict：不抢他属正文）。
        :param message_ids: 待绑定的消息 id 列表（调用方决定是否包含锚点）
        :return: (新增条数, 冲突条数, 新增 token 估算, 新增消息列表)
        '''
        added, conflicts, tokens, msgs = 0, 0, 0, []
        for message_id in message_ids:
            owner = db.bound_event_of(job["session_id"], message_id)
            if owner is not None and owner != bound:
                conflicts += 1
                base.log.warning("ingest.bind_conflict", session_id=job["session_id"],
                                 message_id=message_id, bound_event=owner,
                                 target_event=bound)
                continue
            if db.bind_event(job["session_id"], message_id, bound, strict=True):
                added += 1
                row = self._by_id.get(message_id)
                if row:
                    msgs.append(row)
                    tokens += estimate_tokens(row.get("content", ""))
        return added, conflicts, tokens, msgs

    def _should_resummarize(self, db, event_id: int, added: int,
                            delta_tokens: int) -> bool:
        '''
        补跑摘要的阈值：满足任一条件才重跑调用 A。
        - 新增 ≥5 条；或
        - 新增 token ≥ 事件总 token 的 20% **且** ≥50 token（小事件里一条消息
          就超过 20%，只按比例判断会让短事件每次都被重跑）；或
        - 距上次摘要 ≥24h（事件长期被续写）。
        '''
        if added >= 5:
            return True
        event = db.get_event(event_id)
        if event is None:
            return False
        total = sum(estimate_tokens(row.content)
                    for row in db.list_event_messages(event_id))
        if (delta_tokens >= MIN_RESUMMARY_TOKENS
                and total and delta_tokens >= total * 0.2):
            return True
        if event.summarized_at:
            try:
                last = datetime.fromisoformat(event.summarized_at)
            except (TypeError, ValueError):
                return False
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last).total_seconds() >= 24 * 3600:
                return True
        return False

    def _resummarize(self, db, job, event_id: int, new_messages: list) -> None:
        '''已闭合事件补摘要：更新标题/摘要/FTS，新条目按 new 落库（幂等去重）。'''
        event = db.get_event(event_id)
        if event is None:
            return
        all_msgs = [self._by_id[mid] for mid in
                    [row.message_id for row in db.list_event_messages(event_id)]
                    if mid in self._by_id]
        if not all_msgs:
            return
        meta = EventMeta(event_id=event_id, session_id=job["session_id"],
                         message_count=len(all_msgs), first_at=all_msgs[0]["ts"],
                         last_at=all_msgs[-1]["ts"],
                         tokens=sum(estimate_tokens(m.get("content", ""))
                                    for m in all_msgs))
        summary = extract(meta, all_msgs, self._summary_factory(),
                          previous_summary=event.summary or "",
                          existing=_existing_memories(db, all_msgs))
        if not summary.ok:
            base.log.warning("ingest.resummarize_failed", event_id=event_id,
                             error=summary.error)
            return
        ingest_event(db, session_id=job["session_id"], summary=summary,
                     transport=self._consolidate_factory(), event_id=event_id,
                     origin_project=job.get("origin_project"),
                     message_ids=[m["msg_id"] for m in new_messages],
                     refresh=False)
        self._index_event(db, event_id)
        db.index_fts("event", event_id, summary.title, summary.summary or "",
                     origin_project=event.origin_project,
                     session_id=job["session_id"])
        base.log.info("ingest.resummarized", event_id=event_id,
                      new_messages=len(new_messages))

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
    # 外壳会在发送前生成 job_id 并登记待确认：内核必须沿用它，
    # 否则完成帧里的 id 与外壳登记的对不上，补投对账会失效。
    job_id = (payload.get("job_id") or data.get("job_id")
              or f"job_{uuid.uuid4().hex[:8]}")
    envelope = dict(data)
    envelope["job_id"] = job_id

    accepted = bool(new_messages)
    frame.push(q, envelope, "ingest.receipt", {
        # 回执带 job_id：外壳据此把这批消息记为「待确认」，直到收到
        # ingest.completed 才从待确认队列里删（回执≠已落库）。
        "job_id": job_id,
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

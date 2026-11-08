# -*- coding: utf-8 -*-
"""维护任务：重跑事件摘要、归纳洞见（后台线程 + 进度快照）。

为什么单独一层：这两件事都要调模型（几秒到几十秒），不能占内核主循环；
但也不适合塞进 ingest worker（那会阻塞新消息写入）。做法与 `vec.reindex`
一致：起线程、写一个全局快照、界面轮询 `maintenance.status` 看进度。

快照字段约定（前端「维护」卡片直接渲染）：
    running / done / total / started_at / finished_at / elapsed_ms
    重跑摘要：ok / failed / note（最近一条失败原因）
    归纳洞见：created（新增洞见数）/ clusters / note
"""
from __future__ import annotations

import threading
import time

import base

RESUMMARIZE = {"running": False, "done": 0, "total": 0, "ok": 0, "failed": 0,
               "error": "", "note": "", "started_at": "", "finished_at": "",
               "elapsed_ms": 0}
INDUCE = {"running": False, "done": 0, "total": 0, "created": 0, "clusters": 0,
          "error": "", "note": "", "started_at": "", "finished_at": "",
          "elapsed_ms": 0}
_LOCK = threading.Lock()


def status(db=None) -> dict:
    '''两个任务的快照 + 待复核事件数（界面据此决定按钮是否可点）。'''
    snapshot = {"resummarize": dict(RESUMMARIZE), "induce": dict(INDUCE)}
    if db is not None:
        try:
            snapshot["pending_events"] = len(db.list_events_needing_review(limit=200))
        except Exception:                 # 迁移前的老库不应让状态接口报错
            snapshot["pending_events"] = 0
        try:
            snapshot["insights"] = int(db.count_l3("insight"))
        except Exception:
            snapshot["insights"] = 0
    return snapshot


def _begin(snapshot: dict, total: int, **extra) -> dict:
    '''开始一轮任务：清掉上一轮的结果字段，避免界面显示旧数字。'''
    snapshot.update({
        "running": True, "done": 0, "total": int(total),
        "error": "", "note": "",
        "started_at": base.get_cur_time(), "finished_at": "", "elapsed_ms": 0,
    })
    snapshot.update(extra)
    return dict(snapshot)


def _finish(snapshot: dict, started: float) -> None:
    snapshot["running"] = False
    snapshot["finished_at"] = base.get_cur_time()
    snapshot["elapsed_ms"] = int((time.monotonic() - started) * 1000)


def resummarize(db, *, event_ids=None, transport_factory=None,
                consolidate_factory=None, refresh_digest: bool = True) -> dict:
    '''
    重跑指定事件（或全部待复核事件）的摘要，成功后按正常链路落 l3 + 更新 FTS。

    与 ingest 的自动补跑共用同一套 `summary.extract` → `ingest.ingest_event`，
    区别只是**不看新增消息阈值**：这是人工触发，就是要现在补。
    '''
    with _LOCK:
        if RESUMMARIZE["running"]:
            return {"started": False, "reason": "already_running",
                    "state": dict(RESUMMARIZE)}
        if event_ids:
            events = [db.get_event(int(event_id)) for event_id in event_ids]
            events = [event for event in events if event is not None]
        else:
            events = db.list_events_needing_review(limit=200)
        snapshot = _begin(RESUMMARIZE, len(events), ok=0, failed=0)

    def work():
        started = time.monotonic()
        sessions: set = set()
        try:
            import embed
            import ingest
            from segmenter import estimate_tokens
            from summary import EventMeta, extract

            for event in events:
                try:
                    rows = db.list_event_messages(event.id)
                    if not rows:
                        raise RuntimeError("事件没有正文消息")
                    messages = [{
                        "msg_id": row.message_id, "seq": row.seq,
                        "role": row.role, "content": row.content,
                        "ts": row.occur_at,
                    } for row in rows]
                    meta = EventMeta(
                        event_id=event.id, session_id=event.session_id,
                        message_count=len(messages),
                        first_at=messages[0]["ts"], last_at=messages[-1]["ts"],
                        tokens=sum(estimate_tokens(item["content"])
                                   for item in messages))
                    result = extract(
                        meta, messages, transport_factory(),
                        previous_summary=event.summary or "",
                        existing=ingest._existing_memories(db, messages))
                    if not result.ok:
                        RESUMMARIZE["failed"] += 1
                        RESUMMARIZE["note"] = f"事件 #{event.id}: {result.error}"
                        continue
                    ingest.ingest_event(
                        db, session_id=event.session_id, summary=result,
                        transport=(consolidate_factory()
                                   if consolidate_factory is not None else None),
                        event_id=event.id,
                        origin_project=event.origin_project,
                        message_ids=[], refresh=False)
                    embed.index_event(db, event.id)
                    RESUMMARIZE["ok"] += 1
                    RESUMMARIZE["note"] = f"事件 #{event.id} 已重跑"
                    sessions.add(event.session_id)
                except Exception as exc:
                    RESUMMARIZE["failed"] += 1
                    RESUMMARIZE["note"] = f"事件 #{event.id}: {exc}"
                    base.log.warning("maintenance.resummarize_failed",
                                     event_id=event.id, error=repr(exc))
                finally:
                    RESUMMARIZE["done"] += 1
            if refresh_digest:
                for session_id in sessions:
                    try:
                        import ingest
                        ingest.refresh_digest(db, session_id)
                    except Exception as exc:
                        base.log.warning("maintenance.digest_failed",
                                         session_id=session_id, error=repr(exc))
        except Exception as exc:
            RESUMMARIZE["error"] = str(exc)
        finally:
            _finish(RESUMMARIZE, started)

    threading.Thread(target=work, name="resummarize", daemon=True).start()
    return {"started": True, "state": snapshot}


def induce_insights(db, *, transport_factory=None, min_support: int = 3,
                    max_clusters: int = 5, threshold=None) -> dict:
    '''跑一轮洞见归纳（两路聚类：cue + 语义相似度），进度写 INDUCE 快照。'''
    import insight

    kwargs: dict = {"min_support": int(min_support)}
    if threshold is not None:
        kwargs["threshold"] = float(threshold)

    with _LOCK:
        if INDUCE["running"]:
            return {"started": False, "reason": "already_running",
                    "state": dict(INDUCE)}
        groups, plan_error = [], ""
        try:
            groups = insight.all_clusters(db, limit=int(max_clusters), **kwargs)
        except Exception as exc:
            plan_error = str(exc)
        snapshot = _begin(INDUCE, len(groups), created=0, clusters=len(groups))
        INDUCE["error"] = plan_error

    def work():
        started = time.monotonic()
        try:
            if plan_error:
                return
            created = 0

            def progress(done, total, item):
                nonlocal created
                created += len(item.get("ids") or ())
                INDUCE["done"] = int(done)
                INDUCE["created"] = created
                if item.get("error"):
                    INDUCE["note"] = f"簇 {item.get('cluster')}: {item['error']}"

            insight.induce(db, transport_factory(), max_clusters=int(max_clusters),
                           groups=groups, progress=progress, **kwargs)
        except Exception as exc:
            INDUCE["error"] = str(exc)
        finally:
            _finish(INDUCE, started)

    threading.Thread(target=work, name="induce-insights", daemon=True).start()
    return {"started": True, "state": snapshot}

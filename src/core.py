# -*- coding: utf-8 -*-
"""核心记忆编译：系统提示词段 + 动态上下文快照。

两类自我认知分开注入（标签也分开）：
- `source=configured`（设置的自我）→ 系统提示词段 `<self_configured>`；
- `source=agent`（自己写的自我）→ 动态上下文 `<self_notes>`（user 角色快照）。

系统提示词段还带一小撮「全适用」的核心规则（洞见/经验/认知），
按 importance/use_count 排序并受 `core.max_chars` 预算约束。

标签约定（不同内容用专属标签，便于模型区分与人工审计）：
    <long_term_memory>
      <self_configured><self_item kind="identity" id="1">…</self_item></self_configured>
      <core_memory>
        <insight id="12">…</insight>
        <experience id="34">…</experience>
        <cognition id="56">…</cognition>
      </core_memory>
    </long_term_memory>

    <self_notes source="assistant" revision="7">
      <note kind="behavior" id="3">…</note>
    </self_notes>
"""
from __future__ import annotations

import hashlib
import threading
from html import escape

import base
import config
import database
import frame
import retrieval

SELF_SOURCES = ("configured", "agent")

# 「上次/之前」这类指代出现时，说明用户在回指旧事，召回放宽
STRONG_HINTS = ("上次", "之前", "昨晚", "刚才", "那天", "前天", "最后一次",
                "你忘了", "记不记得", "我们说过")

# 会话 -> {turn, seen{(kind, id): turn}}；进程内，重启即重置
_recall_state: dict = {}
_recall_lock = threading.Lock()

# 最近一次编译产出的召回条目：管理页只读它，不重算（重算会拨快冷却计数）
_last_recall = {"text": "", "items": [], "session_id": None}


def last_recall() -> dict:
    '''最近一次召回的快照（只读）。'''
    with _recall_lock:
        return {"text": _last_recall["text"],
                "items": [dict(item) for item in _last_recall["items"]],
                "session_id": _last_recall["session_id"]}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _escape(text) -> str:
    return escape(str(text or ""), quote=False)


def _self_items(rows) -> list:
    return [f'<self_item kind="{_escape(getattr(row, "kind", "identity"))}" '
            f'id="{row.id}">{_escape(row.content)}</self_item>' for row in rows]


def _core_items(rows, tag: str) -> list:
    return [f'<{tag} id="{row.id}">{_escape(row.content)}</{tag}>' for row in rows]


def _build_text(configured: list, insights: list, experiences: list,
                cognitions: list, budget: int) -> tuple:
    '''按优先级装配系统提示词段；超预算的条目整条丢弃，不截半句。'''
    self_items = _self_items(configured)
    core_items = (_core_items(insights, "insight")
                  + _core_items(experiences, "experience")
                  + _core_items(cognitions, "cognition"))

    # 先按预算裁剪：外层与分节标签也要占额度
    outer = len("<long_term_memory>\n</long_term_memory>")
    kept_self, kept_core = [], []
    used = outer + len("<self_configured>\n</self_configured>") + \
        len("<core_memory>\n</core_memory>")
    for item in self_items:
        if used + len(item) + 1 > budget:
            break
        kept_self.append(item)
        used += len(item) + 1
    for item in core_items:
        if used + len(item) + 1 > budget:
            break
        kept_core.append(item)
        used += len(item) + 1

    blocks = []
    if kept_self:
        blocks.append("<self_configured>\n" + "\n".join(kept_self) + "\n</self_configured>")
    if kept_core:
        blocks.append("<core_memory>\n" + "\n".join(kept_core) + "\n</core_memory>")
    text = ""
    if blocks:
        text = "<long_term_memory>\n" + "\n".join(blocks) + "\n</long_term_memory>"
    return text, {"self": len(kept_self), "insight": len(insights),
                  "experience": len(experiences), "cognition": len(cognitions)}


def _notes_text(rows) -> str:
    if not rows:
        return ""
    revision = max(int(getattr(row, "revision", 1) or 1) for row in rows)
    items = [f'<note kind="{_escape(row.kind)}" id="{row.id}">'
             f"{_escape(row.content)}</note>" for row in rows]
    return (f'<self_notes source="assistant" revision="{revision}">\n'
            + "\n".join(items) + "\n</self_notes>")


def _recent_query(db, session_id: str | None, cfg: dict) -> str:
    '''用最近几条消息拼检索语句：被动回想的输入就是「现在在聊什么」。'''
    if not session_id:
        return ""
    messages = db.recent_messages(session_id, int(cfg.get("query_messages", 4)))
    text = " ".join((message.content or "").strip() for message in messages)
    return text[:int(cfg.get("query_chars", 500))]


def _commit_recall(session_id: str | None, text: str, items: list) -> None:
    '''每次编译都覆盖快照：空召回也要写空，避免界面显示上一轮的结果。'''
    with _recall_lock:
        _last_recall["text"] = text
        _last_recall["items"] = list(items)
        _last_recall["session_id"] = session_id


def _build_recall(db, session_id: str | None, query_hint: str | None,
                  advance_turn: bool = True,
                  query_vector: list | None = None) -> dict:
    '''
    被动回想：按当前对话捞几条相关记忆，组装 <recall> 块。

    五条硬约束：
    - `touch=False`：注入不算「被使用」，否则每轮都加热度，越召回越热；
    - 冷却：同一条记忆在同一会话里 N 轮内不重复注入，避免看第二遍就当背景；
    - **必须绑定会话**：没有 session_id 就不召回。旧实现退回「最近有消息的
      会话」，多会话场景会把别的会话的记忆注入当前提示词（0.5.1 修）；
    - **只有用户步骤推进冷却**：后台刷新（写入完成/设置变更）只重编核心文本，
      调 `advance_turn=False` 时既不推进轮次、也不改快照（0.5.2 修）；
    - **有向量就用**：只靠字面命中时，换个说法提旧事就召不回（0.5.3 修）。
    '''
    cfg = config.get_config().get("recall", {}) or {}
    session_id = (session_id or "").strip() or None

    def finish(text: str, items: list, skipped: bool = False) -> dict:
        if not skipped:
            _commit_recall(session_id, text, items)
        return {"text": text, "items": items, "skipped": skipped}

    if not cfg.get("enabled", True):
        return finish("", [])
    if not session_id:
        return finish("", [])
    if not advance_turn:
        return finish("", [], skipped=True)

    query = (query_hint or "").strip() or _recent_query(db, session_id, cfg)
    query = query[:int(cfg.get("query_chars", 500))]
    if not query:
        return finish("", [])

    limit = int(cfg.get("max_items", 2))
    if any(word in query for word in STRONG_HINTS):
        limit = int(cfg.get("max_items_strong", 5))
    if limit <= 0:
        return finish("", [])
    min_score = float(cfg.get("min_score", 0.55))
    cooldown = int(cfg.get("same_id_cooldown", 10))

    key = session_id
    with _recall_lock:
        if key not in _recall_state and len(_recall_state) >= 200:
            # 长期运行：会话状态有上限，淘汰轮次最旧的会话
            oldest = min(_recall_state, key=lambda name: _recall_state[name]["turn"])
            _recall_state.pop(oldest, None)
        state = _recall_state.setdefault(key, {"turn": 0, "seen": {}})
        state["turn"] += 1
        turn = state["turn"]

    try:
        hits = retrieval.search(db, query, limit=limit, scope="all",
                                touch=False, query_vector=query_vector)
    except Exception as exc:                      # 召回失败不能拖垮上下文编译
        base.log.warning("recall.search_failed", error=type(exc).__name__)
        return finish("", [])

    # 先按分数取前 limit 条，再滤冷却：宁可不注入，也不拿低分条目硬凑
    candidates = [hit for hit in hits if hit.score >= min_score][:limit]
    picked = []
    with _recall_lock:
        state = _recall_state.setdefault(key, {"turn": turn, "seen": {}})
        for hit in candidates:
            mark = (hit.kind, hit.id)
            last = state["seen"].get(mark)
            if last is not None and turn - last < cooldown:
                continue
            state["seen"][mark] = turn
            picked.append(hit)
        if len(state["seen"]) > 500:              # 防止长期运行无限增长
            cutoff = turn - cooldown
            state["seen"] = {k: v for k, v in state["seen"].items() if v > cutoff}

    if not picked:
        return finish("", [])

    lines = ['<recall note="回忆，可能过时；与现场证据冲突时以现场为准">']
    items = []
    for hit in picked:
        stamp = str(hit.meta.get("created_at") or "")[:10]
        # 注入的是线索不是全文：每条截断，避免一条长摘要吃掉上下文
        brief = (hit.text or "").strip().replace("\n", " ")[:160]
        lines.append(f'  <item kind="{_escape(hit.kind)}" id="{hit.id}" '
                     f'ts="{stamp}">{_escape(brief)}</item>')
        items.append({"kind": hit.kind, "id": hit.id, "text": brief,
                      "score": round(hit.score, 4), "created_at": stamp})
    lines.append("</recall>")
    return finish("\n".join(lines), items)


def compile_core(db: database.Database, cfg: dict | None = None,
                 session_id: str | None = None,
                 query_hint: str | None = None,
                 advance_turn: bool = True,
                 use_recall: bool = True,
                 query_vector: list | None = None) -> dict:
    '''
    编译注入文本；返回核心块、自述与召回。

    :param advance_turn: 是否推进召回冷却轮次。只有用户步骤（pre-step）为 True；
                         后台刷新传 False，避免 9 次写入就把 10 轮冷却耗光。
    :param use_recall: 是否编译召回。后台刷新只需核心文本，传 False 时不动召回快照。
    :param query_vector: 当前输入的向量（外壳算好随帧传）；有它召回才走向量通道。
    '''
    core_cfg = cfg if cfg is not None else config.get_config().get("core", {})
    enabled = bool(core_cfg.get("enabled", True))
    if not enabled:
        return {"enabled": False, "version": "", "text": "",
                "notes_version": "", "notes_text": "",
                "recall_version": "", "recall_text": "", "recall_items": [],
                "recall_skipped": True,
                "stats": {"self_configured": 0, "self_notes": 0,
                          "insight": 0, "experience": 0, "cognition": 0,
                          "recall": 0}}

    budget = int(core_cfg.get("max_chars", 2000))
    configured = db.list_self_memory(
        source="configured", limit=int(core_cfg.get("max_self_configured", 20)))
    notes = db.list_self_memory(
        source="agent", limit=int(core_cfg.get("max_self_notes", 10)))

    insights = [row for row in db.list_insights(limit=200)
                if getattr(row, "scope", "global") == "global"
                and int(row.importance or 0) >= 4]
    insights.sort(key=lambda row: (-int(row.importance or 0),
                                   -int(row.use_count or 0), -row.id))
    insights = insights[:int(core_cfg.get("max_insights", 6))]

    experiences = [row for row in db.list_experiences(limit=500)
                   if getattr(row, "scope", "project") == "global"
                   and int(row.importance or 0) >= 5]
    experiences.sort(key=lambda row: (-int(row.importance or 0),
                                      -int(row.use_count or 0), -row.id))
    experiences = experiences[:int(core_cfg.get("max_experiences", 4))]

    cognitions = [row for row in db.list_cognitions(limit=500)
                  if row.origin_project is None
                  and int(row.importance or 0) >= 5
                  and str(row.subject or "") not in ("助手",)]
    cognitions.sort(key=lambda row: (-int(row.importance or 0),
                                     -int(row.use_count or 0), -row.id))
    cognitions = cognitions[:int(core_cfg.get("max_cognitions", 4))]

    text, stats = _build_text(configured, insights, experiences, cognitions, budget)
    notes_text = _notes_text(notes)
    if use_recall:
        recall = _build_recall(db, session_id, query_hint,
                               advance_turn=advance_turn,
                               query_vector=query_vector)
    else:
        recall = {"text": "", "items": [], "skipped": True}
    return {
        "enabled": True,
        "version": _hash(text) if text else "",
        "text": text,
        "notes_version": _hash(notes_text) if notes_text else "",
        "notes_text": notes_text,
        "recall_version": _hash(recall["text"]) if recall["text"] else "",
        "recall_text": recall["text"],
        "recall_items": recall["items"],
        "recall_skipped": bool(recall.get("skipped")),
        "stats": {"self_configured": stats["self"], "self_notes": len(notes),
                  "insight": stats["insight"], "experience": stats["experience"],
                  "cognition": stats["cognition"],
                  "recall": len(recall["items"])},
    }


def get_core(data, q: base.message_queue, db=None):
    '''协议入口：返回核心记忆与自述快照；召回按需编译（advance_turn 控制冷却）。

    业务参数在**帧的 `data`** 里（与 tools.calls / admin / ingest.request 一致），
    只有 `request_id`/`session_id`/`agent_id` 在信封顶层。外壳用
    `kernel.request('core.get', {query_hint, recall, advance_turn}, {sessionId})`
    发帧，所以 session_id 取 data 优先、信封兜底——只读顶层会让 query_hint 与
    两个开关全部落回默认值（0.5.3 修）。
    '''
    if db is None:
        import tools                                # 复用共享连接（勿关闭）
        db = tools._db()
    payload = data.get("data") or {}
    result = compile_core(
        db, session_id=payload.get("session_id") or data.get("session_id"),
        query_hint=payload.get("query_hint"),
        advance_turn=bool(payload.get("advance_turn", True)),
        use_recall=bool(payload.get("recall", True)),
        query_vector=payload.get("query_vector"))
    frame.push(q, data, "core", result)

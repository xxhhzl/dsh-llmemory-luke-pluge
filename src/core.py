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
from html import escape

import base
import config
import database
import frame

SELF_SOURCES = ("configured", "agent")


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


def compile_core(db: database.Database, cfg: dict | None = None) -> dict:
    '''编译注入文本；返回 {enabled, version, text, notes_version, notes_text, stats}。'''
    core_cfg = cfg if cfg is not None else config.get_config().get("core", {})
    enabled = bool(core_cfg.get("enabled", True))
    if not enabled:
        return {"enabled": False, "version": "", "text": "",
                "notes_version": "", "notes_text": "",
                "stats": {"self_configured": 0, "self_notes": 0,
                          "insight": 0, "experience": 0, "cognition": 0}}

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
    return {
        "enabled": True,
        "version": _hash(text) if text else "",
        "text": text,
        "notes_version": _hash(notes_text) if notes_text else "",
        "notes_text": notes_text,
        "stats": {"self_configured": stats["self"], "self_notes": len(notes),
                  "insight": stats["insight"], "experience": stats["experience"],
                  "cognition": stats["cognition"]},
    }


def get_core(data, q: base.message_queue, db=None):
    '''协议入口：返回当前核心记忆与自述快照（外壳注册 section/context 用）。'''
    if db is None:
        import tools                                # 复用共享连接（勿关闭）
        db = tools._db()
    payload = compile_core(db)
    frame.push(q, data, "core", payload)

# -*- coding: utf-8 -*-
"""管理 API：给「设置 → 长期记忆」页面用的增删查改。

设计边界（重要）：
- **模型工具仍只追加**（memory_search/get/recent 无写入口，见 docs/tools.md §7）；
  这里是**人工管理入口**，只经由本地回环 HTTP 暴露给同一个用户。
- 表结构驱动的通用接口：`tables` 返回列/可编辑列，前端据此渲染表单，
  新增表时不需要改前端。
- 删除会同步清理全文索引与引用（否则检索层会拿到悬空行）。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import base
import config
import database
import frame
import json as _json

MAX_LIMIT = 200

# 列表页只回前若干字符：一条消息可能 70 KB，50 行就是上百 KB，
# 会把设置页卡住。编辑时前端再单独 get 一行完整内容。
_LIST_CLIP = {"content": 200, "summary": 300, "context": 120}

# 表 -> (主键列, 可写列)；顺序即前端展示顺序
TABLES = {
    "event": ("id", ["session_id", "title", "summary", "origin_project",
                     "needs_review", "review_reason"]),
    "message": ("msg_key", ["session_id", "message_id", "owner", "seq", "role",
                            "content", "occur_at"]),
    "memory": ("msg_key", ["session_id", "message_id", "owner", "seq",
                           "occur_at", "event_id"]),
    "experience": ("id", ["context", "content", "kind", "confidence", "importance",
                          "origin_session", "origin_project", "source_event_id",
                          "cues", "status", "invalid_reason"]),
    "cognition": ("id", ["subject", "content", "kind", "confidence", "importance",
                         "origin_session", "origin_project", "source_event_id",
                         "cues", "status", "invalid_reason"]),
    "digest": ("session_id", ["session_id", "content"]),
}

# message / memory 的主键是复合键，管理界面用 rowid 定位
_ROWID_TABLES = {"message", "memory"}

# 主键列与 seq 不允许在「编辑」里改：改了会让 message 与 memory 的 join 断开、
# 或撞 UNIQUE(session_id, seq)。写入新行时仍可用（insert 走另一份白名单）。
_KEY_COLUMNS = {
    "message": {"session_id", "message_id", "seq"},
    "memory": {"session_id", "message_id", "seq"},
    "digest": {"session_id"},
}

UPDATABLE = {
    name: [column for column in writable if column not in _KEY_COLUMNS.get(name, ())]
    for name, (_pk, writable) in TABLES.items()
}


class AdminError(Exception):
    '''参数不合法（由 admin 处理器转成 error 帧）。'''


def _db() -> database.Database:
    '''复用 tools 层的连接缓存：每次请求新建连接会重放全量 DDL 与迁移。'''
    import tools          # 延迟导入：tools 依赖 database，避免模块级环
    return tools._db()


def _table(name: str) -> str:
    if name not in TABLES:
        raise AdminError(f"不支持的表: {name!r}")
    return name


def _columns(conn, table: str) -> list[dict]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    result = []
    for row in rows:
        result.append({
            "name": row["name"],
            "type": row["type"] or "",
            "notnull": bool(row["notnull"]),
            "default": row["dflt_value"],
            "pk": bool(row["pk"]),
        })
    return result


def _escape_like(text: str) -> str:
    '''LIKE 里的 % 和 _ 是通配符：用户搜 "50%" 不该匹配全部。'''
    return (text.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_"))


def _search_columns(conn, table: str) -> list[str]:
    cols = {row["name"] for row in _columns(conn, table)}
    wanted = ["title", "summary", "content", "context", "subject",
              "message_id", "session_id", "review_reason"]
    return [name for name in wanted if name in cols]


def list_rows(db, *, table: str, limit: int = 50, offset: int = 0,
              query: str = "", order: str = "desc") -> dict:
    table = _table(table)
    limit = max(1, min(int(limit or 50), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    direction = "ASC" if str(order).lower() == "asc" else "DESC"

    conn = db._conn
    where, params = "", []
    text = str(query or "").strip()
    if text:
        cols = _search_columns(conn, table)
        if not cols:
            raise AdminError(f"{table} 没有可搜索的文本列")
        where = " WHERE " + " OR ".join(f"{name} LIKE ? ESCAPE '\\'" for name in cols)
        params = [f"%{_escape_like(text)}%"] * len(cols)

    pk = "rowid" if table in _ROWID_TABLES else TABLES[table][0]
    total = conn.execute(f"SELECT COUNT(*) AS n FROM {table}{where}",
                         params).fetchone()["n"]
    select = ", ".join(
        f"substr({column['name']}, 1, {_LIST_CLIP[column['name']]}) AS {column['name']}"
        if column["name"] in _LIST_CLIP else column["name"]
        for column in _columns(conn, table))
    rows = conn.execute(
        f"SELECT rowid AS _rowid, {select} FROM {table}{where} "
        f"ORDER BY {pk} {direction} LIMIT ? OFFSET ?",
        [*params, limit, offset]).fetchall()
    return {"table": table, "total": int(total), "limit": limit,
            "offset": offset, "rows": [dict(row) for row in rows]}


def get_row(db, *, table: str, row_id) -> dict:
    table = _table(table)
    conn = db._conn
    if table in _ROWID_TABLES:
        row = conn.execute(f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid = ?",
                           (int(row_id),)).fetchone()
    else:
        pk = TABLES[table][0]
        row = conn.execute(f"SELECT rowid AS _rowid, * FROM {table} WHERE {pk} = ?",
                           (row_id,)).fetchone()
    if row is None:
        raise AdminError(f"{table} 中没有 {row_id}")
    return dict(row)


def _normalize_value(table: str, column: str, value):
    if column == "cues":
        if isinstance(value, str):
            try:
                value = _json.loads(value)
            except ValueError:
                value = [part.strip() for part in value.split(",") if part.strip()]
        return _json.dumps(list(value or []), ensure_ascii=False) if value else None
    if column in ("needs_review",):
        return 1 if value in (True, 1, "1", "true") else 0
    if column in ("importance", "seq", "event_id", "source_event_id"):
        return None if value in (None, "") else int(value)
    if column in ("confidence",):
        return None if value in (None, "") else float(value)
    if isinstance(value, (dict, list)):
        return _json.dumps(value, ensure_ascii=False)
    return value


def _required_columns(conn, table: str) -> set:
    '''必填列：NOT NULL、无默认值、非主键（主键由自增或调用方负责）。'''
    return {row["name"] for row in _columns(conn, table)
            if row["notnull"] and row["default"] is None and not row["pk"]}


def _friendly_sqlite_error(exc: sqlite3.Error) -> str:
    '''把 SQLite 的约束错误翻成用户能照着改的话。'''
    text = str(exc)
    if "NOT NULL constraint failed" in text:
        column = text.rsplit(".", 1)[-1].strip()
        return f"{column} 必填（不能为空）"
    if "UNIQUE constraint failed" in text:
        return f"唯一约束冲突：{text.split(':', 1)[-1].strip()}"
    if "FOREIGN KEY constraint failed" in text:
        return "外键不存在：请先创建被引用的行"
    return text


def insert_row(db, *, table: str, values: dict) -> dict:
    table = _table(table)
    allowed = TABLES[table][1]
    raw = {k: v for k, v in (values or {}).items() if k in allowed}
    # 空串/未填的列直接不写，交给列默认值——UI 把所有可写列都提交一遍，
    # 若原样写 NULL 会撞 NOT NULL（confidence / importance / seq / source_event_id）。
    required = _required_columns(db._conn, table)
    data: dict = {}
    for key, value in raw.items():
        normalized = _normalize_value(table, key, value)
        if normalized is None:
            continue
        if (key in required and isinstance(normalized, str)
                and not normalized.strip()):
            continue      # 必填列的空串按“没填”处理，让 NOT NULL 报出列名
        data[key] = normalized
    now = base.get_cur_time()
    if table == "digest":
        # digest 是覆盖缓存：走 upsert，保留 revision 语义与旧的溯源
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            raise AdminError("session_id 必填")
        existing = db.get_digest(session_id)
        revision = db.upsert_digest(
            session_id, str(data.get("content") or ""),
            ref_event_ids=existing.ref_event_ids if existing else None)
        return {"table": "digest", "id": session_id, "revision": revision}
    # 先补默认值再判空：这样“只填了必填列且是空串”会走到 SQL，
    # 由 NOT NULL 报出具体列名（比“没有可写入的字段”有用得多）。
    data.setdefault("created_at", now)
    if table in ("experience", "cognition"):
        # l3 的 source_event_id 是 NOT NULL；人工写入没有来源事件，用 0 作哨兵
        data.setdefault("source_event_id", 0)
    if not data:
        raise AdminError("没有可写入的字段（全部为空）")
    conn = db._conn
    columns = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    try:
        cur = conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                           list(data.values()))
    except sqlite3.IntegrityError as exc:
        raise AdminError(_friendly_sqlite_error(exc)) from exc
    except sqlite3.Error as exc:
        raise AdminError(f"写入失败: {exc}") from exc
    conn.commit()
    new_id = cur.lastrowid
    if table in ("experience", "cognition"):
        content = data.get("content") or ""
        db.index_fts(table, new_id, "", content,
                     origin_project=data.get("origin_project"),
                     session_id=data.get("origin_session"))
    elif table == "event":
        db.index_fts("event", new_id, data.get("title") or "",
                     data.get("summary") or "",
                     origin_project=data.get("origin_project"),
                     session_id=data.get("session_id"))
    return {"table": table, "id": new_id}


def update_row(db, *, table: str, row_id, values: dict) -> dict:
    table = _table(table)
    allowed = set(UPDATABLE[table])
    data = {k: v for k, v in (values or {}).items() if k in allowed}
    if not data:
        raise AdminError("没有可更新的字段")

    conn = db._conn
    if table == "digest":
        # 工作记忆是覆盖缓存：走 upsert 才能 bump revision / updated_at，
        # 且必须把旧的 ref_event_ids 带回去，否则保存一次内容就把溯源清空。
        session_id = str(row_id)
        existing = db.get_digest(session_id)
        if existing is None:
            raise AdminError(f"digest 中没有 {session_id}")
        content = str(data.get("content", existing.content) or "")
        revision = db.upsert_digest(session_id, content,
                                    ref_event_ids=existing.ref_event_ids)
        return {"table": "digest", "id": session_id, "revision": revision,
                "updated": len(data)}

    pk = "rowid" if table in _ROWID_TABLES else TABLES[table][0]
    assignments = ", ".join(f"{name} = ?" for name in data)
    params = [_normalize_value(table, k, v) for k, v in data.items()]
    try:
        cur = conn.execute(f"UPDATE {table} SET {assignments} WHERE {pk} = ?",
                           [*params, row_id])
    except sqlite3.Error as exc:
        raise AdminError(f"更新失败: {exc}") from exc
    if cur.rowcount != 1:
        raise AdminError(f"{table} 中没有 {row_id}")
    conn.commit()

    # 文本变化要同步全文索引，否则检索仍是旧内容
    if table == "event":
        row = conn.execute("SELECT title, summary, origin_project, session_id "
                           "FROM event WHERE id = ?", (row_id,)).fetchone()
        db.index_fts("event", int(row_id), row["title"], row["summary"] or "",
                     origin_project=row["origin_project"],
                     session_id=row["session_id"])
    elif table in ("experience", "cognition"):
        row = conn.execute(f"SELECT content, origin_project, origin_session, status "
                           f"FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if row["status"] == "active":
            db.index_fts(table, int(row_id), "", row["content"],
                         origin_project=row["origin_project"],
                         session_id=row["origin_session"])
        else:
            # 手工置为 invalid/superseded：索引必须跟着摘掉，否则失效条目
            # 仍会占满召回窗口再被 _eligible 丢弃。
            conn.execute("DELETE FROM memory_fts WHERE ref_kind = ? AND ref_id = ?",
                         (table, int(row_id)))
            conn.commit()
    return {"table": table, "id": row_id, "updated": len(data)}


def _strip_id_from_json(raw, event_id: int):
    '''从 JSON 整数数组里剔除某个 id；没有变化返回 None。'''
    if not raw:
        return None
    try:
        items = _json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list):
        return None
    kept, changed = [], False
    for item in items:
        try:
            same = int(item) == event_id
        except (TypeError, ValueError):
            same = False
        if same:
            changed = True
        else:
            kept.append(item)
    return _json.dumps(kept, ensure_ascii=False) if changed else None


def delete_row(db, *, table: str, row_id) -> dict:
    table = _table(table)
    conn = db._conn
    pk = "rowid" if table in _ROWID_TABLES else TABLES[table][0]

    if table == "event":
        # 事件删除：解绑消息、清理索引，再删行（否则检索会拿到悬空事件）
        event_id = int(row_id)
        conn.execute("UPDATE memory SET event_id = NULL WHERE event_id = ?", (event_id,))
        conn.execute("DELETE FROM memory_fts WHERE ref_kind = 'event' AND ref_id = ?",
                     (event_id,))
        conn.execute("UPDATE event SET prev_event_id = NULL WHERE prev_event_id = ?",
                     (event_id,))
        conn.execute("UPDATE event SET next_event_id = NULL WHERE next_event_id = ?",
                     (event_id,))
        # 指向该事件的其他引用一并收干净：l3 的“凭什么”、digest 的溯源、
        # 别的事件的 related_events。event.id 没有 AUTOINCREMENT 会被复用，
        # 留着旧锚点等于把旧记忆静默挂到新事件上。
        for l3 in ("experience", "cognition"):
            conn.execute(
                f"UPDATE {l3} SET source_event_id = 0 WHERE source_event_id = ?",
                (event_id,))
        for row in conn.execute(
                "SELECT session_id, ref_event_ids FROM digest "
                "WHERE ref_event_ids LIKE ?", (f"%{event_id}%",)).fetchall():
            stripped = _strip_id_from_json(row["ref_event_ids"], event_id)
            if stripped is not None:
                conn.execute(
                    "UPDATE digest SET ref_event_ids = ? WHERE session_id = ?",
                    (stripped, row["session_id"]))
        for row in conn.execute(
                "SELECT id, related_events FROM event WHERE related_events LIKE ? "
                "AND id != ?", (f"%{event_id}%", event_id)).fetchall():
            stripped = _strip_id_from_json(row["related_events"], event_id)
            if stripped is not None:
                conn.execute("UPDATE event SET related_events = ? WHERE id = ?",
                             (stripped, row["id"]))
    elif table == "message":
        row = conn.execute("SELECT session_id, message_id FROM message WHERE rowid = ?",
                           (int(row_id),)).fetchone()
        if row is None:
            raise AdminError(f"message 中没有 {row_id}")
        conn.execute("DELETE FROM memory WHERE session_id = ? AND message_id = ?",
                     (row["session_id"], row["message_id"]))
    elif table in ("experience", "cognition"):
        conn.execute(f"DELETE FROM memory_fts WHERE ref_kind = '{table}' AND ref_id = ?",
                     (int(row_id),))

    try:
        cur = conn.execute(f"DELETE FROM {table} WHERE {pk} = ?", (row_id,))
    except sqlite3.Error as exc:
        raise AdminError(f"删除失败: {exc}") from exc
    if cur.rowcount != 1:
        raise AdminError(f"{table} 中没有 {row_id}")
    conn.commit()
    return {"table": table, "id": row_id, "deleted": True}


def describe(db) -> dict:
    conn = db._conn
    tables = []
    for name, (pk, writable) in TABLES.items():
        tables.append({
            "name": name,
            "pk": pk,
            "writable": writable,
            "updatable": UPDATABLE[name],
            "columns": _columns(conn, name),
        })
    return {"tables": tables, "db_path": str(db.path)}


_OPS = {
    "tables": lambda db, data: describe(db),
    "list": lambda db, data: list_rows(db, table=data.get("table", ""),
                                        limit=data.get("limit", 50),
                                        offset=data.get("offset", 0),
                                        query=data.get("query", ""),
                                        order=data.get("order", "desc")),
    "get": lambda db, data: get_row(db, table=data.get("table", ""),
                                    row_id=data.get("id")),
    "insert": lambda db, data: insert_row(db, table=data.get("table", ""),
                                          values=data.get("values") or {}),
    "update": lambda db, data: update_row(db, table=data.get("table", ""),
                                          row_id=data.get("id"),
                                          values=data.get("values") or {}),
    "delete": lambda db, data: delete_row(db, table=data.get("table", ""),
                                          row_id=data.get("id")),
}


def admin(data, q: base.message_queue):
    '''管理入口：data 里带 op + 参数；结果以 admin 帧返回。'''
    payload = data.get("data") or {}
    op = str(payload.get("op") or "").strip()
    handler = _OPS.get(op)
    if handler is None:
        frame.push(q, data, "admin",
                   {"ok": False, "error": f"不支持的 op: {op!r}"})
        return
    db = _db()
    try:
        result = handler(db, payload)
        frame.push(q, data, "admin", {"ok": True, "op": op, "result": result})
    except AdminError as exc:
        frame.push(q, data, "admin", {"ok": False, "op": op, "error": str(exc)})
    except Exception as exc:      # 兜底：管理界面不能把内核打挂
        base.log.error("admin.failed", op=op, error=repr(exc))
        frame.push(q, data, "admin",
                   {"ok": False, "op": op, "error": type(exc).__name__})

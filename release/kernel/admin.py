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
import threading
import time
from datetime import datetime, timezone

import base
import config
import core
import database
import frame
import maintenance
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
    "experience": ("id", ["context", "content", "kind", "scope", "confidence",
                          "importance", "origin_session", "origin_project",
                          "source_event_id", "cues", "status", "invalid_reason"]),
    "cognition": ("id", ["subject", "content", "kind", "confidence", "importance",
                         "origin_session", "origin_project", "source_event_id",
                         "cues", "status", "invalid_reason"]),
    "insight": ("id", ["content", "scope", "confidence", "importance",
                       "origin_session", "origin_project", "source_experience_ids",
                       "cues", "status", "invalid_reason"]),
    "self_memory": ("id", ["source", "kind", "content", "revision",
                           "origin_session", "status", "invalid_reason"]),
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


SELF_KINDS = ("identity", "behavior", "limitation", "goal")
SELF_MAX_CHARS = 500


def _self_dict(row) -> dict:
    return {"id": row.id, "source": row.source, "kind": row.kind,
            "content": row.content, "revision": row.revision,
            "origin_session": row.origin_session, "status": row.status,
            "created_at": row.created_at}


def self_list(db, data: dict) -> dict:
    '''列自我认知；source 省略表示两类都列。'''
    source = str(data.get("source") or "").strip() or None
    if source not in (None, "configured", "agent"):
        raise AdminError("source 只能是 configured / agent")
    rows = db.list_self_memory(source=source, limit=int(data.get("limit", 50)))
    return {"items": [_self_dict(row) for row in rows]}


def self_set(db, data: dict) -> dict:
    '''
    用户写「设置的自我」（只接受 source=configured）。

    同一 kind 已有 active 行时：旧行 superseded，新行 revision+1（只追加）。
    模型自述（source=agent）走工具 `memory_self_write`，不从这里写。
    '''
    source = str(data.get("source") or "configured").strip()
    if source != "configured":
        raise AdminError("用户接口只能写 source=configured；agent 由模型工具写")
    kind = str(data.get("kind") or "identity").strip()
    if kind not in SELF_KINDS:
        raise AdminError(f"kind 只能是 {', '.join(SELF_KINDS)}")
    content = str(data.get("content") or "").strip()
    if not content:
        raise AdminError("content 不能为空")
    if len(content) > SELF_MAX_CHARS:
        raise AdminError(f"content 超过 {SELF_MAX_CHARS} 字")

    limit = int(config.get_config().get("core", {}).get("max_self_configured", 20))
    active = db.list_self_memory(source="configured", limit=1000)
    same_kind = [row for row in active if row.kind == kind]
    if not same_kind and len(active) >= limit:
        raise AdminError(f"自我设定已达上限 {limit} 条，请先停用旧条目")
    revision = max([int(row.revision or 1) for row in same_kind], default=0) + 1
    now = base.get_cur_time()
    new_id = db.insert_self_memory(database.SelfMemory(
        source="configured", kind=kind, content=content, revision=revision,
        origin_session=str(data.get("session_id") or "") or None,
        created_at=now))
    for row in same_kind:
        db._conn.execute(
            "UPDATE self_memory SET status = 'superseded', valid_to = ?, "
            "superseded_id = ? WHERE id = ?", (now, new_id, row.id))
    db._conn.commit()
    return {"id": new_id, "kind": kind, "revision": revision,
            "superseded": [row.id for row in same_kind]}


def self_disable(db, data: dict) -> dict:
    '''停用一条自我认知（configured / agent 都可，用户有权停用）。'''
    row_id = int(data.get("id") or 0)
    reason = str(data.get("reason") or "用户停用")[:120]
    cur = db._conn.execute(
        "UPDATE self_memory SET status = 'invalid', invalid_reason = ?, valid_to = ? "
        "WHERE id = ? AND status = 'active'",
        (reason, base.get_cur_time(), row_id))
    db._conn.commit()
    if cur.rowcount != 1:
        raise AdminError(f"self_memory 中没有 active 的 {row_id}")
    return {"id": row_id, "status": "invalid", "reason": reason}


SETTING_KEYS = ("model.jev", "model.extract", "embedding")
_SECRET_FIELDS = ("apiKey", "key")


def _mask_setting(value):
    '''读取设置时隐藏 key，只留尾 4 位。'''
    if not isinstance(value, dict):
        return value
    masked = dict(value)
    for field in _SECRET_FIELDS:
        secret = masked.get(field)
        if isinstance(secret, str) and secret:
            masked[field] = f"…{secret[-4:]}" if len(secret) > 4 else "…"
    return masked


def settings_get(db, data: dict) -> dict:
    stored = db.list_settings()
    items = {}
    for key in SETTING_KEYS:
        value = stored.get(key)
        if value is not None:
            items[key] = _mask_setting(value)
    return {"items": items, "keys": list(SETTING_KEYS)}


def settings_raw(data, q):
    '''外壳专用：返回未脱敏设置（key 只在宿主进程间传递，不经 HTTP）。'''
    import frame as _frame
    _frame.push(q, data, "settings.raw", {"items": _db().list_settings()})


def settings_set(db, data: dict) -> dict:
    key = str(data.get("key") or "").strip()
    if key not in SETTING_KEYS:
        raise AdminError(f"未知设置项: {key!r}")
    value = data.get("value")
    if not isinstance(value, dict):
        raise AdminError("value 必须是对象")
    old = db.get_setting(key) or {}
    merged = {**(old if isinstance(old, dict) else {}), **value}
    # UI 只能拿到掩码 key：空值或掩码值表示“保持原 key”
    secret = str(value.get("apiKey") or "")
    if (not secret or secret.startswith("…")) and isinstance(old, dict) and old.get("apiKey"):
        merged["apiKey"] = old["apiKey"]
    db.set_setting(key, merged)
    return {"key": key, "value": _mask_setting(merged)}


_REINDEX = {"running": False, "done": 0, "total": 0, "error": "",
            "started_at": "", "finished_at": "", "elapsed_ms": 0}
_REINDEX_LOCK = threading.Lock()


def vec_status(db, data: dict) -> dict:
    import ann
    return {**db.vec_status(), "reindex": dict(_REINDEX),
            "ann": ann.get_store().status(db)}


def vec_reindex(db, data: dict) -> dict:
    '''
    后台重建向量索引（sqlite-vec + 外壳 embedding）。

    UI 靠 `reindex` 快照画进度：点下去立刻是 running，轮询到 running=False
    才算结束。已经跑着的时候把当前快照一起返回，前端才不会误判成「没开始」
    而把「重建中」收掉。
    '''
    with _REINDEX_LOCK:
        if _REINDEX["running"]:
            return {"started": False, "reason": "already_running",
                    "state": dict(_REINDEX)}
        _REINDEX.update({"running": True, "done": 0, "total": 0, "error": "",
                         "started_at": base.get_cur_time(), "finished_at": "",
                         "elapsed_ms": 0})
        snapshot = dict(_REINDEX)

    def work():
        started = time.monotonic()
        try:
            import embed
            if not getattr(db, "_vec_enabled", False):
                _REINDEX["error"] = "向量索引不可用（缺 sqlite-vec 扩展或向量表未建）"
                return
            rows = []
            for kind, loader in (
                    ("experience", lambda: db.list_experiences(limit=100000)),
                    ("cognition", lambda: db.list_cognitions(limit=100000)),
                    ("insight", lambda: db.list_insights(limit=100000))):
                for row in loader():
                    if getattr(row, "status", "active") == "active":
                        rows.append((kind, row))
            _REINDEX["total"] = len(rows)

            failures = 0
            written = 0

            def note_error(exc):
                nonlocal failures
                failures += 1
                if not _REINDEX["error"]:
                    _REINDEX["error"] = str(exc) or "embedding 调用失败"

            for index in range(0, len(rows), 16):
                batch = rows[index:index + 16]
                written += embed.index_rows(db, batch, on_error=note_error)
                _REINDEX["done"] = min(len(rows), index + len(batch))
                # 端点没起来时每批都会秒失败：连续两批写不进任何向量就收手，
                # 免得「假装重建成功」（实测：Ollama 未启动时 87 条 <0.2s 全绿）
                if written == 0 and failures >= 2:
                    _REINDEX["error"] = (
                        f"embedding 没有写入任何向量，已中止：{_REINDEX['error']}")
                    break
            if rows and written == 0 and not _REINDEX["error"]:
                _REINDEX["error"] = "embedding 没有写入任何向量（检查端点配置）"
        except Exception as exc:
            _REINDEX["error"] = str(exc)
        finally:
            _REINDEX["running"] = False
            _REINDEX["finished_at"] = base.get_cur_time()
            _REINDEX["elapsed_ms"] = int((time.monotonic() - started) * 1000)

    threading.Thread(target=work, name="vec-reindex", daemon=True).start()
    return {"started": True, "state": snapshot}


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


# ---- 维护任务（重跑摘要 / 归纳洞见）----

def _shell_transport(purpose: str):
    '''模型调用一律走外壳代理：内核只发 llm.request，不持有凭据。'''
    import summary
    return summary.ShellSummaryTransport(purpose=purpose)


def _summary_transport():
    return _shell_transport("summary")


def _consolidate_transport():
    return _shell_transport("consolidate")


def _insight_transport():
    return _shell_transport("insight")


def event_resummarize(db, data: dict) -> dict:
    '''重跑事件摘要：`{id}` 单条，`{ids: [...]}` 多条，缺省=全部待复核。'''
    ids = data.get("ids")
    if ids is None and data.get("id") is not None:
        ids = [data["id"]]
    if ids is not None and not isinstance(ids, list):
        raise AdminError("ids 必须是数组")
    return maintenance.resummarize(
        db, event_ids=ids, transport_factory=_summary_transport,
        consolidate_factory=_consolidate_transport)


def insight_induce(db, data: dict) -> dict:
    '''跑一轮洞见归纳（后台）。'''
    try:
        min_support = max(2, int(data.get("min_support") or 3))
        max_clusters = max(1, int(data.get("max_clusters") or 5))
    except (TypeError, ValueError):
        raise AdminError("min_support / max_clusters 必须是整数") from None
    return maintenance.induce_insights(
        db, transport_factory=_insight_transport, min_support=min_support,
        max_clusters=max_clusters, threshold=data.get("threshold"))


def maintenance_status(db, data: dict) -> dict:
    return maintenance.status(db)


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
    "self.list": self_list,
    "self.set": self_set,
    "self.disable": self_disable,
    "settings.get": settings_get,
    "settings.set": settings_set,
    "vec.status": vec_status,
    "vec.reindex": vec_reindex,
    "maintenance.status": maintenance_status,
    "event.resummarize": event_resummarize,
    "insight.induce": insight_induce,
    "core.preview": lambda db, data: core.compile_core(db),
    # 管理页的 chip 只读最近一次召回的快照：重算会拨快冷却计数
    "recall.preview": lambda db, data: core.last_recall(),
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
        # 自我认知变化 → 通知外壳刷新 core（系统提示词/动态上下文下一轮生效）
        if op.startswith("self.") or (op in ("insert", "update", "delete")
                                      and payload.get("table") == "self_memory"):
            frame.push(q, data, "notify", {"kind": "core"})
        if op == "settings.set":
            frame.push(q, data, "notify", {"kind": "settings"})
    except AdminError as exc:
        frame.push(q, data, "admin", {"ok": False, "op": op, "error": str(exc)})
    except Exception as exc:      # 兜底：管理界面不能把内核打挂
        base.log.error("admin.failed", op=op, error=repr(exc))
        frame.push(q, data, "admin",
                   {"ok": False, "op": op, "error": type(exc).__name__})

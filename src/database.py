# -*- coding: utf-8 -*-
"""SQLite 存储层：行 ↔ dataclass 映射 + 参数化 SQL。

设计约束（与 docs/database.md 一致）：

- 三张表：`memory`（引用与归属）、`message`（正文）、`event`（事件）。
- **只追加**：记忆、消息没有删除或失效接口；事件摘要允许后续补充（doc 明确「可后续补充」）。
- 事件链 `prev_event_id ⇄ next_event_id` 为同会话双向链，插入时在同一事务内回填。
- 单进程串行写入：进程内 `RLock` + SQLite `busy_timeout`；WAL 模式下读不阻塞写。

用法：

    with Database() as db:                     # 路径与超时取自 config
        db.insert_message(Message(...))
        event = db.create_event("s_1", "调 drawio 配色")
        db.bind_event("s_1", "m_1024", event.id)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

import base
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
  session_id  TEXT    NOT NULL,
  message_id  TEXT    NOT NULL,
  owner       TEXT    NOT NULL,
  seq         INTEGER NOT NULL,
  occur_at    TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,
  event_id    INTEGER,
  PRIMARY KEY (session_id, message_id),
  UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS message (
  session_id  TEXT    NOT NULL,
  message_id  TEXT    NOT NULL,
  owner       TEXT    NOT NULL,
  seq         INTEGER NOT NULL,
  role        TEXT    NOT NULL,
  content     TEXT    NOT NULL,
  occur_at    TEXT    NOT NULL,
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (session_id, message_id),
  UNIQUE (session_id, seq)
);

CREATE TABLE IF NOT EXISTS event (
  id             INTEGER PRIMARY KEY,
  session_id     TEXT    NOT NULL,
  title          TEXT    NOT NULL,
  summary        TEXT,
  created_at     TEXT    NOT NULL,
  prev_event_id  INTEGER,
  next_event_id  INTEGER,
  related_events TEXT,
  cnt            INTEGER NOT NULL DEFAULT 0
);

-- 仅加索引，不改列；related_events 按 doc 要求不建索引
CREATE INDEX IF NOT EXISTS idx_memory_session_seq  ON memory(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_memory_event        ON memory(event_id);
CREATE INDEX IF NOT EXISTS idx_message_session_seq ON message(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_session       ON event(session_id, id);

-- 经验（程序记忆）：怎么做
CREATE TABLE IF NOT EXISTS experience (
  id              INTEGER PRIMARY KEY,
  context         TEXT,                       -- 触发条件：什么情形下适用
  content         TEXT    NOT NULL,           -- 一句话做法（≤200 字符）
  kind            TEXT    NOT NULL DEFAULT 'technique',  -- technique / pitfall / command
  confidence      REAL    NOT NULL DEFAULT 0.8,
  importance      INTEGER NOT NULL DEFAULT 3,
  origin_session  TEXT,                       -- 从哪次会话学到的
  origin_project  TEXT,                       -- 从哪个项目学到的；NULL = 通用
  source_event_id INTEGER NOT NULL,
  cues            TEXT,                       -- JSON 数组：检索线索
  use_count       INTEGER NOT NULL DEFAULT 0,
  last_used_at    TEXT,
  created_at      TEXT    NOT NULL,
  status          TEXT    NOT NULL DEFAULT 'active',  -- active / superseded / invalid
  superseded_id   INTEGER,
  valid_to        TEXT,
  invalid_reason  TEXT
);

-- 认知（语义记忆）：是什么
CREATE TABLE IF NOT EXISTS cognition (
  id              INTEGER PRIMARY KEY,
  subject         TEXT    NOT NULL,           -- 命题主体：用户 / 项目 / 工具
  content         TEXT    NOT NULL,           -- 一句话事实（≤200 字符）
  kind            TEXT    NOT NULL DEFAULT 'fact',  -- fact / preference / constraint
  confidence      REAL    NOT NULL DEFAULT 0.8,
  importance      INTEGER NOT NULL DEFAULT 3,
  origin_session  TEXT,
  origin_project  TEXT,
  source_event_id INTEGER NOT NULL,
  cues            TEXT,
  use_count       INTEGER NOT NULL DEFAULT 0,
  last_used_at    TEXT,
  created_at      TEXT    NOT NULL,
  status          TEXT    NOT NULL DEFAULT 'active',
  superseded_id   INTEGER,
  valid_to        TEXT,
  invalid_reason  TEXT
);

-- 工作记忆：可覆盖的缓存
CREATE TABLE IF NOT EXISTS digest (
  session_id    TEXT PRIMARY KEY,
  content       TEXT NOT NULL,
  ref_event_ids TEXT,                         -- JSON 数组
  revision      INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exp_origin  ON experience(origin_project, status);
CREATE INDEX IF NOT EXISTS idx_exp_event   ON experience(source_event_id);
CREATE INDEX IF NOT EXISTS idx_cog_origin  ON cognition(origin_project, status);
CREATE INDEX IF NOT EXISTS idx_cog_subject ON cognition(subject);
CREATE INDEX IF NOT EXISTS idx_cog_event   ON cognition(source_event_id);

-- 统一全文索引：跨 event / experience / cognition；trigram 支持中文子串
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  ref_kind UNINDEXED,
  ref_id   UNINDEXED,
  title,
  body,
  tokenize='trigram'
);
"""

# list_events 的排序白名单：列名直接拼 SQL，必须是固定常量
_EVENT_ORDER = {"id": "id", "cnt": "cnt"}


# ---------------- 行模型 ----------------

@dataclass(frozen=True, slots=True)
class Memory:
    '''记忆表一行：只存引用与归属，不含正文。'''
    session_id: str
    message_id: str
    owner: str
    seq: int
    occur_at: str
    created_at: str
    event_id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Memory":
        return cls(
            session_id=row["session_id"],
            message_id=row["message_id"],
            owner=row["owner"],
            seq=row["seq"],
            occur_at=row["occur_at"],
            created_at=row["created_at"],
            event_id=row["event_id"],
        )


@dataclass(frozen=True, slots=True)
class Message:
    '''消息表一行。工具消息的 content 为 {"call": ..., "result": ...} 的 JSON 文本。'''
    session_id: str
    message_id: str
    owner: str
    seq: int
    role: str
    content: str
    occur_at: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        return cls(
            session_id=row["session_id"],
            message_id=row["message_id"],
            owner=row["owner"],
            seq=row["seq"],
            role=row["role"],
            content=row["content"],
            occur_at=row["occur_at"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class Event:
    '''事件表一行。related_events 在库里是 JSON 文本，读出来是 tuple[int]。'''
    id: int
    session_id: str
    title: str
    summary: str | None
    created_at: str
    prev_event_id: int | None
    next_event_id: int | None
    related_events: tuple[int, ...] = field(default_factory=tuple)
    cnt: int = 0            # 被使用/召回次数，新建为 0，用于查询排序

    def to_dict(self) -> dict:
        return {
            "id": self.id, "session_id": self.session_id, "title": self.title,
            "summary": self.summary, "created_at": self.created_at,
            "prev_event_id": self.prev_event_id, "next_event_id": self.next_event_id,
            "related_events": list(self.related_events), "cnt": self.cnt,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        raw = row["related_events"]
        try:
            related = tuple(int(x) for x in json.loads(raw)) if raw else ()
        except (TypeError, ValueError):
            related = ()
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            summary=row["summary"],
            created_at=row["created_at"],
            prev_event_id=row["prev_event_id"],
            next_event_id=row["next_event_id"],
            related_events=related,
            cnt=row["cnt"] if "cnt" in row.keys() else 0,
        )


@dataclass(frozen=True, slots=True)
class EventSpan:
    '''事件 + 其发生区间（由关联消息的 occur_at 聚合得到，不落库）。'''
    event: Event
    first_at: str
    last_at: str


# l3 两表共用列：检索时用 UNION ALL 合成一个视图
_L3_COLUMNS = (
    "content", "kind", "confidence", "importance",
    "origin_session", "origin_project", "source_event_id", "cues",
    "use_count", "last_used_at", "created_at", "status",
    "superseded_id", "valid_to", "invalid_reason",
)
_L3_EXTRA = {"experience": "context", "cognition": "subject"}


@dataclass(frozen=True, slots=True)
class Experience:
    '''经验（程序记忆）：context 是触发条件，content 是做法。'''
    id: int = 0
    context: str | None = None
    content: str = ""
    kind: str = "technique"
    confidence: float = 0.8
    importance: int = 3
    origin_session: str | None = None
    origin_project: str | None = None
    source_event_id: int | None = None
    cues: tuple[str, ...] = ()
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str = ""
    status: str = "active"
    superseded_id: int | None = None
    valid_to: str | None = None
    invalid_reason: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Experience":
        return cls(
            id=row["id"], context=row["context"], content=row["content"],
            kind=row["kind"], confidence=row["confidence"],
            importance=row["importance"], origin_session=row["origin_session"],
            origin_project=row["origin_project"], source_event_id=row["source_event_id"],
            cues=_load_cues(row["cues"]), use_count=row["use_count"],
            last_used_at=row["last_used_at"], created_at=row["created_at"],
            status=row["status"], superseded_id=row["superseded_id"],
            valid_to=row["valid_to"], invalid_reason=row["invalid_reason"],
        )


@dataclass(frozen=True, slots=True)
class Cognition:
    '''认知（语义记忆）：subject 是命题主体，content 是事实。'''
    id: int = 0
    subject: str = ""
    content: str = ""
    kind: str = "fact"
    confidence: float = 0.8
    importance: int = 3
    origin_session: str | None = None
    origin_project: str | None = None
    source_event_id: int | None = None
    cues: tuple[str, ...] = ()
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str = ""
    status: str = "active"
    superseded_id: int | None = None
    valid_to: str | None = None
    invalid_reason: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Cognition":
        return cls(
            id=row["id"], subject=row["subject"], content=row["content"],
            kind=row["kind"], confidence=row["confidence"],
            importance=row["importance"], origin_session=row["origin_session"],
            origin_project=row["origin_project"], source_event_id=row["source_event_id"],
            cues=_load_cues(row["cues"]), use_count=row["use_count"],
            last_used_at=row["last_used_at"], created_at=row["created_at"],
            status=row["status"], superseded_id=row["superseded_id"],
            valid_to=row["valid_to"], invalid_reason=row["invalid_reason"],
        )


@dataclass(frozen=True, slots=True)
class Digest:
    '''工作记忆快照：可覆盖的缓存，不是事实源。'''
    session_id: str
    content: str
    ref_event_ids: tuple[int, ...] = ()
    revision: int = 0
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Digest":
        raw = row["ref_event_ids"]
        try:
            refs = tuple(int(x) for x in json.loads(raw)) if raw else ()
        except (TypeError, ValueError):
            refs = ()
        return cls(session_id=row["session_id"], content=row["content"],
                   ref_event_ids=refs, revision=row["revision"],
                   updated_at=row["updated_at"])


def _load_cues(raw) -> tuple:
    try:
        return tuple(str(x) for x in json.loads(raw)) if raw else ()
    except (TypeError, ValueError):
        return ()


# ---------------- 存储 ----------------

class Database:
    '''单文件 SQLite 封装：WAL、写串行、事务化。'''

    def __init__(self, path: str | Path | None = None,
                 busy_timeout_ms: int | None = None):
        store = config.get_config().get("store", {})
        self.path = Path(path or store.get("db_path") or "memory.db")
        self.busy_timeout_ms = int(
            store.get("busy_timeout_ms", 5000) if busy_timeout_ms is None
            else busy_timeout_ms)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path),
                                     timeout=self.busy_timeout_ms / 1000,
                                     check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self.init_schema()

    # ---- 生命周期 ----

    def init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self):
        '''老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。'''
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(event)")}
        if "cnt" not in columns:
            self._conn.execute(
                "ALTER TABLE event ADD COLUMN cnt INTEGER NOT NULL DEFAULT 0")

    def close(self):
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        '''
        显式事务：成功提交，异常回滚。
        嵌套调用降级为 SAVEPOINT，避免 "cannot start a transaction within a transaction"。
        '''
        with self._lock:
            if self._conn.in_transaction:
                name = f"sp_{threading.get_ident()}_{id(object())}"
                self._conn.execute(f"SAVEPOINT {name}")
                try:
                    yield self._conn
                except BaseException:
                    self._conn.execute(f"ROLLBACK TO {name}")
                    self._conn.execute(f"RELEASE {name}")
                    raise
                else:
                    self._conn.execute(f"RELEASE {name}")
                return

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---- 写入：记忆 ----

    def insert_memory(self, item: Memory) -> bool:
        '''插入一条引用；已存在返回 False（幂等）。'''
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO memory "
                "(session_id, message_id, owner, seq, occur_at, created_at, event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.session_id, item.message_id, item.owner, item.seq,
                 item.occur_at, item.created_at, item.event_id))
            return cur.rowcount == 1

    def bind_event(self, session_id: str, message_id: str, event_id: int) -> bool:
        '''把记忆行归入事件；行不存在或已归属同一事件返回 False。'''
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memory SET event_id = ? "
                "WHERE session_id = ? AND message_id = ? "
                "AND (event_id IS NULL OR event_id != ?)",
                (event_id, session_id, message_id, event_id))
            return cur.rowcount == 1

    # ---- 写入：消息 ----

    def insert_message(self, item: Message) -> bool:
        '''插入一条消息；已存在返回 False（幂等）。'''
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO message "
                "(session_id, message_id, owner, seq, role, content, occur_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (item.session_id, item.message_id, item.owner, item.seq, item.role,
                 item.content, item.occur_at, item.created_at))
            return cur.rowcount == 1

    def insert_messages(self, items) -> int:
        '''批量插入；返回实际新增条数，整批在同一事务内。'''
        inserted = 0
        with self.transaction():
            for item in items:
                if self.insert_message(item):
                    inserted += 1
        return inserted

    # ---- 写入：事件 ----

    def create_event(self, session_id: str, title: str,
                     summary: str | None = None,
                     related_events=None) -> Event:
        '''
        新建事件并维护同会话双向链：取同会话最大 id 作 prev，回填上一条的 next。
        三步在同一事务内完成。
        '''
        related = [int(x) for x in (related_events or [])]
        created_at = base.get_cur_time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM event WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,)).fetchone()
            prev_id = row["id"] if row else None

            cur = conn.execute(
                "INSERT INTO event "
                "(session_id, title, summary, created_at, prev_event_id, next_event_id, related_events) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (session_id, title, summary, created_at, prev_id,
                 json.dumps(related) if related else None))
            event_id = cur.lastrowid

            if prev_id is not None:
                conn.execute("UPDATE event SET next_event_id = ? WHERE id = ?",
                             (event_id, prev_id))

        event = self.get_event(event_id)
        assert event is not None
        return event

    def set_event_summary(self, event_id: int, summary: str | None) -> bool:
        '''补充/覆盖事件摘要（doc：summary 可后续补充）；传 None 表示清空。'''
        with self._lock:
            cur = self._conn.execute(
                "UPDATE event SET summary = ? WHERE id = ?", (summary, event_id))
            return cur.rowcount == 1

    def set_event_title(self, event_id: int, title: str) -> bool:
        '''覆盖事件标题（摘要器产出新标题时用）。'''
        with self._lock:
            cur = self._conn.execute(
                "UPDATE event SET title = ? WHERE id = ?", (title, event_id))
            return cur.rowcount == 1

    def bump_event_cnt(self, event_id: int, by: int = 1) -> int | None:
        '''
        事件被使用/召回一次：cnt 加 by（默认 1）。
        返回新计数；事件不存在返回 None。单条 UPDATE，原子自增，不读改写。
        '''
        amount = int(by)
        if amount <= 0:
            raise ValueError("by 必须是正整数")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE event SET cnt = cnt + ? WHERE id = ?", (amount, event_id))
            if cur.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT cnt FROM event WHERE id = ?", (event_id,)).fetchone()
            return int(row["cnt"])

    def bump_events(self, event_ids, by: int = 1) -> int:
        '''批量自增（检索结果一次召回多条时用）；返回实际命中的事件数。'''
        amount = int(by)
        if amount <= 0:
            raise ValueError("by 必须是正整数")
        ids = [int(x) for x in event_ids]
        if not ids:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "UPDATE event SET cnt = cnt + ? WHERE id = ?",
                [(amount, event_id) for event_id in ids])
            return cur.rowcount

    # ---- 读取 ----

    def get_memory(self, session_id: str, message_id: str) -> Memory | None:
        row = self._conn.execute(
            "SELECT * FROM memory WHERE session_id = ? AND message_id = ?",
            (session_id, message_id)).fetchone()
        return Memory.from_row(row) if row else None

    def list_memory(self, session_id: str, after_seq: int | None = None,
                    limit: int = 500) -> list[Memory]:
        '''按会话取引用，seq 升序；after_seq 是「严格大于」的增量游标，None 表示从头。'''
        if after_seq is None:
            rows = self._conn.execute(
                "SELECT * FROM memory WHERE session_id = ? "
                "ORDER BY seq LIMIT ?", (session_id, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM memory WHERE session_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?", (session_id, after_seq, limit)).fetchall()
        return [Memory.from_row(row) for row in rows]

    def get_message(self, session_id: str, message_id: str) -> Message | None:
        row = self._conn.execute(
            "SELECT * FROM message WHERE session_id = ? AND message_id = ?",
            (session_id, message_id)).fetchone()
        return Message.from_row(row) if row else None

    def list_messages(self, session_id: str, after_seq: int | None = None,
                      limit: int = 500) -> list[Message]:
        '''按会话取消息，seq 升序；after_seq 是「严格大于」的增量游标，None 表示从头。'''
        if after_seq is None:
            rows = self._conn.execute(
                "SELECT * FROM message WHERE session_id = ? "
                "ORDER BY seq LIMIT ?", (session_id, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM message WHERE session_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?", (session_id, after_seq, limit)).fetchall()
        return [Message.from_row(row) for row in rows]

    def list_event_messages(self, event_id: int) -> list[Message]:
        '''取事件关联的全部消息，按 seq 升序（即事件正文）。'''
        rows = self._conn.execute(
            "SELECT m.* FROM message m "
            "JOIN memory r ON r.session_id = m.session_id AND r.message_id = m.message_id "
            "WHERE r.event_id = ? ORDER BY m.seq", (event_id,)).fetchall()
        return [Message.from_row(row) for row in rows]

    def get_event(self, event_id: int) -> Event | None:
        row = self._conn.execute(
            "SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
        return Event.from_row(row) if row else None

    def event_for_message(self, session_id: str, message_id: str) -> int | None:
        '''消息已归属的事件 id；未归属返回 None（切分时用于判重）。'''
        row = self._conn.execute(
            "SELECT event_id FROM memory WHERE session_id = ? AND message_id = ?",
            (session_id, message_id)).fetchone()
        if row is None or row["event_id"] is None:
            return None
        return int(row["event_id"])

    def list_events(self, session_id: str, limit: int = 50,
                    descending: bool = True, order_by: str = "id") -> list[Event]:
        '''按会话取事件；order_by 支持 id（默认，时间序）与 cnt（使用次数，热度序）。'''
        column = _EVENT_ORDER.get(order_by)
        if column is None:
            raise ValueError(f"order_by 只支持 {sorted(_EVENT_ORDER)}")
        order = "DESC" if descending else "ASC"
        rows = self._conn.execute(
            f"SELECT * FROM event WHERE session_id = ? "
            f"ORDER BY {column} {order}, id {order} LIMIT ?",
            (session_id, limit)).fetchall()
        return [Event.from_row(row) for row in rows]

    def event_span(self, event_id: int) -> EventSpan | None:
        '''事件发生区间 = 关联消息 occur_at 的 min/max（不落库，按需聚合）。'''
        event = self.get_event(event_id)
        if event is None:
            return None
        row = self._conn.execute(
            "SELECT MIN(m.occur_at) AS first_at, MAX(m.occur_at) AS last_at "
            "FROM message m JOIN memory r "
            "ON r.session_id = m.session_id AND r.message_id = m.message_id "
            "WHERE r.event_id = ?", (event_id,)).fetchone()
        if not row or row["first_at"] is None:
            return None
        return EventSpan(event=event, first_at=row["first_at"], last_at=row["last_at"])

    def list_events_overlapping(self, session_id: str, from_at: str, to_at: str,
                                limit: int = 50) -> list[EventSpan]:
        '''
        取发生区间与 [from_at, to_at] 相交的事件（闭区间）：
        last_at >= from_at AND first_at <= to_at。
        '''
        rows = self._conn.execute(
            "SELECT e.*, t.first_at AS first_at, t.last_at AS last_at FROM event e "
            "JOIN (SELECT r.event_id AS eid, MIN(m.occur_at) AS first_at, "
            "             MAX(m.occur_at) AS last_at "
            "      FROM memory r JOIN message m "
            "        ON m.session_id = r.session_id AND m.message_id = r.message_id "
            "      WHERE r.event_id IS NOT NULL GROUP BY r.event_id) t ON t.eid = e.id "
            "WHERE e.session_id = ? AND t.last_at >= ? AND t.first_at <= ? "
            "ORDER BY t.last_at DESC LIMIT ?",
            (session_id, from_at, to_at, limit)).fetchall()
        return [EventSpan(event=Event.from_row(row),
                          first_at=row["first_at"], last_at=row["last_at"])
                for row in rows]

    # ---- 写入：l3（经验 / 认知） ----

    def _l3_values(self, table: str, item) -> tuple:
        extra = _L3_EXTRA[table]
        values = [getattr(item, extra)]
        for name in _L3_COLUMNS:
            value = getattr(item, name)
            if name == "cues":
                value = json.dumps(list(value), ensure_ascii=False) if value else None
            elif name == "created_at" and not value:
                value = base.get_cur_time()
            values.append(value)
        return tuple(values)

    def _l3_insert(self, conn, table: str, item) -> int:
        extra = _L3_EXTRA[table]
        columns = (extra,) + _L3_COLUMNS
        placeholders = ", ".join("?" for _ in columns)
        cur = conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            self._l3_values(table, item))
        return int(cur.lastrowid)

    def insert_experience(self, item: Experience) -> int:
        '''写入一条经验（程序记忆），返回新 id。'''
        with self.transaction() as conn:
            return self._l3_insert(conn, "experience", item)

    def insert_cognition(self, item: Cognition) -> int:
        '''写入一条认知（语义记忆），返回新 id。'''
        with self.transaction() as conn:
            return self._l3_insert(conn, "cognition", item)

    def _get_l3(self, table: str, model, item_id: int):
        row = self._conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        return model.from_row(row) if row else None

    def get_experience(self, item_id: int) -> Experience | None:
        return self._get_l3("experience", Experience, item_id)

    def get_cognition(self, item_id: int) -> Cognition | None:
        return self._get_l3("cognition", Cognition, item_id)

    def find_l3_id(self, table: str, event_id: int, content: str) -> int | None:
        '''同一来源事件下是否已有相同内容的条目（幂等写入用）。'''
        if table not in _L3_EXTRA:
            raise ValueError(f"不支持的 l3 表: {table!r}")
        row = self._conn.execute(
            f"SELECT id FROM {table} WHERE source_event_id = ? AND content = ? "
            f"ORDER BY id LIMIT 1", (event_id, content)).fetchone()
        return int(row["id"]) if row else None

    def find_l3_candidates(self, table: str, *, subject: str | None = None,
                           query: str | None = None,
                           origin_project: str | None = None,
                           limit: int = 5) -> list:
        '''召回合并候选（active、未过期）：主体精确或内容 LIKE。'''
        if table not in _L3_EXTRA:
            raise ValueError(f"不支持的 l3 表: {table!r}")
        model = Experience if table == "experience" else Cognition
        clauses = ["status = 'active'", "(valid_to IS NULL OR valid_to > ?)"]
        params: list = [base.get_cur_time()]
        if origin_project is not None:
            clauses.append("(origin_project IS NULL OR origin_project = ?)")
            params.append(origin_project)
        if subject and query:
            clauses.append("(subject = ? OR content LIKE ?)")
            params.extend([subject, f"%{query}%"])
        elif subject:
            clauses.append("subject = ?")
            params.append(subject)
        elif query:
            clauses.append("content LIKE ?")
            params.append(f"%{query}%")
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY importance DESC, id DESC LIMIT ?", params).fetchall()
        return [model.from_row(row) for row in rows]

    def _list_l3(self, table: str, model, *, origin_project: str | None = None,
                 status: str = "active", kind: str | None = None,
                 limit: int = 50, include_expired: bool = False) -> list:
        clauses = ["status = ?"]
        params: list = [status]
        if not include_expired:
            clauses.append("(valid_to IS NULL OR valid_to > ?)")
            params.append(base.get_cur_time())
        if origin_project is not None:
            clauses.append("(origin_project IS NULL OR origin_project = ?)")
            params.append(origin_project)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY id DESC LIMIT ?", params).fetchall()
        return [model.from_row(row) for row in rows]

    def list_experiences(self, **kwargs) -> list:
        '''按条件取经验；origin_project 给定时包含通用条目（origin_project IS NULL）。'''
        return self._list_l3("experience", Experience, **kwargs)

    def list_cognitions(self, **kwargs) -> list:
        return self._list_l3("cognition", Cognition, **kwargs)

    def _supersede_l3(self, table: str, old_id: int, item) -> int:
        '''用新条目取代旧条目：新行 superseded_id=old，旧行 status='superseded'。'''
        now = base.get_cur_time()
        with self.transaction() as conn:
            old = conn.execute(
                f"SELECT id FROM {table} WHERE id = ?", (old_id,)).fetchone()
            if old is None:
                raise KeyError(f"{table} {old_id} 不存在")
            new_item = replace(item, superseded_id=old_id, status="active",
                               created_at=item.created_at or now)
            new_id = self._l3_insert(conn, table, new_item)
            conn.execute(
                f"UPDATE {table} SET status = 'superseded', valid_to = ? WHERE id = ?",
                (now, old_id))
            return new_id

    def supersede_experience(self, old_id: int, item: Experience) -> int:
        return self._supersede_l3("experience", old_id, item)

    def supersede_cognition(self, old_id: int, item: Cognition) -> int:
        return self._supersede_l3("cognition", old_id, item)

    def _invalidate_l3(self, table: str, item_id: int, reason: str) -> bool:
        '''失效（无后继）：只改状态与原因，不删行、不改 content。'''
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE {table} SET status = 'invalid', invalid_reason = ?, valid_to = ? "
                f"WHERE id = ? AND status = 'active'",
                (reason, base.get_cur_time(), item_id))
            return cur.rowcount == 1

    def invalidate_experience(self, item_id: int, reason: str) -> bool:
        return self._invalidate_l3("experience", item_id, reason)

    def invalidate_cognition(self, item_id: int, reason: str) -> bool:
        return self._invalidate_l3("cognition", item_id, reason)

    def _touch_l3(self, table: str, ids, by: int = 1) -> int:
        amount = int(by)
        if amount <= 0:
            raise ValueError("by 必须是正整数")
        ids = [int(x) for x in ids]
        if not ids:
            return 0
        now = base.get_cur_time()
        with self.transaction() as conn:
            cur = conn.executemany(
                f"UPDATE {table} SET use_count = use_count + ?, last_used_at = ? WHERE id = ?",
                [(amount, now, item_id) for item_id in ids])
            return cur.rowcount

    def touch_experiences(self, ids, by: int = 1) -> int:
        '''经验被召回：use_count 自增、last_used_at 刷新。'''
        return self._touch_l3("experience", ids, by)

    def touch_cognitions(self, ids, by: int = 1) -> int:
        return self._touch_l3("cognition", ids, by)

    # ---- 写入：digest（工作记忆缓存） ----

    def upsert_digest(self, session_id: str, content: str,
                      ref_event_ids=None) -> int:
        '''覆盖写某会话的工作记忆，返回新 revision。'''
        now = base.get_cur_time()
        refs = json.dumps([int(x) for x in (ref_event_ids or [])], ensure_ascii=False)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM digest WHERE session_id = ?", (session_id,)).fetchone()
            revision = (row["revision"] + 1) if row else 1
            conn.execute(
                "INSERT INTO digest (session_id, content, ref_event_ids, revision, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "content = excluded.content, ref_event_ids = excluded.ref_event_ids, "
                "revision = excluded.revision, updated_at = excluded.updated_at",
                (session_id, content, refs, revision, now))
            return revision

    def get_digest(self, session_id: str) -> Digest | None:
        row = self._conn.execute(
            "SELECT * FROM digest WHERE session_id = ?", (session_id,)).fetchone()
        return Digest.from_row(row) if row else None

    # ---- 索引：统一全文检索 ----

    def index_fts(self, ref_kind: str, ref_id: int, title: str = "",
                  body: str = "") -> None:
        '''写入/覆盖一条全文索引；ref_kind 取 event / experience / cognition。'''
        if ref_kind not in _L3_EXTRA and ref_kind != "event":
            raise ValueError(f"不支持的 ref_kind: {ref_kind!r}")
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM memory_fts WHERE ref_kind = ? AND ref_id = ?",
                (ref_kind, int(ref_id)))
            conn.execute(
                "INSERT INTO memory_fts (ref_kind, ref_id, title, body) VALUES (?, ?, ?, ?)",
                (ref_kind, int(ref_id), title or "", body or ""))

    def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        '''全文检索；trigram 需要 ≥3 字符，更短的查询退回 LIKE。'''
        q = (query or "").strip()
        if not q:
            return []
        if len(q) >= 3:
            rows = self._conn.execute(
                "SELECT ref_kind, ref_id, title, body, bm25(memory_fts) AS rank "
                "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (q, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT ref_kind, ref_id, title, body, 0.0 AS rank "
                "FROM memory_fts WHERE body LIKE ? LIMIT ?",
                (f"%{q}%", limit)).fetchall()
        return [dict(row) for row in rows]

    # ---- 统计 ----

    def count_memory(self, session_id: str | None = None) -> int:
        if session_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM memory").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM memory WHERE session_id = ?",
                (session_id,)).fetchone()
        return int(row["n"])

    def count_messages(self, session_id: str | None = None) -> int:
        if session_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM message").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM message WHERE session_id = ?",
                (session_id,)).fetchone()
        return int(row["n"])

    def count_events(self, session_id: str | None = None) -> int:
        if session_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM event").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM event WHERE session_id = ?",
                (session_id,)).fetchone()
        return int(row["n"])

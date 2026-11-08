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
  cnt            INTEGER NOT NULL DEFAULT 0,
  origin_project TEXT,                       -- 事件归属项目；NULL = 全局可见（情景证据链）
  summarized_at  TEXT,                       -- 上次摘要时间；用于判断是否需要重跑调用 A
  needs_review   INTEGER NOT NULL DEFAULT 0, -- 摘要失败/降级，等待复核
  review_reason  TEXT,
  last_used_at   TEXT                        -- 最近一次被召回/再认的时间
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
  scope           TEXT    NOT NULL DEFAULT 'project',  -- global / project：适用范围的模型判断
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

-- 洞见（归纳层）：由多条经验归纳出的通用规则，证据链在 source_experience_ids
CREATE TABLE IF NOT EXISTS insight (
  id                   INTEGER PRIMARY KEY,
  content              TEXT    NOT NULL,      -- 归纳后的规则（≤200 字符）
  scope                TEXT    NOT NULL DEFAULT 'global',  -- global / project
  confidence           REAL    NOT NULL DEFAULT 0.8,
  importance           INTEGER NOT NULL DEFAULT 4,
  origin_session       TEXT,
  origin_project       TEXT,
  source_experience_ids TEXT,                 -- JSON 数组：支撑这条洞见的经验 id
  cues                 TEXT,                  -- JSON 数组
  use_count            INTEGER NOT NULL DEFAULT 0,
  last_used_at         TEXT,
  created_at           TEXT    NOT NULL,
  status               TEXT    NOT NULL DEFAULT 'active',
  superseded_id        INTEGER,
  valid_to             TEXT,
  invalid_reason       TEXT
);

-- 自我认知：configured（设置，进系统提示词）/ agent（自己写，进动态上下文）
CREATE TABLE IF NOT EXISTS self_memory (
  id              INTEGER PRIMARY KEY,
  source          TEXT    NOT NULL DEFAULT 'configured',  -- configured / agent
  kind            TEXT    NOT NULL DEFAULT 'identity',    -- identity / behavior / limitation / goal
  content         TEXT    NOT NULL,
  revision        INTEGER NOT NULL DEFAULT 1,             -- 每次改写 +1（只追加，可回滚）
  origin_session  TEXT,
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

-- 插件设置（模型/embedding 等）：本地键值，JSON 字符串
CREATE TABLE IF NOT EXISTS app_setting (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exp_origin  ON experience(origin_project, status);
CREATE INDEX IF NOT EXISTS idx_exp_event   ON experience(source_event_id);
CREATE INDEX IF NOT EXISTS idx_cog_origin  ON cognition(origin_project, status);
CREATE INDEX IF NOT EXISTS idx_cog_subject ON cognition(subject);
CREATE INDEX IF NOT EXISTS idx_cog_event   ON cognition(source_event_id);
CREATE INDEX IF NOT EXISTS idx_ins_origin  ON insight(origin_project, status);
CREATE INDEX IF NOT EXISTS idx_self_source ON self_memory(source, status);

-- 统一全文索引：跨 event / experience / cognition；trigram 支持中文子串
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  ref_kind UNINDEXED,
  ref_id   UNINDEXED,
  title,
  body,
  origin_project UNINDEXED,     -- 范围列：召回前按项目过滤，别让范围外条目占名额
  session_id     UNINDEXED,     -- 事件来源会话（l3 为 origin_session）
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
    origin_project: str | None = None  # 归属项目；NULL = 全局可见
    summarized_at: str | None = None   # 上次摘要时间；NULL = 尚未摘要
    needs_review: int = 0              # 1 = 待复核（摘要失败或条目降级）
    review_reason: str | None = None
    last_used_at: str | None = None    # 最近一次被召回/再认

    def to_dict(self) -> dict:
        return {
            "id": self.id, "session_id": self.session_id, "title": self.title,
            "summary": self.summary, "created_at": self.created_at,
            "prev_event_id": self.prev_event_id, "next_event_id": self.next_event_id,
            "related_events": list(self.related_events), "cnt": self.cnt,
            "origin_project": self.origin_project,
            "summarized_at": self.summarized_at,
            "needs_review": bool(self.needs_review),
            "review_reason": self.review_reason,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        raw = row["related_events"]
        try:
            related = tuple(int(x) for x in json.loads(raw)) if raw else ()
        except (TypeError, ValueError):
            related = ()
        keys = row.keys()
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            title=row["title"],
            summary=row["summary"],
            created_at=row["created_at"],
            prev_event_id=row["prev_event_id"],
            next_event_id=row["next_event_id"],
            related_events=related,
            cnt=row["cnt"] if "cnt" in keys else 0,
            origin_project=row["origin_project"] if "origin_project" in keys else None,
            summarized_at=row["summarized_at"] if "summarized_at" in keys else None,
            needs_review=row["needs_review"] if "needs_review" in keys else 0,
            review_reason=row["review_reason"] if "review_reason" in keys else None,
            last_used_at=row["last_used_at"] if "last_used_at" in keys else None,
        )


@dataclass(frozen=True, slots=True)
class EventSpan:
    '''事件 + 其发生区间（由关联消息的 occur_at 聚合得到，不落库）。'''
    event: Event
    first_at: str
    last_at: str


# l3 两表共用列：检索时用 UNION ALL 合成一个视图
_VEC_TABLES = {
    "experience": "vec_experience",
    "cognition": "vec_cognition",
    "insight": "vec_insight",
}
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
    scope: str = "project"        # global（跨项目成立）/ project（依赖本项目结构）
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
            origin_project=row["origin_project"], scope=row["scope"],
            source_event_id=row["source_event_id"],
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
class Insight:
    '''洞见（归纳层）：由多条经验归纳出的规则，source_experience_ids 是证据链。'''
    id: int = 0
    content: str = ""
    scope: str = "global"
    confidence: float = 0.8
    importance: int = 4
    origin_session: str | None = None
    origin_project: str | None = None
    source_experience_ids: tuple[int, ...] = ()
    cues: tuple[str, ...] = ()
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str = ""
    status: str = "active"
    superseded_id: int | None = None
    valid_to: str | None = None
    invalid_reason: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Insight":
        return cls(
            id=row["id"], content=row["content"], scope=row["scope"],
            confidence=row["confidence"], importance=row["importance"],
            origin_session=row["origin_session"],
            origin_project=row["origin_project"],
            source_experience_ids=_load_ids(row["source_experience_ids"]),
            cues=_load_cues(row["cues"]), use_count=row["use_count"],
            last_used_at=row["last_used_at"], created_at=row["created_at"],
            status=row["status"], superseded_id=row["superseded_id"],
            valid_to=row["valid_to"], invalid_reason=row["invalid_reason"],
        )


@dataclass(frozen=True, slots=True)
class SelfMemory:
    '''自我认知：source=configured 进系统提示词；source=agent 进动态上下文。'''
    id: int = 0
    source: str = "configured"
    kind: str = "identity"
    content: str = ""
    revision: int = 1
    origin_session: str | None = None
    created_at: str = ""
    status: str = "active"
    superseded_id: int | None = None
    valid_to: str | None = None
    invalid_reason: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SelfMemory":
        return cls(
            id=row["id"], source=row["source"], kind=row["kind"],
            content=row["content"], revision=row["revision"],
            origin_session=row["origin_session"], created_at=row["created_at"],
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


def _load_ids(raw) -> tuple:
    '''JSON 数组 → 整数元组（洞见的证据链）。'''
    try:
        return tuple(int(x) for x in json.loads(raw)) if raw else ()
    except (TypeError, ValueError):
        return ()


class _StaleTarget(Exception):
    '''supersede 的目标行在事务内已不再是 active（并发或同批重复指向）。'''


def _like_escape(text: str) -> str:
    '''转义 LIKE 的通配符：查询里的 % / _ 是字面量，不是通配符。'''
    return (str(text).replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


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
        self._savepoint_seq = 0
        self._savepoint_seq_lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path),
                                     timeout=self.busy_timeout_ms / 1000,
                                     check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        # 临时表放内存：沙箱/受限环境里系统临时目录可能不可写，
        # 而 FTS5 的 DROP/CREATE 会用到临时存储（表现为 unable to open database file）
        self._conn.execute("PRAGMA temp_store = MEMORY")
        self._vec_enabled = self._load_vec()
        self.init_schema()
        if self._vec_enabled:
            self._init_vec_tables()

    # ---- 生命周期 ----

    def init_schema(self):
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()

    # ---- sqlite-vec ----

    def _load_vec(self) -> bool:
        '''加载 sqlite-vec 扩展；失败只降级，不影响主库。'''
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._conn.execute("SELECT vec_version()").fetchone()
            return True
        except Exception as exc:
            base.log.warning("db.vec_unavailable", error=repr(exc))
            return False

    def _init_vec_tables(self):
        dim = int(config.get_config().get("embedding", {}).get("dim", 1024))
        self._vec_dim = dim
        for kind, table in _VEC_TABLES.items():
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if row is not None and f"float[{dim}]" not in (row["sql"] or "").lower():
                self._conn.execute(f"DROP TABLE {table}")   # 维度变了：重建（需重索引）
                row = None
            if row is None:
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE {table} USING vec0("
                    f"ref_id INTEGER PRIMARY KEY, origin_project TEXT, "
                    f"embedding FLOAT[{dim}])")
        self._conn.commit()

    @staticmethod
    def _vec_blob(vector) -> bytes:
        import sqlite_vec
        return sqlite_vec.serialize_float32([float(x) for x in vector])

    def vec_upsert(self, kind: str, ref_id: int, vector,
                   origin_project: str | None = None) -> bool:
        table = _VEC_TABLES.get(kind)
        if not self._vec_enabled or table is None or not vector:
            return False
        blob = self._vec_blob(vector)
        with self.transaction() as conn:
            conn.execute(f"DELETE FROM {table} WHERE ref_id = ?", (int(ref_id),))
            conn.execute(
                f"INSERT INTO {table} (ref_id, origin_project, embedding) "
                f"VALUES (?, ?, ?)",
                (int(ref_id), origin_project or "", blob))
        return True

    def vec_unindex(self, kind: str, ref_id: int) -> bool:
        '''移除一条向量（只清索引，记忆行本身不动）。'''
        table = _VEC_TABLES.get(kind)
        if not self._vec_enabled or table is None:
            return False
        with self.transaction() as conn:
            self._vec_remove(conn, kind, ref_id)
        return True

    def _vec_remove(self, conn, kind: str, ref_id: int) -> None:
        '''在当前事务里清掉一条向量（记忆行本身不动，只清索引）。'''
        table = _VEC_TABLES.get(kind)
        if not self._vec_enabled or table is None:
            return
        conn.execute(f"DELETE FROM {table} WHERE ref_id = ?", (int(ref_id),))

    def vec_search(self, kind: str, vector, k: int = 50,
                   origin_project: str | None = None,
                   scope: str = "all") -> dict:
        '''返回 {ref_id: cosine distance}；scope=project 时合并「本项目 + 通用」。'''
        table = _VEC_TABLES.get(kind)
        if not self._vec_enabled or table is None or not vector:
            return {}
        blob = self._vec_blob(vector)
        limit = max(1, int(k))

        def run(project: str | None):
            sql = (f"SELECT ref_id, distance FROM {table} "
                   f"WHERE embedding MATCH ? AND k = {limit}")
            params = [blob]
            if project is not None:
                sql += " AND origin_project = ?"
                params.append(project)
            try:
                return {int(row["ref_id"]): float(row["distance"])
                        for row in self._conn.execute(sql, params)}
            except sqlite3.Error as exc:
                base.log.warning("db.vec_search_failed", kind=kind, error=repr(exc))
                return {}

        if scope == "project":
            merged = run(origin_project or "")
            for ref_id, distance in run("").items():
                merged[ref_id] = min(distance, merged.get(ref_id, distance))
            return merged
        return run(None)

    def vec_status(self) -> dict:
        tables = {}
        if self._vec_enabled:
            for kind, table in _VEC_TABLES.items():
                try:
                    count = self._conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                except sqlite3.Error:
                    count = 0
                tables[kind] = int(count)
        return {"enabled": self._vec_enabled,
                "dim": getattr(self, "_vec_dim", 0), "counts": tables}

    # ---- 插件设置 ----

    def get_setting(self, key: str):
        row = self._conn.execute(
            "SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return None

    def set_setting(self, key: str, value) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO app_setting (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, payload, base.get_cur_time()))

    def list_settings(self) -> dict:
        return {row["key"]: json.loads(row["value"])
                for row in self._conn.execute("SELECT key, value FROM app_setting")}

    def _migrate(self):
        '''老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段。'''
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(event)")}
        if "cnt" not in columns:
            self._conn.execute(
                "ALTER TABLE event ADD COLUMN cnt INTEGER NOT NULL DEFAULT 0")
        if "origin_project" not in columns:
            self._conn.execute(
                "ALTER TABLE event ADD COLUMN origin_project TEXT")
        if "summarized_at" not in columns:
            self._conn.execute(
                "ALTER TABLE event ADD COLUMN summarized_at TEXT")
        if "needs_review" not in columns:
            self._conn.execute(
                "ALTER TABLE event ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
        if "review_reason" not in columns:
            self._conn.execute(
                "ALTER TABLE event ADD COLUMN review_reason TEXT")
        if "last_used_at" not in columns:
            self._conn.execute(
                "ALTER TABLE event ADD COLUMN last_used_at TEXT")
        # 经验补 scope（适用范围）：老库按「跨项目可见即通用」回填
        exp_columns = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(experience)")}
        if "scope" not in exp_columns:
            self._conn.execute(
                "ALTER TABLE experience ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'")
            self._conn.execute(
                "UPDATE experience SET scope = 'global' WHERE origin_project IS NULL")
        self._ensure_supersede_indexes()
        self._ensure_fts_scope_columns()
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self._normalize_timestamps()

    def _ensure_supersede_indexes(self):
        '''
        生命周期不变量：同一旧行至多一个 active 后继（supersede 链不分叉）。
        老库可能已有重复数据，建索引会失败——此时降级为「仅应用层守卫」并告警，
        不能让索引挡住进程启动。
        '''
        for table in ("experience", "cognition"):
            name = f"idx_{table[:3]}_superseded"
            try:
                self._conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                    f"ON {table}(superseded_id) WHERE superseded_id IS NOT NULL")
            except sqlite3.IntegrityError as exc:
                base.log.warning("db.supersede_index_unavailable", table=table,
                                 error=type(exc).__name__)

    def _normalize_timestamps(self):
        '''
        一次性把历史 occur_at 归一成 UTC（user_version 0 → 1）。

        时间只在同一时区下才能按字典序比较；老库是 +08:00，外壳可能给 Z，
        混存会让 memory_recent 的区间判断整体偏移。改写后长度与格式一致。
        '''
        with self.transaction() as conn:
            for table in ("message", "memory"):
                rows = conn.execute(
                    f"SELECT rowid AS rid, occur_at FROM {table}").fetchall()
                updates = []
                for row in rows:
                    normalized = base.to_utc_iso(row["occur_at"], default="")
                    if normalized and normalized != row["occur_at"]:
                        updates.append((normalized, row["rid"]))
                if updates:
                    conn.executemany(
                        f"UPDATE {table} SET occur_at = ? WHERE rowid = ?", updates)
                    base.log.info("db.timestamps_normalized", table=table,
                                  rows=len(updates))
            conn.execute("PRAGMA user_version = 1")

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
                with self._savepoint_seq_lock:
                    self._savepoint_seq += 1
                    name = f"sp_{self._savepoint_seq}"
                self._conn.execute(f"SAVEPOINT {name}")
                try:
                    yield self._conn
                except BaseException:
                    self._rollback_to(name)
                    raise
                else:
                    self._conn.execute(f"RELEASE {name}")
                return

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._rollback_all()
                raise
            else:
                self._conn.execute("COMMIT")

    def _ensure_fts_scope_columns(self):
        '''
        memory_fts 加范围列（origin_project / session_id）。

        FTS5 虚拟表不能 ALTER ADD COLUMN，只能重建；老库在这里做一次性迁移：
        删表重建后从源表回灌（memory_fts 只存渲染后的 body，必须回源取）。
        '''
        columns = [row["name"] for row in
                   self._conn.execute("PRAGMA table_info(memory_fts)")]
        if "origin_project" in columns and "session_id" in columns:
            return
        base.log.warning("db.fts_scope_columns_missing", existing=columns)
        # DROP / CREATE / 回灌必须在同一个事务里：拆开的话，回灌中途失败会留下
        # “列已补齐但内容为空”的索引，下次启动因为列存在直接 return，永不自愈。
        with self.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS memory_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5("
                "ref_kind UNINDEXED, ref_id UNINDEXED, title, body, "
                "origin_project UNINDEXED, session_id UNINDEXED, "
                "tokenize='trigram')")
            counts = self._reindex_fts_conn(conn)
        base.log.info("db.fts_reindexed", **counts)

    def reindex_fts(self) -> dict:
        '''从源表重建全文索引，返回各类型重建条数。'''
        with self.transaction() as conn:
            return self._reindex_fts_conn(conn)

    def _reindex_fts_conn(self, conn) -> dict:
        '''reindex 的实际动作；接收连接是为了能并入调用方的事务。'''
        counts = {"event": 0, "experience": 0, "cognition": 0}
        conn.execute("DELETE FROM memory_fts")
        for row in conn.execute(
                "SELECT id, title, summary, origin_project, session_id FROM event"
        ).fetchall():
            conn.execute(
                "INSERT INTO memory_fts (ref_kind, ref_id, title, body, "
                "origin_project, session_id) VALUES ('event', ?, ?, ?, ?, ?)",
                (row["id"], row["title"], row["summary"] or "",
                 row["origin_project"], row["session_id"]))
            counts["event"] += 1
        # 只回灌仍在生效的 l3：superseded / invalid 的索引在失效时就删过，
        # 全量回灌会把它们重新塞回召回窗口，占掉 top-k 名额。
        for table in ("experience", "cognition"):
            for row in conn.execute(
                    f"SELECT id, content, origin_project, origin_session "
                    f"FROM {table} WHERE status = 'active'").fetchall():
                conn.execute(
                    "INSERT INTO memory_fts (ref_kind, ref_id, title, body, "
                    "origin_project, session_id) VALUES (?, ?, '', ?, ?, ?)",
                    (table, row["id"], row["content"],
                     row["origin_project"], row["origin_session"]))
                counts[table] += 1
        return counts

    def _rollback_to(self, name: str):
        '''回滚到保存点；回滚自身失败不能掩盖原始异常。'''
        try:
            self._conn.execute(f"ROLLBACK TO {name}")
            self._conn.execute(f"RELEASE {name}")
        except sqlite3.Error as exc:
            base.log.error("db.rollback_failed", savepoint=name,
                           error=type(exc).__name__)

    def _rollback_all(self):
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            base.log.error("db.rollback_failed", error=type(exc).__name__)

    # ---- 写入：记忆 ----

    def insert_memory(self, item: Memory) -> bool:
        '''
        插入一条引用；message_id 已存在返回 False（幂等）。
        seq 撞车时也只返回 False 并告警——正文层面的冲突由 insert_message 报告。
        '''
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO memory "
                    "(session_id, message_id, owner, seq, occur_at, created_at, event_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, message_id) DO NOTHING",
                    (item.session_id, item.message_id, item.owner, item.seq,
                     item.occur_at, item.created_at, item.event_id))
                return cur.rowcount == 1
            except sqlite3.IntegrityError as exc:
                base.log.warning("memory.seq_conflict", session_id=item.session_id,
                                 message_id=item.message_id, seq=item.seq,
                                 error=type(exc).__name__)
                return False

    def bind_event(self, session_id: str, message_id: str, event_id: int,
                   strict: bool = False) -> bool:
        '''
        把记忆行归入事件。
        :param strict: True 只绑定尚未归属的消息（event_id IS NULL）；已归属其他
                       事件时不改绑、返回 False（事件闭合后正文不再追加）
        :return: 发生绑定返回 True；行不存在、已归属同一事件、或 strict 下已归属
                 其他事件返回 False
        '''
        with self._lock:
            if strict:
                cur = self._conn.execute(
                    "UPDATE memory SET event_id = ? "
                    "WHERE session_id = ? AND message_id = ? AND event_id IS NULL",
                    (event_id, session_id, message_id))
            else:
                cur = self._conn.execute(
                    "UPDATE memory SET event_id = ? "
                    "WHERE session_id = ? AND message_id = ? "
                    "AND (event_id IS NULL OR event_id != ?)",
                    (event_id, session_id, message_id, event_id))
            return cur.rowcount == 1

    def bound_event_of(self, session_id: str, message_id: str) -> int | None:
        '''消息当前归属的事件 id（含已归属的）；未归属返回 None。'''
        row = self._conn.execute(
            "SELECT event_id FROM memory WHERE session_id = ? AND message_id = ?",
            (session_id, message_id)).fetchone()
        if row is None or row["event_id"] is None:
            return None
        return int(row["event_id"])

    # ---- 写入：消息 ----

    def insert_message(self, item: Message) -> int:
        '''
        插入一条消息。
        :return: 1 新插入；0 message_id 已存在（幂等重复）；-1 seq 撞车（不同
                 message_id 占用同一 seq，正文未写入，调用方必须告警）
        '''
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO message "
                    "(session_id, message_id, owner, seq, role, content, occur_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, message_id) DO NOTHING",
                    (item.session_id, item.message_id, item.owner, item.seq, item.role,
                     item.content, item.occur_at, item.created_at))
                return 1 if cur.rowcount == 1 else 0
            except sqlite3.IntegrityError:
                # UNIQUE(session_id, seq) 撞车：不是重复，是序号冲突
                return -1

    def insert_messages(self, items) -> tuple:
        '''
        批量插入；整批在同一事务内。
        :return: (实际新增条数, [(message_id, seq) 冲突列表])；message_id 重复的不算冲突
        '''
        inserted = 0
        conflicts = []
        with self.transaction():
            for item in items:
                rc = self.insert_message(item)
                if rc == 1:
                    inserted += 1
                elif rc == -1:
                    conflicts.append((item.message_id, item.seq))
        return inserted, conflicts

    # ---- 写入：事件 ----

    def create_event(self, session_id: str, title: str,
                     summary: str | None = None,
                     related_events=None,
                     origin_project: str | None = None) -> Event:
        '''
        新建事件并维护同会话双向链：取同会话最大 id 作 prev，回填上一条的 next。
        三步在同一事务内完成。origin_project=None 表示全局可见。
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
                "(session_id, title, summary, created_at, prev_event_id, next_event_id, "
                "related_events, origin_project) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
                (session_id, title, summary, created_at, prev_id,
                 json.dumps(related) if related else None, origin_project))
            event_id = cur.lastrowid

            if prev_id is not None:
                conn.execute("UPDATE event SET next_event_id = ? WHERE id = ?",
                             (event_id, prev_id))

        event = self.get_event(event_id)
        if event is None:
            raise RuntimeError(f"create_event 后读取失败: event_id={event_id}")
        return event

    def set_event_summary(self, event_id: int, summary: str | None,
                          summarized_at: str | None = None) -> bool:
        '''
        补充/覆盖事件摘要（doc：summary 可后续补充）；传 None 表示清空。
        :param summarized_at: 摘要时间；缺省表示不改该列（重跑阈值靠它判断新近度）
        '''
        with self._lock:
            if summarized_at is None:
                cur = self._conn.execute(
                    "UPDATE event SET summary = ? WHERE id = ?", (summary, event_id))
            else:
                cur = self._conn.execute(
                    "UPDATE event SET summary = ?, summarized_at = ? WHERE id = ?",
                    (summary, summarized_at, event_id))
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
        now = base.get_cur_time()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE event SET cnt = cnt + ?, last_used_at = ? WHERE id = ?",
                (amount, now, event_id))
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
        now = base.get_cur_time()
        with self.transaction() as conn:
            cur = conn.executemany(
                "UPDATE event SET cnt = cnt + ?, last_used_at = ? WHERE id = ?",
                [(amount, now, event_id) for event_id in ids])
            return cur.rowcount

    def set_event_review(self, event_id: int, needs_review: bool,
                         reason: str | None = None) -> bool:
        '''标记/清除事件的复核状态（摘要失败或条目降级时置位）。'''
        with self._lock:
            cur = self._conn.execute(
                "UPDATE event SET needs_review = ?, review_reason = ? WHERE id = ?",
                (1 if needs_review else 0, reason, event_id))
            return cur.rowcount == 1

    def list_events_needing_review(self, session_id: str | None = None,
                                   limit: int = 50) -> list[Event]:
        '''待复核事件：重启后仍能被找到（状态落库，不只存在回执里）。'''
        if session_id is None:
            rows = self._conn.execute(
                "SELECT * FROM event WHERE needs_review = 1 "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM event WHERE needs_review = 1 AND session_id = ? "
                "ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
        return [Event.from_row(row) for row in rows]

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

    def last_bound_seq(self, session_id: str) -> int | None:
        '''
        该会话里「已归属某个事件」的最大消息序号；None 表示还没切分过。

        增量切分的起点：序号之后的消息才需要重新判定，之前的一律复用已绑定事件。
        '''
        row = self._conn.execute(
            "SELECT MAX(m.seq) AS last_seq FROM memory r "
            "JOIN message m ON m.session_id = r.session_id "
            " AND m.message_id = r.message_id "
            "WHERE r.session_id = ? AND r.event_id IS NOT NULL",
            (session_id,)).fetchone()
        if row is None or row["last_seq"] is None:
            return None
        return int(row["last_seq"])

    def message_at_seq(self, session_id: str, seq: int) -> Message | None:
        '''取指定 seq 的消息（增量切分的锚点）。'''
        row = self._conn.execute(
            "SELECT * FROM message WHERE session_id = ? AND seq = ?",
            (session_id, seq)).fetchone()
        return Message.from_row(row) if row else None

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

        比较走 SQLite 的 `datetime()`：它认识 ISO 8601 的时区后缀并换算到 UTC，
        所以老库的 +08:00 与新写入的 +00:00 混存时也不会错 8 小时。
        '''
        rows = self._conn.execute(
            "SELECT e.*, t.first_at AS first_at, t.last_at AS last_at FROM event e "
            "JOIN (SELECT r.event_id AS eid, MIN(m.occur_at) AS first_at, "
            "             MAX(m.occur_at) AS last_at "
            "      FROM memory r JOIN message m "
            "        ON m.session_id = r.session_id AND m.message_id = r.message_id "
            "      WHERE r.event_id IS NOT NULL AND r.session_id = ? "
            "      GROUP BY r.event_id) t ON t.eid = e.id "
            "WHERE e.session_id = ? "
            "  AND datetime(t.last_at) >= datetime(?) "
            "  AND datetime(t.first_at) <= datetime(?) "
            "ORDER BY datetime(t.last_at) DESC LIMIT ?",
            (session_id, session_id, from_at, to_at, limit)).fetchall()
        return [EventSpan(event=Event.from_row(row),
                          first_at=row["first_at"], last_at=row["last_at"])
                for row in rows]

    # ---- 写入：l3（经验 / 认知） ----

    def _l3_columns(self, table: str) -> tuple:
        '''表 → 写入列；scope 只属于经验，认知没有这一列。'''
        columns = (_L3_EXTRA[table],) + _L3_COLUMNS
        if table == "experience":
            columns = columns + ("scope",)
        return columns

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
        if table == "experience":
            values.append(getattr(item, "scope", "project"))
        return tuple(values)

    def _l3_insert(self, conn, table: str, item) -> int:
        columns = self._l3_columns(table)
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

    def get_insight(self, item_id: int) -> Insight | None:
        row = self._conn.execute(
            "SELECT * FROM insight WHERE id = ?", (item_id,)).fetchone()
        return Insight.from_row(row) if row else None

    def insert_insight(self, item: Insight) -> int:
        '''写入一条洞见（归纳层），返回新 id。'''
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO insight (content, scope, confidence, importance, "
                "origin_session, origin_project, source_experience_ids, cues, "
                "use_count, last_used_at, created_at, status, superseded_id, "
                "valid_to, invalid_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.content, item.scope, item.confidence, item.importance,
                 item.origin_session, item.origin_project,
                 json.dumps(list(item.source_experience_ids), ensure_ascii=False)
                 if item.source_experience_ids else None,
                 json.dumps(list(item.cues), ensure_ascii=False) if item.cues else None,
                 item.use_count, item.last_used_at,
                 item.created_at or base.get_cur_time(), item.status,
                 item.superseded_id, item.valid_to, item.invalid_reason))
            return int(cur.lastrowid)

    def list_insights(self, *, origin_project: str | None = None,
                      status: str = "active", limit: int = 50,
                      include_expired: bool = False) -> list:
        clauses, params = ["status = ?"], [status]
        if not include_expired:
            clauses.append("(valid_to IS NULL OR valid_to > ?)")
            params.append(base.get_cur_time())
        if origin_project is not None:
            clauses.append("(origin_project IS NULL OR origin_project = ?)")
            params.append(origin_project)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM insight WHERE {' AND '.join(clauses)} "
            f"ORDER BY importance DESC, id DESC LIMIT ?", params).fetchall()
        return [Insight.from_row(row) for row in rows]

    def insert_self_memory(self, item: SelfMemory) -> int:
        '''写入一条自我认知，返回新 id。'''
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO self_memory (source, kind, content, revision, "
                "origin_session, created_at, status, superseded_id, valid_to, "
                "invalid_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.source, item.kind, item.content, item.revision,
                 item.origin_session, item.created_at or base.get_cur_time(),
                 item.status, item.superseded_id, item.valid_to,
                 item.invalid_reason))
            return int(cur.lastrowid)

    def get_self_memory(self, item_id: int) -> SelfMemory | None:
        row = self._conn.execute(
            "SELECT * FROM self_memory WHERE id = ?", (item_id,)).fetchone()
        return SelfMemory.from_row(row) if row else None

    def list_self_memory(self, *, source: str | None = None,
                         status: str = "active", limit: int = 50,
                         include_expired: bool = False) -> list:
        clauses, params = ["status = ?"], [status]
        if not include_expired:
            clauses.append("(valid_to IS NULL OR valid_to > ?)")
            params.append(base.get_cur_time())
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM self_memory WHERE {' AND '.join(clauses)} "
            f"ORDER BY revision DESC, id DESC LIMIT ?", params).fetchall()
        return [SelfMemory.from_row(row) for row in rows]

    def get_many(self, table: str, ids) -> dict:
        '''
        批量按 id 取行：{id: row}。检索层逐条取数的 N+1 改这里一次 IN 查询。
        :param table: event / experience / cognition
        '''
        if table != "event" and table not in _L3_EXTRA and table != "insight":
            raise ValueError(f"不支持的表: {table!r}")
        wanted = [int(x) for x in ids]
        if not wanted:
            return {}
        model = {"event": Event, "experience": Experience,
                 "cognition": Cognition, "insight": Insight}[table]
        marks = ", ".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE id IN ({marks})", wanted).fetchall()
        return {row["id"]: model.from_row(row) for row in rows}

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
        '''召回合并候选（active、未过期）：主体包含或内容 LIKE。

        subject 用包含匹配而不是等值匹配：同一事物的写法会分叉
        （「dsh-longmemory-plugin」/「dsh-longmemory-plugin 项目」/「记忆插件」），
        等值匹配下这些条目互相召回不到，同义条目只会不断新增。
        '''
        if table not in _L3_EXTRA:
            raise ValueError(f"不支持的 l3 表: {table!r}")
        model = Experience if table == "experience" else Cognition
        clauses = ["status = 'active'", "(valid_to IS NULL OR valid_to > ?)"]
        params: list = [base.get_cur_time()]
        if origin_project is not None:
            clauses.append("(origin_project IS NULL OR origin_project = ?)")
            params.append(origin_project)
        if subject and query:
            # 双向包含：新写法包含旧写法，或旧写法包含新写法，都算同一事物
            clauses.append("(subject LIKE ? ESCAPE '\\' "
                           "OR ? LIKE '%' || subject || '%' ESCAPE '\\' "
                           "OR content LIKE ? ESCAPE '\\')")
            escaped = _like_escape(subject)
            params.extend([f"%{escaped}%", escaped, f"%{_like_escape(query)}%"])
        elif subject:
            clauses.append("(subject LIKE ? ESCAPE '\\' "
                           "OR ? LIKE '%' || subject || '%' ESCAPE '\\')")
            escaped = _like_escape(subject)
            params.extend([f"%{escaped}%", escaped])
        elif query:
            clauses.append("content LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(query)}%")
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY importance DESC, id DESC LIMIT ?", params).fetchall()
        return [model.from_row(row) for row in rows]

    def _list_l3(self, table: str, model, *, origin_project: str | None = None,
                 status: str = "active", kind: str | None = None,
                 limit: int = 50, include_expired: bool = False,
                 created_from: str | None = None,
                 created_to: str | None = None) -> list:
        '''
        按条件取 l3。
        :param created_from/created_to: 入库时间闭区间，**下推到 SQL**——
               旧实现先按 id 取 top-N 再在 Python 里按时间筛，窗口内条目会被
               挤出 top-N 而取不到；比较走 datetime()，兼容不同时区后缀
        '''
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
        if created_from is not None:
            clauses.append("datetime(created_at) >= datetime(?)")
            params.append(created_from)
        if created_to is not None:
            clauses.append("datetime(created_at) <= datetime(?)")
            params.append(created_to)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY id DESC LIMIT ?", params).fetchall()
        return [model.from_row(row) for row in rows]

    def list_experiences(self, **kwargs) -> list:
        '''按条件取经验；origin_project 给定时包含通用条目（origin_project IS NULL）。'''
        return self._list_l3("experience", Experience, **kwargs)

    def list_l3_by_event(self, table: str, event_id: int) -> list:
        '''按来源事件取 l3（向量索引/回填用）。'''
        if table == "experience":
            model = Experience
        elif table == "cognition":
            model = Cognition
        else:
            raise ValueError(f"不支持的表: {table!r}")
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE source_event_id = ?", (int(event_id),)).fetchall()
        return [model.from_row(row) for row in rows]

    def list_cognitions(self, **kwargs) -> list:
        return self._list_l3("cognition", Cognition, **kwargs)

    def _supersede_l3(self, table: str, old_id: int, item) -> int | None:
        '''
        用新条目取代旧条目：新行 superseded_id=old，旧行 status='superseded'。
        :return: 新条目 id；旧行不存在抛 KeyError；旧行已不是 active 返回 None
                 （调用方应降级为「新增」，不得炸掉整个事件事务）
        '''
        now = base.get_cur_time()
        with self.transaction() as conn:
            old = conn.execute(
                f"SELECT status FROM {table} WHERE id = ?", (old_id,)).fetchone()
            if old is None:
                raise KeyError(f"{table} {old_id} 不存在")
            if old["status"] != "active":
                return None
            new_item = replace(item, superseded_id=old_id, status="active",
                               created_at=item.created_at or now)
            new_id = self._l3_insert(conn, table, new_item)
            cur = conn.execute(
                f"UPDATE {table} SET status = 'superseded', valid_to = ? "
                f"WHERE id = ? AND status = 'active'",
                (now, old_id))
            if cur.rowcount != 1:
                # 状态在读取后被改动：回滚本次取代，交给调用方降级为新增
                raise _StaleTarget(old_id)
            # 失效条目退出全文索引，否则会一直占用召回名额（同事务，不留残影）
            conn.execute("DELETE FROM memory_fts WHERE ref_kind = ? AND ref_id = ?",
                         (table, old_id))
            self._vec_remove(conn, table, old_id)
            return new_id

    def supersede_experience(self, old_id: int, item: Experience) -> int | None:
        try:
            return self._supersede_l3("experience", old_id, item)
        except _StaleTarget:
            return None

    def supersede_cognition(self, old_id: int, item: Cognition) -> int | None:
        try:
            return self._supersede_l3("cognition", old_id, item)
        except _StaleTarget:
            return None

    def _invalidate_l3(self, table: str, item_id: int, reason: str) -> bool:
        '''失效（无后继）：只改状态与原因，不删行、不改 content。'''
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET status = 'invalid', invalid_reason = ?, valid_to = ? "
                f"WHERE id = ? AND status = 'active'",
                (reason, base.get_cur_time(), item_id))
            if cur.rowcount != 1:
                return False
            conn.execute("DELETE FROM memory_fts WHERE ref_kind = ? AND ref_id = ?",
                         (table, item_id))
            self._vec_remove(conn, table, item_id)
            return True

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

    def touch_insights(self, ids, by: int = 1) -> int:
        return self._touch_l3("insight", ids, by)

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
                  body: str = "", origin_project: str | None = None,
                  session_id: str | None = None) -> None:
        '''
        写入/覆盖一条全文索引；ref_kind 取 event / experience / cognition。

        范围列（origin_project / session_id）只存不索引：召回时先按它过滤，
        避免范围外条目占满 top-k 名额（过滤逻辑仍由检索层 `_eligible` 兜底）。
        '''
        if ref_kind not in _L3_EXTRA and ref_kind not in ("event", "insight"):
            raise ValueError(f"不支持的 ref_kind: {ref_kind!r}")
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM memory_fts WHERE ref_kind = ? AND ref_id = ?",
                (ref_kind, int(ref_id)))
            conn.execute(
                "INSERT INTO memory_fts (ref_kind, ref_id, title, body, "
                "origin_project, session_id) VALUES (?, ?, ?, ?, ?, ?)",
                (ref_kind, int(ref_id), title or "", body or "",
                 origin_project, session_id))

    def search_fts(self, query: str, limit: int = 20, *,
                   origin_project: str | None = None,
                   restrict_project: bool = False,
                   session_id: str | None = None) -> list[dict]:
        '''
        全文检索；trigram 需要 ≥3 字符，更短的查询或语法错误退回 LIKE。

        :param restrict_project: True 时在**召回前**按范围列过滤（本项目 + 通用）；
                                 False 表示跨项目（scope=all）
        :param session_id: 限定会话（事件按来源会话、l3 按 origin_session）
        :param origin_project: 当前项目；restrict_project 且为 None 时只剩通用条目

        LIKE 回退的两个细节（中文 2 字查询是热路径）：
        1. `%` / `_` 必须转义，否则查询本身会被当成通配符；
        2. 没有 bm25 排名，改按「标题命中优先、正文更短优先」排序。
        '''
        q = (query or "").strip()
        if not q:
            return []

        clauses, params = [], []
        if restrict_project:
            clauses.append("(origin_project IS NULL OR origin_project = ?)")
            params.append(origin_project)
        if session_id is not None:
            clauses.append("(ref_kind != 'event' OR session_id = ?)")
            params.append(session_id)
        extra = (" AND " + " AND ".join(clauses)) if clauses else ""

        # 查询级联：整串当短语会让自然语言查询必然召回 0
        # （"用户之前让我做的事 任务 项目 自由猫" 要求这些词在文档里连续出现）。
        # 顺序：全部长词 AND → 长词 OR → 全部词 LIKE(AND) → 全部词 LIKE(OR)，
        # 逐级放宽，先精确后召回。
        tokens = q.split() or [q]
        long_tokens = [token for token in tokens if len(token) >= 3]

        def fts(match: str):
            try:
                return self._conn.execute(
                    "SELECT ref_kind, ref_id, title, body, origin_project, "
                    "session_id, bm25(memory_fts) AS rank "
                    "FROM memory_fts WHERE memory_fts MATCH ?" + extra +
                    " ORDER BY rank LIMIT ?",
                    [match, *params, limit]).fetchall()
            except sqlite3.OperationalError:
                return []

        def like(mode_and: bool):
            conditions, flat = [], []
            for token in tokens:
                pattern = f"%{_like_escape(token)}%"
                conditions.append(
                    "(body LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')")
                flat.extend([pattern, pattern])
            joiner = " AND " if mode_and else " OR "
            head = f"%{_like_escape(tokens[0])}%"
            return self._conn.execute(
                "SELECT ref_kind, ref_id, title, body, origin_project, "
                "session_id, 0.0 AS rank FROM memory_fts WHERE "
                + joiner.join(conditions) + extra +
                " ORDER BY (title LIKE ? ESCAPE '\\') DESC, length(body) ASC LIMIT ?",
                [*flat, *params, head, limit]).fetchall()

        quoted = ['"' + token.replace('"', '""') + '"' for token in long_tokens]
        rows = fts(" AND ".join(quoted)) if quoted else []
        if not rows and len(quoted) > 1:
            rows = fts(" OR ".join(quoted))
        if not rows:
            rows = like(mode_and=True)
        if not rows and len(tokens) > 1:
            rows = like(mode_and=False)
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

    def count_l3(self, table: str, session_id: str | None = None) -> int:
        '''统计经验/认知/洞见条数（含历史行，按来源会话可选过滤）。'''
        if table not in _L3_EXTRA and table != "insight":
            raise ValueError(f"不支持的 l3 表: {table!r}")
        if session_id is None:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        else:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE origin_session = ?",
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

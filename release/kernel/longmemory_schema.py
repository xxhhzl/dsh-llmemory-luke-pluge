# -*- coding: utf-8 -*-
"""数据库 DDL：与行映射和查询逻辑分离，迁移时集中核对。"""

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
  last_used_at   TEXT,                       -- 最近一次被召回/再认的时间
  valid_to       TEXT                        -- 失效时间；非空且已过则退出召回
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
  invalid_reason  TEXT,
  feedback_score  INTEGER NOT NULL DEFAULT 0   -- 反馈分：useless 累计降权，不删条目
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
  invalid_reason  TEXT,
  feedback_score  INTEGER NOT NULL DEFAULT 0   -- 反馈分：useless 累计降权，不删条目
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
  invalid_reason       TEXT,
  feedback_score       INTEGER NOT NULL DEFAULT 0
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

-- 原文索引：摘要常漏掉合同号、编号、原话，这些细节必须另有检索入口。
-- 独立成表而不并进 memory_fts：事件 body 会被反复重写，原文只写一次；
-- 检索命中后经 memory(event_id) 回连到所属事件。
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
  session_id UNINDEXED,
  message_id UNINDEXED,
  body,
  tokenize='trigram'
);
"""

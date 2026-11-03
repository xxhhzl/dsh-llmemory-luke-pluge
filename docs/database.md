## 记忆表（纯引用）

```sql
CREATE TABLE memory (
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
```

七列，没有代理主键 id，`(session_id, message_id)` 本身就是唯一键。

| 字段 | 来源 | 作用 | 为什么在记忆表 |
|---|---|---|---|
| session_id | 宿主 | 归属、切分范围 | 本地查询的过滤条件 |
| message_id | 宿主 | 指向消息、幂等去重 | 引用本体 |
| owner | 宿主 | 归属者名称，按人或 agent 过滤 | 查"某人说过什么"不该回宿主捞 |
| seq | 宿主 | 会话内顺序、增量游标 | 切分与游标都靠它 |
| occur_at | 宿主 | 发生时间、间隔判定、时间范围查 | 宿主侧 join 不了（消息不在本库），必须冗余 |
| created_at | 本地 | 入库时间、全局排序 | 本地生成，单调 |
| event_id | 本地 | 归属事件 | 避免用范围条件 join 段表 |

## 消息表（正文）

```sql
CREATE TABLE message (
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
```

八列，`(session_id, message_id)` 为唯一键。

| 字段 | 来源 | 作用 |
|---|---|---|
| session_id | 宿主 | 归属会话 |
| message_id | 宿主 | 消息 id，与记忆表一一对应 |
| owner | 宿主 | 归属者名称；工具消息填调用该工具的 agent 名称 |
| seq | 宿主 | 会话内顺序，与记忆表一致 |
| role | 宿主 | user / assistant / tool / system，切分规则要用 |
| content | 宿主 | 正文；工具消息为 `{"call": {}, "result": {}}` |
| occur_at | 宿主 | 消息发生时间 |
| created_at | 本地 | 入库时间 |

## 事件表

```sql
CREATE TABLE event (
  id             INTEGER PRIMARY KEY,
  session_id     TEXT    NOT NULL,
  title          TEXT    NOT NULL,
  summary        TEXT,
  created_at     TEXT    NOT NULL,
  prev_event_id  INTEGER,
  next_event_id  INTEGER,
  related_events TEXT,
  cnt            INTEGER NOT NULL DEFAULT 0,
  origin_project TEXT,
  summarized_at  TEXT,
  needs_review   INTEGER NOT NULL DEFAULT 0,
  review_reason  TEXT
);
```

十三列。事件没有状态字段：一次 `/memory` 执行完毕即视为该批事件全部归档。

| 字段 | 来源 | 作用 |
|---|---|---|
| id | 本地 | 事件主键，自增 |
| session_id | 本地 | 所属会话 |
| title | 模型 | 一句话标题，切分过程中即时生成 |
| summary | 模型 | 事件摘要，可后续补充 |
| created_at | 本地 | 事件创建时间 |
| prev_event_id | 本地 | 同会话上一个事件，无条件指向，表示顺序 |
| next_event_id | 本地 | 同会话下一个事件，无条件指向，表示顺序 |
| related_events | 模型 | JSON 数组，存语义相关的事件 id，如 `[12, 15]` |
| cnt | 本地 | 使用计数：新建为 0，事件每被使用/召回一次 +1，供查询排序（热度） |
| origin_project | 本地 | 归属项目；NULL = 全局可见。范围是**数据的固有属性**，不是检索参数 |
| summarized_at | 本地 | 上次摘要时间；NULL = 尚未摘要。已闭合事件被续写时据此判断是否补跑调用 A |
| needs_review / review_reason | 本地 | 复核状态：摘要失败记 `summary_failed`，条目降级记 `needs_review`；`list_events_needing_review` 可查。状态落库，重启后不丢 |
| last_used_at | 本地 | 最近一次被召回/再认的时间；ACT-R 激活的新近项读它（老库 NULL 时退化到 created_at） |

- `prev_event_id` / `next_event_id` 构成同会话的双向链：`e10 ⇄ e11 ⇄ e12`。
- 插入新事件时，取同会话最大 id 作为 `prev`，并回填上一条的 `next`，三步放在同一事务内。
- 不论消息多少都建事件，没有最小长度限制。
- `related_events` 为 JSON，不建索引；事件量增大后若要按关联查询，再拆出关联表。
- `cnt` 用单条 `UPDATE event SET cnt = cnt + 1` 原子自增（`Database.bump_event_cnt`），不做读改写；
  `list_events(order_by="cnt")` 可按热度取事件，默认仍按 `id` 的时间序。

## 经验表（程序记忆：怎么做）

```sql
CREATE TABLE experience (
  id              INTEGER PRIMARY KEY,
  context         TEXT,                       -- 触发条件
  content         TEXT    NOT NULL,           -- 做法（≤200 字符）
  kind            TEXT    NOT NULL DEFAULT 'technique',
  confidence      REAL    NOT NULL DEFAULT 0.8,
  importance      INTEGER NOT NULL DEFAULT 3,
  origin_session  TEXT,
  origin_project  TEXT,
  source_event_id INTEGER NOT NULL,
  cues            TEXT,                       -- JSON 数组
  use_count       INTEGER NOT NULL DEFAULT 0,
  last_used_at    TEXT,
  created_at      TEXT    NOT NULL,
  status          TEXT    NOT NULL DEFAULT 'active',
  superseded_id   INTEGER,
  valid_to        TEXT,
  invalid_reason  TEXT
);
```

| 字段 | 人类记忆对应 | 作用 |
|---|---|---|
| context | 条件侧（触发线索） | 什么情形下适用；缺了它经验会变成无条件建议 |
| content | 程序（做法） | 一句话做法：改哪里、跑什么命令、避开什么 |
| kind | 程序子类 | `technique` / `pitfall` / `command` |
| confidence | 可靠性 | 被验证过的程度（做得成），与认知的「信得过」同名不同义 |
| importance | 显著性 | 排序乘子，默认 3 |
| origin_session / origin_project | 源记忆 | 从哪次会话、哪个项目学到的；`origin_project` 为 NULL 表示通用 |
| source_event_id | 情景锚点 | 具体事件 id，回答「凭什么」 |
| cues | 编码特异性线索 | 检索线索：工具名、报错码、技术栈 |
| use_count / last_used_at | 练习、提取强度 | 召回次数与最近召回时间，用于遗忘衰减 |
| status / superseded_id / valid_to / invalid_reason | 消退、再学习 | 生命周期；只追加，不删行 |

## 认知表（语义记忆：是什么）

```sql
CREATE TABLE cognition (
  id              INTEGER PRIMARY KEY,
  subject         TEXT    NOT NULL,           -- 命题主体
  content         TEXT    NOT NULL,           -- 事实（≤200 字符）
  kind            TEXT    NOT NULL DEFAULT 'fact',
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
```

与经验表只有三处不同：`subject`（主体）对应 `context`（触发条件）、`kind` 取 `fact / preference / constraint`、`confidence` 表示**确信度**（信不信）而非可靠性（做不做得成）。

其余字段含义与经验表一致；两表共用列，检索时用 `UNION ALL` 合成一个视图。

## 工作记忆表（digest，缓存）

```sql
CREATE TABLE digest (
  session_id    TEXT PRIMARY KEY,
  content       TEXT NOT NULL,
  ref_event_ids TEXT,             -- JSON 数组
  revision      INTEGER NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL
);
```

**这是缓存不是事实源**：允许 `UPSERT` 覆盖写，`revision` 每次 +1；丢了可以从事件摘要重建，不承担审计职责。

## 全文索引（memory_fts）

```sql
CREATE VIRTUAL TABLE memory_fts USING fts5(
  ref_kind UNINDEXED,   -- event / experience / cognition
  ref_id   UNINDEXED,
  title,
  body,
  tokenize='trigram'
);
```

- `trigram` 支持中文子串（`数据库`、`SQLite` 都能命中）；查询短于 3 字符时退回 `LIKE` 扫描。
- 写入与原表**同一事务**：`DELETE ... WHERE ref_kind=? AND ref_id=?` 再 `INSERT`，天然幂等。
- 不回填历史数据：事件摘要补充（`set_event_summary`）时重新索引一次即可。

## 写入链路（ingest）

`src/ingest.py` 把一次摘要结果写进库，顺序固定：

```
BEGIN
  1. event：新建（或复用切分器建好的 id）并写入标题/摘要
  2. experience / cognition：按 source_event_id + content 幂等插入；
     若带调用 B 的裁决，则按 new / supersede / invalidate 执行（新行、或旧行置 superseded/invalid）
  3. memory_fts：event（title+summary）、每条 l3（content）
  4. digest：最近 N 个事件 + 重要度 top3 的经验/认知，重新拼装
COMMIT
```

- **失败口径**：摘要 `ok=False` 时只写事件（`summary=NULL`、标题保留），**不写 l3**，避免出现没有摘要的孤儿条目。
- **幂等**：同一 `source_event_id` 下内容相同的条目直接复用旧 id，调用 B 重跑或事件重放不会产生重复行。
- **digest 来源**：`ref_event_ids` 按时间升序存，注入时按顺序拼接。
- **检索范围**：FTS 只索引 `content`（正文），`cues` 是候选召回的线索，不进全文索引。

## 可见范围：不设在表里

人类记忆没有「可见范围」，项目隔离是检索策略，不是记忆属性。因此 l3 两表**不设 `scope` / `project_id`**，只存来源 `origin_project`（不可变）；检索时按

```sql
(origin_project IS NULL OR origin_project = :current_project)
```

过滤：通用知识永远可见，项目知识只在本项目可见。跨项目检索需显式放开。

## 表关系

```
memory(session_id, message_id)  ──1:1──▶  message(session_id, message_id)
       引用与归属                              正文

memory.event_id  ──N:1──▶  event.id
event.prev_event_id  ⇄  event.next_event_id     （同会话双向链）
event.related_events  ──▶  JSON 数组，语义关联

experience.source_event_id  ──N:1──▶  event.id   （经验可追溯到来源事件）
cognition.source_event_id   ──N:1──▶  event.id   （认知可追溯到来源事件）
experience.superseded_id    ──N:1──▶  experience.id（取代链，旧行保留）
cognition.superseded_id     ──N:1──▶  cognition.id

memory_fts(ref_kind, ref_id, origin_project, session_id)
                              ──▶  event / experience / cognition（多态索引 + 范围列）
digest.ref_event_ids          ──▶  JSON 数组（缓存拼装来源）
```

- 删除消息时需在同一事务内删除对应记忆行，否则产生悬空引用。
- 删除事件时，需把相关记忆行的 `event_id` 置空，或连同消息一并删除，避免悬空。
- 记忆表冗余 `occur_at`，是为了不 join 消息表也能按时间筛选。
- l3 两表**不删除**：失效走 `status='invalid'`，被替代走 `status='superseded'` + `superseded_id`，正文永远保留。
- **取代链不分叉**：`superseded_id` 上有部分唯一索引（`WHERE superseded_id IS NOT NULL`），
  同一旧行至多一个后继。旧行已非 `active` 时 `supersede_*` 返回 `None`，调用方降级为新增
  （模型给出过期 target 不得回滚整个事件）；老库有重复数据时索引降级为仅应用层守卫并告警。
- **绑定不改写历史**：`bind_event(..., strict=True)` 只绑定 `event_id IS NULL` 的消息；
  已归属其他事件的正文不再改绑，冲突记 `ingest.bind_conflict` 日志——事件闭合后正文不追加。
- **FTS 范围列**：`memory_fts` 除 `ref_kind/ref_id/title/body` 外还存 `origin_project` /
  `session_id`（UNINDEXED，只存不索引），检索层用它在**召回前**过滤；FTS5 不支持 ADD
  COLUMN，老库打开时由 `_ensure_fts_scope_columns` 重建并从源表回灌（`reindex_fts`）。
- **经验泛化晋升**：`ingest.promote_experiences(db, min_use=3)` 把反复使用的项目经验转成
  通用条目——新行 `origin_project=NULL` + 取代旧行（旧行保留），正文不变；FTS 同步更新。
  是否自动触发（例如每周巩固）尚未定，当前作为显式维护 API。
- **消息写入同事务**：一条消息的 `message` 正文与 `memory` 引用在同一事务内写入；
  `UNIQUE(session_id, seq)` 撞车（不同 `message_id` 抢同一 seq）计为 `seq_conflicts`
  并告警，不再被 `INSERT OR IGNORE` 静默计成「重复」。

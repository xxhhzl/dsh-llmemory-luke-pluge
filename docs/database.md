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
  review_reason  TEXT,
  last_used_at   TEXT
);
```

十四列。事件没有状态字段：一次 `/memory` 执行完毕即视为该批事件全部归档。

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
  scope           TEXT    NOT NULL DEFAULT 'project',  -- global / project
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
| scope | 适用范围 | `global`（换项目仍成立）/ `project`（依赖本项目结构、配置或流程）；模型在提取时判断，缺省 `project` |
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

## 洞见表（归纳层：多条经验提炼的规则）

对应 ExpeL 的 insights（[AAAI-24, arXiv:2308.10144](https://arxiv.org/abs/2308.10144)）：
经验是「某次怎么做」，洞见是「多条经验共同支持的通用规则」。

```sql
CREATE TABLE insight (
  id                    INTEGER PRIMARY KEY,
  content               TEXT    NOT NULL,     -- 规则（≤200 字符）
  scope                 TEXT    NOT NULL DEFAULT 'global',  -- global / project
  confidence            REAL    NOT NULL DEFAULT 0.8,
  importance            INTEGER NOT NULL DEFAULT 4,
  origin_session        TEXT,
  origin_project        TEXT,
  source_experience_ids TEXT,                 -- JSON 数组：证据链（≥2 条经验）
  cues                  TEXT,                 -- JSON 数组
  use_count             INTEGER NOT NULL DEFAULT 0,
  last_used_at          TEXT,
  created_at            TEXT    NOT NULL,
  status                TEXT    NOT NULL DEFAULT 'active',
  superseded_id         INTEGER,
  valid_to              TEXT,
  invalid_reason        TEXT
);
```

| 字段 | 说明 |
|---|---|
| `content` | 一般化规则；不带项目名、版本号等一次性细节 |
| `scope` | `global` / `project`，由归纳模型判断 |
| `source_experience_ids` | **证据链**：支撑这条洞见的经验 id，至少 2 条；解析时校验必须来自本次输入，不编造 |
| 生命周期字段 | 与 l3 相同（active / superseded / invalid），但洞见不替代源经验 |

归纳是**显式离线操作**（`insight.induce()`，见 `scripts/induce_insights.py`），不挂在 ingest 热路径上：
需要同一主线索下积累 ≥3 条经验才值得调用模型；结果写库后参与检索（`memory_search` 第四类），
并在工作记忆（digest）里以 `[L4 洞见]` 优先展示。

## 自我认知表（`self_memory`）

```sql
CREATE TABLE self_memory (
  id              INTEGER PRIMARY KEY,
  source          TEXT    NOT NULL DEFAULT 'configured',  -- configured / agent
  kind            TEXT    NOT NULL DEFAULT 'identity',    -- identity/behavior/limitation/goal
  content         TEXT    NOT NULL,
  revision        INTEGER NOT NULL DEFAULT 1,             -- 每次改写 +1
  origin_session  TEXT,
  created_at      TEXT    NOT NULL,
  status          TEXT    NOT NULL DEFAULT 'active',
  superseded_id   INTEGER,
  valid_to        TEXT,
  invalid_reason  TEXT
);
```

- `source=configured`：设置的自我，进**系统提示词** `<self_configured>`；
- `source=agent`：自己写的自我，进**动态上下文** `<self_notes>`（user 快照）；
- 两者都按 `revision` 排序，取 active；注入细节见 [core_memory.md](core_memory.md)。

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
  ref_kind       UNINDEXED,   -- event / experience / cognition
  ref_id         UNINDEXED,
  title,
  body,
  origin_project UNINDEXED,   -- 范围列：召回前按项目过滤
  session_id     UNINDEXED,   -- 事件按来源会话、l3 按 origin_session
  tokenize='trigram'
);
```

- `trigram` 支持中文子串（`数据库`、`SQLite` 都能命中）；查询短于 3 字符时退回 `LIKE` 扫描。
- 写入与原表**同一事务**：`DELETE ... WHERE ref_kind=? AND ref_id=?` 再 `INSERT`，天然幂等。
- 老库没有范围列时一次性迁移：`DROP` + `CREATE` + 全量回灌放在**同一事务**里
  （`_ensure_fts_scope_columns`），中途失败整体回滚，不会留下“列已补齐但内容为空”的索引。
- 回灌只收 `status = 'active'` 的 l3：superseded / invalid 在失效时已经删过索引，
  全量回灌会把它们重新塞回召回窗口。
- 范围列写事件自身的归属（`event.origin_project`），不是本次 job 的项目——
  全局事件被某个项目续写后仍应全局可见。

## 向量索引（sqlite-vec）

```sql
CREATE VIRTUAL TABLE vec_experience USING vec0(
  ref_id          INTEGER PRIMARY KEY,   -- 对应 experience.id
  origin_project  TEXT,                  -- 范围列：KNN 里做元数据过滤
  embedding       FLOAT[1024]            -- 维度取 config.embedding.dim
);
-- vec_cognition / vec_insight 同构
```

- 扩展缺失（`sqlite_vec` 未装、加载失败、`enable_load_extension` 被禁）时**只降级**：
  `_vec_enabled=False`，读写都变成空操作，检索退回纯 FTS，主库照常工作。
- 写入走 `embed.index_rows`：整批文本打成 `embed.request` 帧交给外壳，外壳调
  OpenAI 兼容 `/v1/embeddings`（或 Ollama `/api/embed`）后按 `request_id` 回
  `embed.result`；**内核不持有端点与 key**。失败只记 `embed.vec_index_failed`。
- 向量写入发生在事件落库**之后**（`ingest._index_event`），不进摘要事务：
  embedding 端点慢或挂掉，不能连累记忆落库。
- 维度变了（换 embedding 模型）时，打开库会 `DROP` 掉旧维度的向量表并重建，
  需要 `vec.reindex` 回填；`vec.status` 会显示维度与各表条数。
- `vec.reindex` 的失败口径：`embed.index_rows` 把失败原因回调给调用方，
  连续两批**一条都没写进去**就中止并记 `reindex.error`（实测：端点没起来时
  87 条会在 16ms 内「跑完」但写入 0 条，不能算成功）；`reindex` 快照带
  `started_at` / `finished_at` / `elapsed_ms` / `done` / `total`，界面据此
  一直显示「重建中」直到 `running=false`，再切换成完成或失败。
- 失效 / 被取代的 l3 在同一事务里删掉向量（`_vec_remove`），否则会继续占
  KNN 的 top-k 名额；记忆行本身仍然保留。
- 检索：`retrieval._candidates` 把 FTS 名次与向量名次用 RRF（k=60）融合，
  `rel = 1/(60+rank)`，两路都命中的条目自然更靠前；`scope=project` 时
  过滤 `origin_project = 本项目` 再并入 `origin_project = ''`（通用条目）。
- 检索入口是 `Database.search_vectors`：优先 **ANN**（`src/ann.py`，usearch/HNSW
  进程内缓存），不可用或异常时退回 `vec_search` 的精确 KNN；两条路径都只用名次，
  不做绝对距离阈值判断。
- **向量单位化**：`vec_upsert` 与查询都经 `_vec_unit` 归一化。`vec0` 的 L2 距离与
  usearch 的 cosine 距离只有在向量等长时排序才一致；不归一化会出现
  「ANN 与精确 KNN 结论不同」的假 bug。
- ANN 增量同步：`vec_upsert` / `_vec_remove` 往 `_vec_changes[kind]` 记 id，
  ANN 下次检索时 `take_vec_changes()` 取走差量（覆盖写先摘旧向量再加新的），
  首次使用按 kind 全量装载 `vec_*`。缓存按数据库实例隔离，不落盘。

## 插件设置（app_setting）

```sql
CREATE TABLE app_setting (
  key        TEXT PRIMARY KEY,     -- model.jev / model.extract / embedding
  value      TEXT NOT NULL,        -- JSON 对象
  updated_at TEXT NOT NULL
);
```

- 三槽模型配置存这里（`mode` / `provider` / `model` / `baseUrl` / `apiKey` / `dim` …），
  与记忆库同库同文件：备份记忆库就带上了配置。
- 读给页面时 `apiKey` 一律脱敏成 `…` + 尾 4 位；`settings.set` 收到掩码值或空串
  视为「保持原 key」，避免把掩码当新 key 存进去。
- 未脱敏的 `settings.raw` 只走内核 ↔ 外壳的帧通道（`src/embed.py` 同款机制），
  不经 HTTP。
- 改设置后内核推 `notify{kind:"settings"}`，外壳重新 `settings.raw` 刷新缓存，
  下一轮模型调用即生效（维度变更需重建向量索引）。

## 写入链路（ingest）

`src/ingest.py` 把一次摘要结果写进库，顺序固定：

```
BEGIN
  1. event：新建（或复用切分器建好的 id）并写入标题/摘要
  2. experience / cognition：按 source_event_id + content 幂等插入；
     若带调用 B 的裁决，则按 new / supersede / invalidate 执行（新行、或旧行置 superseded/invalid）
  3. memory_fts：event（title+summary）、每条 l3（content）
COMMIT
```

digest 不在这个事务里：`refresh_digest` 在提交之后跑（批处理时一个 job 只刷一次），
摘要失败回滚不会连带丢掉工作记忆的旧版本。

- **失败口径**：摘要 `ok=False` 时，新建事件只写事件（`summary=NULL`、标题保留），**不写 l3**，
  避免出现没有摘要的孤儿条目；已存在事件补摘要失败时**保留上一次的摘要与 `summarized_at`**，
  只置 `needs_review=1`，等下一次补跑。
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
vec_experience(ref_id) / vec_cognition(ref_id) / vec_insight(ref_id)
                              ──▶  同表主键（向量索引，可随时重建）
digest.ref_event_ids          ──▶  JSON 数组（缓存拼装来源）
app_setting(key, value)       ──▶  插件配置（与记忆数据同库，不含记忆内容）
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

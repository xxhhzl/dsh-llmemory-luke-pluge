# 记忆工具（Tools）

> 内核向模型暴露的工具：名称、作用、输入、返回、错误码。
> 工具通过 `tools.schema`（外壳拉取）声明，通过 `tools.calls` / `tools.result` 调用，
> 帧格式见 [Transmission.md](Transmission.md) §3.1 / §3.3 / §3.4；数据落点见 [database.md](database.md)。
>
> **本文以当前实现为准**（2026-11-03 对齐）。未实现的设计放 §6「Backlog」，不再混在正文里。

---

## 0. 通用约定

| 项 | 约定 |
|---|---|
| 命名 | 小写下划线、动词在前（`memory_search` / `memory_get` / `memory_recent`）；全局唯一 |
| 数量 | 3 个；`memory_note` 尚未实现（见 §6） |
| 范围 | `scope` 只有 `project` / `all`。`project` = 本项目（`origin_project` 匹配）+ 通用（`origin_project IS NULL`）；`all` = 跨项目。**范围是数据的固有属性**，检索参数只决定「本次要不要扩大」 |
| 上下文 | 信封里的 `session_id` / `origin_project` 由 `tools_call` 注入；调用方显式给值时优先 |
| 校验 | 参数在**执行时**用 pydantic 模型校验（`Literal` / 范围），非法值回 `-32602`。schema 只是给模型看的，不是防线 |
| `limit` | schema 上限 100，运行时按 `config.search.limit_max`（默认 20）收敛，并在返回里回显 |
| 时间 | ISO 8601；落库统一归一 UTC，SQL 比较走 SQLite `datetime()`，跨时区不会错序 |
| 错误 | 参数不合法 → `error.code = -32602`；目标不存在 → `error.code = 1003`；未知工具 → `-32601`；内部失败 → `-32603` |
| 副作用 | 读工具不改内容与可见性，但会更新**使用计数**（检索即提取练习，ACT-R 激活依赖它）；**模型侧**没有任何删除/修改/失效入口——增删改只经「设置 → 长期记忆」的人工管理接口（见 docs/shell.md） |

---

## 1. `memory_search`

**作用**：检索四类对象——事件（情景记忆）、经验（程序记忆）、认知（语义记忆）、洞见（归纳层：多条经验提炼的通用规则）——返回按「相关性 × 记忆强度」融合排序的混合列表。

**何时用 / 何时不用**（写进工具描述，模型据此决定是否调用）

| 场景 | 是否该调 |
|---|---|
| 用户提到「之前 / 上次 / 我们讨论过 / 你说过 / 偏好 / 约定」 | 该调 |
| 本次任务要沿用既有决定、项目约定、踩过的坑 | 该调 |
| 不确定记忆里有没有，想先确认一下 | 该调（宁可多查一次） |
| 只是定位当前代码/文件 | 不调，用 `grep` / `read` |
| 本次对话里已经给出的信息 | 不调 |
| 要按时间回放（「昨天/上周」） | 不调，用 `memory_recent` |
| 已拿到 id、要看原文 | 不调，用 `memory_get` |

> 描述里还写了结果怎么读：`score` 是相关性 × 记忆强度的融合分，`meta.use_count` 是使用热度；
> 记忆是「当时记下的」，与当前事实冲突时以当前为准。
> 依据见 `DEEPSEEK.md` 0.3.3 条（Anthropic 两篇工具设计文档 + MCP 规范）。

**流程**

```
FTS5 trigram 召回 top-50（<3 字符退回 LIKE，按标题命中优先排序）
  + ANN（usearch/HNSW）或 sqlite-vec KNN 取 top-50（外壳把 query 向量化后随调用带 query_vector；可选）
  → RRF(k=60) 融合两路名次
  → 联想扩展（同 subject / 同 cues，最多 20 条）
  → 按 kind 分组归一化（事件用 cnt，l3 用 use_count）
  → final = 0.6·rel + 0.4·Strength
  → 同事件簇 ≤2 条；留 10% 探索位给 use_count=0 的条目
  → 返回后对命中条目 +1 使用计数
```

> `query_vector` 不是模型可见参数：它由外壳（`tools-bridge`）在调用前用
> embedding 端点算好，放在 `tools.calls` 的调用项上，内核只读取、不调网络。
> embedding 不可用时该字段缺省，检索自动退化为纯词法。

**输入**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | 是 | — | 检索语句 |
| `limit` | integer | 否 | 5 | 1–100，运行时按 `search.limit_max` 收敛 |
| `kind` | enum(`event`,`experience`,`cognition`,`insight`) \| null | 否 | 全部 | 单值；省略表示四类都搜 |
| `scope` | enum(`project`,`all`) \| null | 否 | `config.search.default_scope`（当前 `all`） | `project` 只看本项目 + 通用 |
| `min_score` | number \| null | 否 | `config.search.min_score`（0） | 融合分下限 |

**返回**

| 字段 | 类型 | 说明 |
|---|---|---|
| `items[].kind` | enum | 条目类型 |
| `items[].id` | integer | 条目 id |
| `items[].title` | string | 事件标题 / l3 的 subject 或 context |
| `items[].text` | string | 事件摘要 / l3 正文 |
| `items[].score` | number | 融合分 0–1（跨类可比） |
| `items[].meta.importance` | number \| null | 仅 l3 |
| `items[].meta.confidence` | number \| null | 仅 l3 |
| `items[].meta.origin_project` | string \| null | 归属项目 |
| `items[].meta.use_count` | integer | 使用次数（事件为 `cnt`） |
| `items[].meta.session_id` | string | 仅事件：来源会话 |
| `items[].meta.assoc` | number | 1 = 直接命中，0.5 = 联想扩展 |
| `count` / `scope` / `limit` | — | 返回条数 / 实际生效范围 / 实际生效 limit |

---

## 2. `memory_get`

**作用**：按 id 取一条记忆——`event` 返回事件与全部正文消息，l3 返回整行；找不到返回 `1003`。

**输入**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | integer | 是 | — | 条目主键（事件或 l3） |
| `kind` | enum(`event`,`experience`,`cognition`,`insight`) | 否 | `event` | 取哪类对象 |

**返回**

| 字段 | 类型 | 说明 |
|---|---|---|
| `kind` | enum | 回显类型 |
| `event` | object | 仅事件：事件行（`id`/`title`/`summary`/`cnt`/`origin_project`/`summarized_at`/`needs_review`…） |
| `messages[]` | object[] | 仅事件：按 `seq` 升序的正文消息（`msg_id`/`role`/`content`/`occur_at`） |
| `item` | object | 仅 l3：整行（`content`/`kind`/`confidence`/`importance`/`origin_session`/`origin_project`/`source_event_id`/`status`/`valid_to`/`use_count`/`last_used_at`/`created_at`） |

再认也计一次使用：事件 `cnt+1`、l3 `use_count+1` 并刷新 `last_used_at`。

---

## 3. `memory_recent`

**作用**：按时间区间取「那段时间发生了什么，以及沉淀了什么」。

**语义**

- **事件**：发生区间（关联消息 `occur_at` 的 min/max）与查询区间相交即返回，按结束时间倒序；
- **经验 / 认知**：按**入库时间** `created_at` 落在区间内返回——是「那段时间沉淀了什么」，不是「事情发生在何时」；补录的历史条目会显示为入库那天，这是刻意接受的语义；
- **独立配额**：`l3_quota = min(3, limit)`，事件占 `limit - l3_quota`；
- 时间比较走 `datetime()`，`+08:00` 与 `+00:00` 混存不会错 8 小时；
- 参数非法（无法解析 / `from_at > to_at`）→ `-32602`。

**输入**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `from_at` | string | 是 | — | 区间起点（含） |
| `to_at` | string | 是 | — | 区间终点（含） |
| `session_id` | string \| null | 否 | 信封里的当前会话 | 限定会话 |
| `limit` | integer | 否 | 10 | 1–50 |

**返回**：`items[]`（`kind` / `id` / `title` / `text` / `score` / `meta.span|created_at|use_count`）+ `count`。

---

## 3.5 `memory_self_write`（写：只写自述自我）

**作用**：给自己写一条自我认知，落到 `self_memory(source=agent)`；下一轮以
`<self_notes source="assistant" revision="N">` 进入动态上下文。这是唯一的写工具，
且**只能写 agent 区**（设置的自我走管理端 `self.set`）。用户/项目事实不要用它——
那些由 ingest 自动提取。

**输入**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `content` | string | 是 | 第一人称一句话，≤300 字 |
| `kind` | enum(`identity`,`behavior`,`limitation`,`goal`) | 否 | 默认 `behavior` |
| `reason` | string \| null | 否 | 为什么写这条（≤120 字，便于审计） |

**返回**：`{id, kind, revision, limit, content, reason}`。

**限制**：active 自述总量 ≤ `core.max_self_notes`（默认 10）；每次写 `revision + 1`，
只追加、可在设置页停用回滚；写入后内核推 `notify {kind:"core"}`，外壳刷新 `core.get`。

---

## 4. 错误码对照

| code | 场景 |
|---|---|
| `-32602` | 参数不合法（枚举越界、范围越界、时间格式/顺序错误） |
| `-32601` | 未知工具名 |
| `1003` | 目标不存在（`memory_get` 找不到 id） |
| `-32603` | 工具内部错误 |

---

## 5. 与实现的一致性检查点

| 检查点 | 现状 |
|---|---|
| `tools.schema` 的 `parameters` 内包含 `required` / `additionalProperties` | ✅（`test_tool_definition_shape`） |
| 执行时校验，非法枚举回 -32602 | ✅（`test_illegal_scope_rejected` 等） |
| `limit` 运行时按配置收敛并回显 | ✅（`test_limit_clamped_to_config`） |
| 信封上下文注入且不覆盖显式参数 | ✅（`test_session_id_falls_back_to_envelope`） |
| 找不到目标返回 1003 | ✅（`test_memory_get_missing_returns_1003`） |

---

## 6. Backlog（未实现，勿按此调用）

| 项 | 说明 |
|---|---|
| `memory_note` | 显式写入经验/认知（`content` / `kind` / `importance` / `related_event_ids`），带相似内容合并。落点与去重口径未定 |
| `memory_get` 的 `start`/`end` 窗口 | 按码点取正文窗口，当前一次返回完整 `content` |
| `memory_search` 的 `kinds` 数组 | 当前只支持单值 `kind` |
| `memory_search` 的 `since` / `until` | 时间过滤，当前由 `memory_recent` 承担 |
| `memory_recent` 的 `from` / `to` 可选与 `min_overlap_seconds` | 当前起止必填 |
| 返回体的 `counts` / `total` / `truncated` | 当前只回 `count` |
| 字符串 id（`e12` / `n_7`） | 当前一律整数主键 |

---

## 7. 为什么不这样做

| 不做 | 原因 |
|---|---|
| 单独提供 `memory_list` / 全量导出 | 上下文会被灌满；UI 列表走管理接口的 `list`（分页 + 长文本裁剪） |
| 再开一个消息级 `memory_get` | 事件本身包含其全部消息，多开入口只增加选择成本 |
| 让 `memory_search` 返回事件正文 | 正文体积不可控；摘要 + id + 按需 `get` 才能稳定控制 token |
| 提供 `memory_ingest` / 触发切分 | 入库与切分属于 `/memory` 命令与外壳职责，模型不应直接写库结构 |
| 提供 `memory_digest` | digest 由 `ingest.completed` 推送并由外壳缓存，重复拉取没有收益 |
| 任何删除 / 修改 / 失效类工具 | 模型侧只追加：事件、消息、摘要一经写入即为历史事实。人工增删改走「设置 → 长期记忆」的本地管理接口，不暴露给模型 |

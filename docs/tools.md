# 记忆工具（Tools）

> 本文定义内核向模型暴露的工具：名称、作用、输入、返回、字段类型与 JSON Schema。
> 工具通过 `tools.schema`（外壳拉取）声明，通过 `tools.calls` / `tools.result` 调用，格式见 [Transmission.md](Transmission.md) §3.1 / §3.3 / §3.4。
> 数据落点见 [database.md](database.md)；本文出现的表名、列名以该文档为准。

---

## 0. 通用约定

| 项 | 约定 |
|---|---|
| 命名 | 小写下划线，动词在前（`memory_search` / `memory_get` / `memory_recent` / `memory_note`）；全局唯一，不与内置工具重名 |
| 数量 | 4 个；同一动词不拆成多把工具（参考 §6） |
| 作用域 | 默认只读**当前会话**（信封里的 `session_id`）；`scope="all"` 才允许跨会话 |
| 时间 | 输入输出一律 ISO 8601 带偏移（如 `2026-10-20T14:55:39+08:00`）；内核内部统一转 UTC 比较，避免字符串跨时区比较出错 |
| 文本偏移 | `start` / `end` 以 **Unicode 码点**计，区间**左闭右开** `[start, end)`；不是字节偏移 |
| 窗口长度 | `end - start` 不得超过 2000 码点；超出或 `end <= start` 视为参数不合法（`-32602`）。返回 `truncated` 与 `next_start`，由调用方续读 |
| 错误 | 参数不合法 → `tools.result.status="error"`，`error.code = -32602`；目标不存在 → `error.code = 1003`（引用无法解析） |
| 只读/写分离 | 读工具（search / get / recent）不产生副作用；写工具只有 `memory_note`，且只追加——**长时记忆不可修改、不可删除、不可失效** |

---

## 1. `memory_search`

**作用**：一次检索三类对象——**记忆**（事件）、**经验**、**认知**——返回按相关度融合排序的混合列表，供模型判断"哪条值得细看"。

**设计说明**
- 三类对象的检索语料不同：
  - `memory` → 事件正文（该事件关联消息的 `content`，定义见 `memory_get`），**不在摘要上检索**；摘要只用于展示。
  - `experience` / `cognition` → 各自的 `content` 全文（l3 记录本身很短，不需要二次取）。
- 返回结果是**混合列表**，用 `items[].kind` 区分；同一个 `limit` 覆盖三类，不保证每类都有。
- `memory` 条目只给摘要与区间，正文按需用 `memory_get` 取；`experience` / `cognition` 条目直接给 `content`（短，内联更省一次调用）。
- 排序：每类先各自检索取前 N，再按 **RRF** 融合（`Σ 1/(60 + rank)`）；跨类原始分数不可直接比较，融合分可比较。
- 事件在**会话结束**或**每次上下文压缩后**由用户触发 `/memory` 切分形成；事件闭合后正文不再追加（继续对话会开新事件），因此摘要在生成时即完整。
- 正常路径下 `summary` 必非空；若模型生成失败，该事件保留 `title`、`summary` 留空并进入复核，不用正文拼接顶替。

### 输入字段

| 字段 | 类型 | 必填 | 默认 | 作用 |
|---|---|---|---|---|
| `query` | string | 是 | — | 检索语句，自然语言或关键词 |
| `kinds` | enum[](`memory`,`experience`,`cognition`) | 否 | 三类全选 | 要检索的对象类型；只查经验就传 `["experience"]` |
| `limit` | integer | 否 | 5 | 返回条数（三类合计），1–30 |
| `session_id` | string \| null | 否 | 当前会话 | 限定会话；`null` 表示按 `scope` 决定 |
| `scope` | enum(`session`,`all`) | 否 | `session` | `session` 只搜当前会话；`all` 跨会话 |
| `since` | string \| null | 否 | `null` | 时间下界：`memory` 按 `occur_at`，`experience`/`cognition` 按 `created_at` |
| `until` | string \| null | 否 | `null` | 时间上界，规则同上 |
| `min_score` | number | 否 | 0 | 融合分下限，0–1；低于此值不返回 |

### 返回字段

| 字段 | 类型 | 作用 |
|---|---|---|
| `items[].id` | string | 记录 id：`memory` 形如 `e12`；`experience`/`cognition` 形如 `n_7` |
| `items[].kind` | enum(`memory`,`experience`,`cognition`) | 条目类型，决定下面哪些字段有值 |
| `items[].session_id` | string | 所属会话；跨会话检索时用于区分来源 |
| `items[].score` | number | 融合分 0–1（RRF 归一后），跨类可比 |
| `items[].summary` | string \| null | 仅 `memory`：事件摘要（≤200 字符） |
| `items[].start_at` | string \| null | 仅 `memory`：事件发生区间起点（首条消息 `occur_at`） |
| `items[].end_at` | string \| null | 仅 `memory`：事件发生区间终点（末条消息 `occur_at`） |
| `items[].msg_count` | integer \| null | 仅 `memory`：该事件消息条数，决定是否值得 `memory_get` 细看 |
| `items[].content` | string \| null | 仅 `experience`/`cognition`：记录正文（短，直接内联） |
| `items[].importance` | number \| null | 仅 `experience`/`cognition`：重要度 0–1 |
| `items[].related_event_ids` | string[] \| null | 仅 `experience`/`cognition`：关联事件 id，可据此跳转 |
| `items[].created_at` | string | 入库时间 |
| `total` | integer | 命中总数（三类合计，可能大于返回条数） |
| `counts` | object | 各类命中数，如 `{"memory":3,"experience":1,"cognition":0}` |
| `truncated` | boolean | 是否因 `limit` 截断 |

### JSON Schema

```json
{
  "name": "memory_search",
  "description": "检索记忆（事件摘要）、经验、认知三类对象，返回按融合分排序的混合列表。记忆正文用 memory_get 取。",
  "strict": true,
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "检索语句，自然语言或关键词"},
      "kinds": {"type": "array", "items": {"type": "string", "enum": ["memory", "experience", "cognition"]}, "minItems": 1, "default": ["memory", "experience", "cognition"], "description": "要检索的对象类型"},
      "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5, "description": "返回条数（三类合计）"},
      "session_id": {"type": ["string", "null"], "description": "限定会话；null 时由 scope 决定"},
      "scope": {"type": "string", "enum": ["session", "all"], "default": "session", "description": "检索范围"},
      "since": {"type": ["string", "null"], "description": "时间下界，ISO 8601"},
      "until": {"type": ["string", "null"], "description": "时间上界，ISO 8601"},
      "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0, "description": "融合分下限"}
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

---

## 2. `memory_get`

**作用**：按**事件 id** 取该事件的全部关联消息，返回消息 JSON 数组；每条消息的 `content` 可用 `start` / `end` 取窗口。

**设计说明**
- 返回的是**消息数组**，不是拼接文本：每个元素与 `message` 表一一对应（`role` / `content` / `occur_at` 等），`role` 边界与工具消息的 JSON 正文都不会被破坏。
- 事件 → 消息通过 `memory.event_id` 关联，按 `seq` 升序排列。
- **窗口作用在每条 `content` 上**：`start`/`end` 为码点偏移、左闭右开 `[start, end)`；约束**窗口长度** `0 < end - start ≤ 2000`，缺省 `end = start + 2000`；越界或 `end <= start` 返回 `error.code=-32602`。
- 每条消息独立返回 `total_chars` / `truncated` / `next_start`，长消息按需续读；短消息一次取完。
- 偏移以**码点**计（Python `str` 语义）。JS `String.prototype.slice` 按 UTF-16 码元，遇 emoji 等增补平面字符会错位；调用方按 `next_start` 续读，不要自行推算。
- 边界：`start >= total_chars` 返回空串且 `truncated=false`；`end > total_chars` 截到 `total_chars`。
- 体积控制完全由窗口决定：每条 `content` 一次最多 `end - start` 个码点，事件的消息条数不额外设上限。

### 输入字段

| 字段 | 类型 | 必填 | 默认 | 作用 |
|---|---|---|---|---|
| `id` | string | 是 | — | 事件 id，如 `e12` |
| `start` | integer | 否 | 0 | 每条 `content` 的窗口起点（含），≥0 |
| `end` | integer \| null | 否 | `start + 2000` | 窗口终点（不含）；须满足 `0 < end - start ≤ 2000` |

### 返回字段

| 字段 | 类型 | 作用 |
|---|---|---|
| `id` | string | 回显事件 id |
| `title` | string | 事件标题 |
| `summary` | string | 事件摘要（生成于事件创建时，此后不变） |
| `occur_at` | string | 事件起始发生时间（首条消息的 `occur_at`） |
| `created_at` | string | 事件入库时间 |
| `messages[]` | object[] | 该事件的消息数组，按 `seq` 升序 |
| `messages[].id` | string | 消息 id，如 `m_1024` |
| `messages[].role` | enum(`user`,`assistant`,`tool`,`system`) | 消息角色 |
| `messages[].occur_at` | string | 消息发生时间 |
| `messages[].content` | string | 窗口 `[start, end)` 内的正文（工具消息为 `{"call":…,"result":…}` JSON 原文） |
| `messages[].total_chars` | integer | 该条正文总长度（码点） |
| `messages[].truncated` | boolean | 窗口之后是否还有内容 |
| `messages[].next_start` | integer \| null | 该条续读起点；`truncated=false` 时为 `null` |
| `total_messages` | integer | 该事件的消息总数 |

### JSON Schema

```json
{
  "name": "memory_get",
  "description": "按事件 id 取该事件的消息数组；每条 content 用 start/end 取窗口（左闭右开，窗口不超过 2000 码点）。",
  "strict": true,
  "parameters": {
    "type": "object",
    "properties": {
      "id": {"type": "string", "description": "事件 id，如 e12"},
      "start": {"type": "integer", "minimum": 0, "default": 0, "description": "每条 content 的窗口起点（含）"},
      "end": {"type": ["integer", "null"], "description": "窗口终点（不含）；须满足 0 < end-start <= 2000，缺省为 start+2000"}
    },
    "required": ["id"],
    "additionalProperties": false
  }
}
```

---

## 3. `memory_recent`

**作用**：按**时间区间**取事件摘要——凡是事件的发生区间与查询区间**有交集**就返回，用于"刚才那半小时聊了什么"这类问题。

**设计说明**
- 只返回摘要与时间，不返回正文；需要细节再用 `memory_get`。
- 事件的发生区间 = 其关联消息的 `occur_at` 的最小值/最大值。**当前 `event` 表没有区间列**，见 §5 的实现依赖。
- `from` / `to` 均可省略：`to` 缺省为当前时间，`from` 缺省为 `to - 30 分钟`。模型不擅长时间加减，默认值必须由内核兜住。
- 相交判定：`event.last_at >= from AND event.first_at <= to`（闭区间）。
- **本工具只回答"某段时间里发生了什么"**。若模型要的是"最近 N 条"（不关心区间），应改用 `memory_search` 按 `occur_at` 倒序，或后续另立 `memory_latest`；不要让 `memory_recent` 同时承担两种语义，否则默认值会把两种问法都答歪。

### 输入字段

| 字段 | 类型 | 必填 | 默认 | 作用 |
|---|---|---|---|---|
| `from` | string \| null | 否 | `to - 30min` | 区间起点，ISO 8601 带偏移 |
| `to` | string \| null | 否 | 当前时间 | 区间终点，ISO 8601 带偏移 |
| `session_id` | string \| null | 否 | 当前会话 | 限定会话 |
| `scope` | enum(`session`,`all`) | 否 | `session` | 检索范围 |
| `limit` | integer | 否 | 20 | 返回条数上限，1–100 |
| `min_overlap_seconds` | integer | 否 | 0 | 交集时长下限；用于过滤"只擦到边"的事件 |

### 返回字段

| 字段 | 类型 | 作用 |
|---|---|---|
| `from` | string | 实际使用的区间起点（回显，便于确认默认值） |
| `to` | string | 实际使用的区间终点 |
| `items[].id` | string | 事件 id，如 `e12` |
| `items[].summary` | string | 事件摘要（`NULL` 时回退 `title`） |
| `items[].start_at` | string | 事件发生区间起点（`min(occur_at)`） |
| `items[].end_at` | string | 事件发生区间终点（`max(occur_at)`） |
| `items[].overlap_seconds` | integer | 与查询区间的交集的秒数 |
| `items[].msg_count` | integer | 该事件关联的消息数 |
| `total` | integer | 交集命中总数 |
| `truncated` | boolean | 是否因 `limit` 截断 |

### JSON Schema

```json
{
  "name": "memory_recent",
  "description": "按时间区间取事件摘要；返回与查询区间有交集的事件（id、摘要、起止时间）。不返回正文。",
  "strict": true,
  "parameters": {
    "type": "object",
    "properties": {
      "from": {"type": ["string", "null"], "description": "区间起点，ISO 8601；缺省为 to 前 30 分钟"},
      "to": {"type": ["string", "null"], "description": "区间终点，ISO 8601；缺省为当前时间"},
      "session_id": {"type": ["string", "null"], "description": "限定会话"},
      "scope": {"type": "string", "enum": ["session", "all"], "default": "session", "description": "检索范围"},
      "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "返回条数上限"},
      "min_overlap_seconds": {"type": "integer", "minimum": 0, "default": 0, "description": "交集时长下限（秒）"}
    },
    "required": [],
    "additionalProperties": false
  }
}
```

---

## 4. `memory_note`

**作用**：显式写入一条经验/认知（l3），供后续检索复用。**当前无落点表，暂缓实现**（见 §5）。

### 输入字段

| 字段 | 类型 | 必填 | 默认 | 作用 |
|---|---|---|---|---|
| `content` | string | 是 | — | 要记住的内容，一句话到一段话，≤1000 字符 |
| `kind` | enum(`experience`,`cognition`) | 是 | — | 经验（做法/流程）或认知（偏好/事实） |
| `importance` | number | 否 | 0.5 | 重要度 0–1，影响检索排序与后续淘汰 |
| `related_event_ids` | string[] | 否 | `[]` | 关联事件 id，最多 10 个 |

### 返回字段

| 字段 | 类型 | 作用 |
|---|---|---|
| `id` | string | 新建记录 id，如 `n_7` |
| `created_at` | string | 入库时间 |
| `deduped` | boolean | 是否与已有记录合并（内容高度相似时为 true） |

### JSON Schema

```json
{
  "name": "memory_note",
  "description": "显式写入一条经验或认知，供后续检索复用。内容相似时合并而不新增。",
  "strict": true,
  "parameters": {
    "type": "object",
    "properties": {
      "content": {"type": "string", "minLength": 1, "maxLength": 1000, "description": "要记住的内容"},
      "kind": {"type": "string", "enum": ["experience", "cognition"], "description": "经验或认知"},
      "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5, "description": "重要度"},
      "related_event_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 10, "default": [], "description": "关联事件 id"}
    },
    "required": ["content", "kind"],
    "additionalProperties": false
  }
}
```

---

## 5. 实现依赖（工具可用的前置条件）

| 依赖 | 影响的工具 | 现状 | 需要的改动 |
|---|---|---|---|
| 工具注册与执行 | 全部 | `tools.py` 只有 `memory_search` 空壳，`tools_call` 为空 | 按本文实现，schema 由 `tool()` 装饰器生成（`required`/`additionalProperties` 已在 `parameters` 内） |
| 事件 → 消息取数 | `memory_get` / `memory_recent` | `memory.event_id` 已存在，按 `seq` 升序取即可 | 无需改表；事件时间区间由关联消息的 `occur_at` 取 `min`/`max` 得到 |

检索索引与 l3 存储按项目安排后续再定，本文不展开。

---

## 6. 为什么不这样做

| 不做 | 原因 |
|---|---|
| 单独提供 `memory_list` / 全量导出 | 上下文会被灌满；UI 需要列表时走 `library.query`（协议里已有分页） |
| 再开一个消息级 `memory_get` | 事件本身就包含其全部消息，`memory_get(event_id)` 已能取到，多开一个入口只增加选择成本 |
| 让 `search` 返回事件正文 | 事件正文体积不可控；摘要 + `id` + 按需 `get` 才能稳定控制 token。`experience`/`cognition` 本身很短，所以在 `search` 里直接内联 `content` |
| 提供 `memory_ingest` / 触发切分 | 入库与切分属于 `/memory` 命令与外壳职责，模型不应直接写库结构 |
| 提供 `memory_digest` | digest 由 `ingest.completed` 推送并由外壳缓存，重复拉取没有收益 |
| 任何删除 / 修改 / 失效类工具 | 长时记忆只追加：事件、消息、摘要一经写入即为历史事实，不提供改与删的口子 |

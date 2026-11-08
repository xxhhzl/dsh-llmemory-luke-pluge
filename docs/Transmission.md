# JS ↔ Python 传输格式（Transmission）

> 一帧 = 一行 JSON（UTF-8，无 BOM，`\n` 结尾）。
> 字段表里「必填」指该帧类型下必须出现；「默认」指可省略时的取值。
> 方向：**下** = Python → JS，**上** = JS → Python。

---

## 1. 公共信封

```json
{
  "protocol_version": "1.0",
  "plugin_version": "0.4.0",
  "type": "ingest.request",
  "request_id": "c-103",
  "job_id": null,
  "session_id": "s_20261020_1",
  "agent_id": "lead#0",
  "ts": "2026-10-20T14:55:39+08:00",
  "data": {}
}
```

| 字段 | 类型 | 必填 | 默认 | 填写方 | 说明 |
|---|---|---|---|---|---|
| `protocol_version` | string `主.次` | 是 | — | 双向 | 协议版本；主版本不同直接拒收，次版本不同按兼容处理 |
| `plugin_version` | string | 是 | — | 双向 | 发送方插件版本，只进日志，不参与路由 |
| `type` | enum | 是 | — | 双向 | 决定 `data` 形状，取值见 §2 |
| `request_id` | string | 请求必填 | `null` | 发起方 | 请求标识；外壳用 `c-` 前缀，内核用 `s-` 前缀 |
| `job_id` | string \| null | 异步必填 | `null` | 内核 | 异步任务标识，回执与完成通知共用同一个值 |
| `session_id` | string | 是 | — | 双向 | 会话标识，决定数据落在哪个命名空间 |
| `agent_id` | string | 是 | — | 双向 | 格式 `名称#id`；`id=0` 表示初始 agent |
| `ts` | string | 是 | — | 双向 | ISO 8601 带时区，用于排序与重放 |
| `data` | 任意 | 是 | — | 双向 | 按 `type` 取值；**一帧只允许一个 `data`**，不得并列其他负载键 |

---

## 2. type 一览

| type | 方向 | 作用 | data 形状 | 触发时机 |
|---|---|---|---|---|
| `tools.schema` | 上（请求）/ 下（应答） | 把本插件可调用的工具清单（名称、说明、参数 JSON Schema）交给外壳，供模型在调用前发现工具 | 请求：`{}`；应答：工具定义数组 | 外壳需要工具列表时主动拉取，内核不主动推送 |
| `commands` | 上（请求）/ 下（应答） | 把插件注册的斜杠指令（名称、别名、参数、分组）交给外壳，驱动指令面板与补全 | 请求：`{}`；应答：指令定义数组 | 外壳需要指令面板时主动拉取，内核不主动推送 |
| `tools.calls` | 上 | 外壳把模型发起的工具调用（`call_id`、工具名、实参）下发给内核执行 | 调用数组 | 模型发起工具调用 |
| `tools.result` | 下 | 内核按 `call_id` 配对回传执行结果（成功数据或错误对象），供外壳写回对话 | 结果数组 | 工具执行完毕 |
| `ingest.request` | 上 | 把当前会话的新增消息与游标交给内核，触发入库与事件切分 | 消息列表 + 游标 | 用户执行 `/memory` |
| `ingest.receipt` | 下 | 立即确认是否受理，并告知本次实际要处理多少条新消息 | 回执 | 收到 ingest 后立即 |
| `ingest.completed` | 下 | 异步汇报切分结果，并下发可直接注入上下文的内存摘要（`digest`） | 处理结果 + digest | 异步任务结束 |
| `library.query` | 上 | 外壳按动作、层、会话、关键词、分页条件查询记忆库，驱动记忆面板与调试视图 | 查询条件 | UI 打开记忆面板 |
| `library.data` | 下 | 返回查询结果（条目、总数、库版本号），供 UI 渲染 | 库内容 | 查询返回 |
| `notify` | 下 | 库内容或 digest 变化时主动告知外壳，避免外壳轮询 | 变更推送 | 库发生写入/删除 |
| `llm.request` | 下 | 内核请求外壳代为调用模型（JEV 判定、事件摘要），内核不持有端点与凭据 | 调用参数（`request_id`、`purpose`、`messages`…） | 内核需要模型能力时 |
| `llm.result` | 上 | 外壳回传模型输出（原文 + usage）或错误对象，按 `request_id` 配对 | 结果 | 模型调用结束 |
| `error` | 双向 | 任一帧处理失败时回报错误码与说明，决定调用方是重试、降级还是丢弃 | 错误 | 任一帧处理失败 |
| `config.set` | 上（请求）/ 下（应答） | 运行期热更新**内核侧**配置（深度合并补丁，可要求重建检索索引） | 请求：配置补丁；应答：生效字段与版本号 | 用户改配置 |
| `config.get` | 上（请求）/ 下（应答） | 读回当前生效配置与版本号，供面板显示与排障 | 请求：`{}`；应答：配置树 + `revision` | 外壳打开设置页或排障 |
| `health` | 上（请求）/ 下（应答） | 探活，外壳据此判断内核是否存活或卡死 | 请求：`{}`；应答：`ok` / `revision` / `queue_depth` | 外壳定期探活 |
| `core.get` | 上（请求）/ 下（应答） | 取核心记忆编译结果：系统提示词段（`<long_term_memory>`）与自述快照（`<self_notes>`） | 请求：`{}`；应答：`version` / `text` / `notes_version` / `notes_text` / `stats` | 外壳启动、内核重启、`ingest.completed` 后刷新 |
| `shutdown` | 上 | 请求优雅退出，给在途任务一个排空窗口（`drain_ms`） | `{"drain_ms": 3000}` | 用户退出 |

---

## 3. 消息定义

### 3.1 `tools.schema`（上=请求 / 下=应答，拉取式）

> **作用**：让外壳拿到本插件可调用的工具清单（名称、说明、参数 JSON Schema），模型据此决定调哪个工具、传什么参数。

**内核不主动推送工具定义。** 外壳在启动或需要刷新时发同名请求（`data` 传 `{}`）：

```json
{
  "protocol_version": "1.0",
  "plugin_version": "0.4.0",
  "type": "tools.schema",
  "request_id": "c-11",
  "job_id": null,
  "session_id": "s_20261020_1",
  "agent_id": "lead#0",
  "ts": "2026-10-20T14:55:00+08:00",
  "data": {}
}
```

内核以同一 `type` 应答，`request_id` / `session_id` / `agent_id` 沿用请求值：

```json
{
  "data": [
    {
      "name": "memory_search",
      "description": "按语义检索长期记忆",
      "strict": true,
      "parameters": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "检索语句"},
          "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": "返回条数"}
        },
        "required": ["query"],
        "additionalProperties": false
      }
    }
  ]
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data[].name` | string | 是 | — | 工具名，全局唯一，小写字母/数字/下划线 |
| `data[].description` | string | 是 | — | 给模型看的说明，写“什么时候用”，不是“是什么” |
| `data[].strict` | boolean | 否 | `false` | 是否要求模型严格按 schema 输出，不允许多余字段 |
| `data[].parameters` | object | 是 | — | 标准 JSON Schema；结构见下 |

`parameters` 内部字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 固定 `object` |
| `properties` | object | 是 | 每个参数名 → 该参数的 schema（`type`、`description`、`enum`、`default`、`minimum` 等） |
| `required` | string[] | 是 | 必填参数名列表；无必填时给空数组 |
| `additionalProperties` | boolean | 是 | 固定 `false`，禁止模型编造参数 |

### 3.2 `commands`（上=请求 / 下=应答，拉取式）

> **作用**：让外壳拿到斜杠指令定义（名称、别名、参数、分组），用于指令面板展示与命令行补全。

与 `tools.schema` 相同：外壳发 `type=commands`、`data={}` 的请求，内核回指令定义数组；**不主动推送**。

```json
{
  "data": [
    {
      "name": "memory",
      "aliases": ["mem"],
      "description": "把当前会话写入长期记忆并切分事件",
      "usage": "/memory [--full] [--mark-end]",
      "arguments": [
        {"name": "full", "flag": "--full", "type": "boolean", "required": false, "default": false, "description": "忽略游标重扫整个会话"},
        {"name": "mark_end", "flag": "--mark-end", "type": "boolean", "required": false, "default": false, "description": "把最后一条 assistant 消息视为任务终点"}
      ],
      "requires_confirmation": false,
      "category": "context"
    }
  ]
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data[].name` | string | 是 | — | 指令名，匹配 `^[a-z][a-z0-9_-]{0,31}$`，不得与内置指令重名 |
| `data[].aliases` | string[] | 否 | `[]` | 别名，同样受命名约束且全局唯一 |
| `data[].description` | string | 是 | — | 一行说明，显示在指令面板 |
| `data[].usage` | string | 否 | 由 arguments 推导 | 用法提示，面板里灰字显示 |
| `data[].arguments` | object[] | 否 | `[]` | 参数定义，结构见下 |
| `data[].requires_confirmation` | boolean | 否 | `false` | 执行前是否弹确认框 |
| `data[].category` | enum | 否 | `context` | 面板分组，见选项表 |

`arguments[]` 每项：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `name` | string | 是 | — | 参数名，作为 `data.args` 的键 |
| `flag` | string | 否 | 由 name 推导 | 命令行写法，如 `--full` |
| `type` | enum | 是 | — | `boolean` / `string` / `integer` / `enum` |
| `required` | boolean | 否 | `false` | 是否必须由用户提供 |
| `default` | 任意 | 否 | `null` | 缺省值；`boolean` 建议显式 `false` |
| `choices` | string[] | 否 | `[]` | 仅 `type=enum` 时有效 |
| `description` | string | 是 | — | 一句话说明作用与影响 |

`category` 选项：

| 选项 | 含义 |
|---|---|
| `context` | 上下文/记忆注入相关 |
| `memory` | 直接操作记忆库（查、删、置顶） |
| `debug` | 调试与统计，默认不进面板 |

### 3.3 `tools.calls`（上）

> **作用**：把模型发起的工具调用（`call_id`、工具名、实参）下发给内核执行；一帧可含多次调用，结果按 `call_id` 配对。

```json
{
  "data": [
    {
      "call_id": "call_77",
      "name": "memory_search",
      "args": {"query": "drawio 配色", "limit": 3}
    }
  ]
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data[].call_id` | string | 是 | — | 本次调用标识，结果按它配对 |
| `data[].name` | string | 是 | — | 工具名，必须在 `tools.schema` 中声明过 |
| `data[].args` | object | 是 | — | 实参对象，保留原始 JSON 类型（数字不转字符串） |

### 3.4 `tools.result`（下）

> **作用**：按 `call_id` 回传执行结果，外壳据此把 tool 消息写回对话；成功与失败互斥，`data` 与 `error` 只填一个。

```json
{
  "data": [
    {
      "call_id": "call_77",
      "status": "success",
      "data": {"items": [{"id": "e12", "layer": "l2", "score": 0.83}]},
      "error": null
    }
  ]
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data[].call_id` | string | 是 | — | 对应请求里的 `call_id` |
| `data[].status` | enum | 是 | — | `success` / `error`，见选项表 |
| `data[].data` | object \| null | 是 | — | 成功时的结构化结果；`status=error` 时为 `null` |
| `data[].error` | object \| null | 是 | — | 失败时的错误对象；`status=success` 时为 `null` |

`status` 选项：

| 选项 | 含义 | 互斥约束 |
|---|---|---|
| `success` | 执行成功 | `data` 非空，`error` 必须为 `null` |
| `error` | 执行失败 | `error` 非空，`data` 必须为 `null` |

### 3.5 `ingest.request`（上）

> **实现**：内核收到后立即回 `ingest.receipt`，把任务交给后台 `IngestWorker`（切分 → 摘要 → 合并裁决 → 落库），完成后由常驻写线程发出 `ingest.completed`。主线程只读 stdin，模型调用（`llm.request`/`llm.result`）在 worker 线程等待。

> **作用**：把会话增量消息交给内核，触发幂等入库与事件切分；游标决定从哪条消息开始处理，`full=true` 时重扫整个会话。

```json
{
  "data": {
    "turn_id": "t_42",
    "trigger": "command",
    "full": false,
    "mark_end": false,
    "cursor": {"last_msg_id": "m_1023", "last_seq": 102},
    "truncated": false,
    "omitted_count": 0,
    "messages": [
      {
        "msg_id": "m_1024",
        "seq": 103,
        "role": "user",
        "content": "回到配色，边框改成 1px",
        "ts": "2026-10-20T10:24:31+08:00",
        "tool_name": null,
        "tool_call_id": null,
        "meta": {}
      }
    ]
  }
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data.turn_id` | string | 是 | — | 本轮标识；与 `session_id` 组成幂等键 |
| `data.trigger` | enum | 是 | — | 触发来源，见选项表 |
| `data.full` | boolean | 否 | `false` | `true` 忽略游标重扫整个会话（仍按 `content_hash` 去重） |
| `data.mark_end` | boolean | 否 | `false` | `true` 把最后一条 assistant 视为任务终点，强制关段 |
| `data.cursor` | object | 否 | `null` | 增量起点；`null` 等价全量 |
| `data.cursor.last_msg_id` | string | 否 | `null` | 已处理到的最后一条消息 id，与 `last_seq` 交叉校验 |
| `data.cursor.last_seq` | integer | 是* | `0` | 已处理到的最大序号；内核只处理更大的 seq |
| `data.truncated` | boolean | 否 | `false` | 列表是否被外壳截断 |
| `data.omitted_count` | integer | 否 | `0` | 被省略的消息条数 |
| `data.messages` | object[] | 是 | — | 消息列表，按 `seq` 升序；单次上限 2000 条 |

`messages[]` 每项：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `msg_id` | string | 是 | — | 会话内唯一；幂等去重第一键 |
| `seq` | integer | 是 | — | 会话内单调序号，从 0 开始 |
| `role` | enum | 是 | — | 消息来源，见选项表 |
| `content` | string | 是 | — | 文本；富内容由外壳先序列化 |
| `ts` | string | 是 | — | 消息产生时间，带时区 |
| `tool_name` | string \| null | 否 | `null` | 仅 `role=tool` 时有值 |
| `tool_call_id` | string \| null | 否 | `null` | 与发起它的助手调用配对 |
| `meta` | object | 否 | `{}` | 宿主附加信息，内核原样保存不解释 |

`trigger` 选项：

| 选项 | 含义 | 幂等键 |
|---|---|---|
| `command` | 用户执行 `/memory` | `(session_id, turn_id, last_seq)` |
| `auto` | 配置 `ingest.auto=true` 时外壳自动调用 | 同上（与 command 共用，防重复） |

`role` 选项：

| 选项 | 含义 | 处理影响 |
|---|---|---|
| `user` | 用户输入 | 唯一触发 JEV 边界判定的角色 |
| `assistant` | 助手输出 | 默认当前事件延续；`mark_end` 时可强制收尾 |
| `tool` | 工具结果 | 恒为延续，命中规则直接 CONTINUE，不调模型 |
| `system` | 系统提示 | 只作上下文，不参与切分与摘要 |

### 3.6 `ingest.receipt`（下，立即）

> **作用**：立即回执，告诉外壳是否受理、本次实际要处理多少条新消息，外壳用它显示进度、避免重复提交。

```json
{
  "data": {
    "accepted": true,
    "new_messages": 6,
    "display": "正在写入 6 条新消息…"
  }
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `job_id` | string | 是 | — | 异步任务 id，后续 `ingest.completed` 复用 |
| `data.accepted` | boolean | 是 | — | 是否受理；`false` 表示重复或全部已处理 |
| `data.new_messages` | integer | 是 | — | 本次实际要处理的新消息数，0 表示无新增 |
| `data.display` | string | 是 | — | 直接展示给用户的一句话，外壳不加工 |

### 3.7 `ingest.completed`（下，异步通知）

> **作用**：异步汇报切分结果，并下发可直接注入上下文的 `digest`；外壳缓存后每轮复用，直到下次 `/memory` 刷新。

```json
{
  "data": {
    "summary": {"messages": 6, "events_created": 1, "events_resumed": 1, "segments_closed": 1, "summaries": 1},
    "display": "已处理 6 条消息：新建 1 个事件、恢复 1 个、生成 1 条摘要",
    "digest": "<memory>\n[L3] 偏好：图内文字保持中文\n[L2 e12] 调 drawio 配色（挂起）：第二张图边框 1px\n</memory>",
    "digest_tokens": 812,
    "decisions": [
      {"msg_id": "m_1024", "decision": "RESUME", "event_id": 12, "segment_no": 2, "p_split": 0.997, "source": "jev", "needs_review": false}
    ]
  }
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `job_id` | string | 是 | — | 对应回执里的 job_id |
| `data.summary` | object | 是 | — | 计数摘要，字段见下 |
| `data.display` | string | 是 | — | 一句话结果，直接展示 |
| `data.digest` | string | 是 | — | **可注入的记忆块**；外壳缓存后每轮直接使用，直到下次 `/memory` |
| `data.digest_tokens` | integer | 是 | — | digest 的 token 估算，用于预算控制 |
| `data.decisions` | object[] | 是 | — | 每条消息的判定结果，见下 |

`summary` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `messages` | integer | 实际入库的新消息数 |
| `duplicates` | integer | 因 `msg_id` 重复而跳过的条数（缺省 0） |
| `events_created` | integer | 新建事件数 |
| `events_resumed` | integer | 恢复的历史事件数 |
| `segments_closed` | integer | 关闭的分段数 |
| `summaries` | integer | 生成的摘要条数 |

`decisions[]` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `msg_id` | string | 对应消息 |
| `decision` | enum | `CONTINUE` / `NEW` / `RESUME` |
| `event_id` | integer | 该消息归入的事件 id |
| `segment_no` | integer | 该消息所在的段号，从 1 开始 |
| `p_split` | number 0–1 | 模型给出的切分概率；规则命中的为 1.0 |
| `source` | enum | `rule` / `jev` / `escalated` / `fallback` |
| `needs_review` | boolean | 是否需要人工复核（低置信或降级时置 true） |

`source` 选项：

| 选项 | 含义 |
|---|---|
| `rule` | 硬规则直接判定（时间、长度、显式转折、工具消息） |
| `jev` | JEV 判定且置信度达标 |
| `escalated` | 低置信后升级给主模型判定 |
| `fallback` | 模型不可用或超时，按默认策略兜底 |

### 3.8 `library.query`（上）

> **作用**：记忆面板与调试视图的查询入口，按动作、层、会话、关键词、分页条件取库内容。

```json
{
  "data": {
    "action": "list",
    "layer": "l2",
    "session_id": "s_20261020_1",
    "query": "配色",
    "page": {"offset": 0, "limit": 50},
    "sort": {"field": "created_at", "order": "desc"}
  }
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data.action` | enum | 是 | — | 查询动作，见选项表 |
| `data.layer` | enum | 否 | 三层混合 | 目标层，见选项表 |
| `data.session_id` | string | 否 | 全部 | 限定会话；UI 通常传当前会话 |
| `data.id` | string | `get` 时必填 | — | 目标记录 id，如 `e12` |
| `data.query` | string | 否 | `null` | 关键词/语义过滤 |
| `data.page.offset` | integer | 否 | `0` | 跳过条数 |
| `data.page.limit` | integer | 否 | `50` | 本页条数，最大 200 |
| `data.sort.field` | enum | 否 | `created_at` | 排序字段，见选项表 |
| `data.sort.order` | enum | 否 | `desc` | 排序方向，见选项表 |

`action` 选项：

| 选项 | 含义 | 必带字段 |
|---|---|---|
| `list` | 分页列表 | 无 |
| `get` | 单条详情 | `id` |
| `timeline` | 事件时间线 | `session_id` |
| `stats` | 容量与队列统计 | 无 |

`layer` 选项：

| 选项 | 含义 | 返回 kind | 存储 |
|---|---|---|---|
| `l1` | 消息引用层 | `message_ref` | `message` 表 |
| `l2` | 事件层 | `event` | `event` 表 |
| `l3` | 经验/认知层 | `experience` / `cognition` | 预留，当前库无对应表 |

状态字段（当前不提供）：

当前库没有状态列（见 `database.md`）：事件一次 `/memory` 执行完毕即归档，消息与记忆行只增不改。**本协议不提供 `status` 过滤或返回字段**；如需失效标记，后续在库层扩展后再回到本节补充。

`sort.field` 选项：

| 选项 | 含义 |
|---|---|
| `created_at` | 入库时间（默认；l1/l2 都有该列） |
| `score` | 相关度，仅 `query` 非空时有效 |

`sort.order` 选项：

| 选项 | 含义 |
|---|---|
| `asc` | 升序，旧的在前 |
| `desc` | 降序，新的在前 |

### 3.9 `library.data`（下）

> **作用**：返回查询结果条目、总数与库版本号 `revision`，UI 据 `revision` 判断是否需要重新拉取。

```json
{
  "data": {
    "items": [
      {
        "id": "e12",
        "layer": "l2",
        "kind": "event",
        "title": "调 drawio 配色",
        "summary": "调整第二张图边框与配色",
        "msg_ids": ["m_100", "m_101", "m_103"],
        "created_at": "2026-10-20T10:26:00+08:00"
      }
    ],
    "total": 1,
    "revision": 37
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.items` | object[] | 结果条目，字段见下 |
| `data.total` | integer | 符合条件总数，不是本页条数 |
| `data.revision` | integer | 库版本号；`notify` 里递增，用于判断是否需要刷新 |

`items[]` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 记录 id；l2 形如 `e12` |
| `layer` | enum | `l1` / `l2` / `l3` |
| `kind` | enum | `event` / `message_ref` / `experience` / `cognition` |
| `title` | string | 一行标题（l2 = `event.title`） |
| `summary` | string | 摘要（l2 = `event.summary`；l1 为正文摘录） |
| `msg_ids` | string[] | 关联消息引用，**不含正文**；来自 `memory` 表 |
| `created_at` | string | 入库时间（`event.created_at` / `message.created_at`） |

> 字段与 `database.md` 一致：事件**没有**状态列，也没有分段表，因此本协议不返回 `status`、`segments`、`updated_at`。排序、过滤只使用库里真实存在的列。

### 3.10 `notify`（下）

> **作用**：库内容或 `digest` 变化时告知外壳增量刷新，替代外壳轮询。

```json
{
  "data": {"event": "memory.updated", "layer": "l2", "op": "upsert", "ids": ["e12"], "revision": 38}
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.event` | enum | 事件类型，见选项表 |
| `data.layer` | enum | 受影响的层 |
| `data.op` | enum | `upsert`（新增或更新）/ `delete`（失效或删除） |
| `data.ids` | string[] | 受影响的记录 id，UI 只刷新这些 |
| `data.revision` | integer | 递增版本号，客户端据此丢弃过期通知 |

`event` 选项：

| 选项 | 含义 |
|---|---|
| `memory.updated` | 库内容变化，UI 需要刷新受影响条目 |
| `digest.updated` | digest 变化（通常伴随 `ingest.completed`） |

### 3.11 `error`（双向）

> **作用**：统一的失败回报；`code` 决定调用方行为（重试／降级／丢弃），`message` 仅供人看、不得用于分支判断。

```json
{
  "data": {"code": 1004, "message": "model unavailable", "detail": "timeout after 5000ms", "retry_after_ms": 2000}
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.code` | integer | 错误码，决定调用方行为，见选项表 |
| `data.message` | string | 人类可读描述，**不得用于分支判断** |
| `data.detail` | string | 补充细节，便于定位 |
| `data.retry_after_ms` | integer | 建议重试间隔；不重试时为 `null` |

`code` 选项：

| 选项 | 含义 | 调用方动作 |
|---|---|---|
| `-32700` | JSON 解析失败 | 丢弃该帧，记录 |
| `-32600` | 非法请求（缺必填字段） | 记录，不重试 |
| `-32601` | 未知 type | 记录，可能版本不匹配 |
| `-32602` | 参数不合法 | 记录，不重试 |
| `-32603` | 内核内部错误 | 重试 1 次后降级 |
| `1001` | 重复请求（幂等命中） | 忽略 |
| `1002` | 游标过期 | 改发全量（`full=true`） |
| `1003` | 引用无法解析 | 标记 `stale_ref` 继续 |
| `1004` | 模型不可用 | 走兜底，标 `needs_review` |
| `1005` | 存储被占用 | 按 `retry_after_ms` 退避 |
| `1006` | 配额超限 | 暂停写入并提示用户 |

### 3.12 `config.set` / `config.get` / `health` / `shutdown`

> **作用**：四个运行期控制帧 —— `config.set` 热更新内核侧配置（深度合并补丁，可选重建检索索引）；`config.get` 读回当前生效配置；`health` 探活；`shutdown` 请求优雅退出并按 `drain_ms` 排空在途任务。

#### 参数归属：模型侧归外壳，内核侧归本插件

JEV 与摘要的模型调用由外壳代理（`llm.request` / `llm.result`，帧定义见 §3.13），内核**不持有** provider、端点与凭据。因此两类参数分开：

| 归属 | 参数 | 说明 |
|---|---|---|
| 外壳（**不**进本插件配置） | `provider` / `model` / `api_key` / `base_url` / `timeout` / `retry` | 由 DSH 的模型设置与 `ctx.llm` 适配器负责；内核只看到"请求已发出 / 超时 / 失败码" |
| 内核（`config.set` 可改） | `jev.accept_confidence` / `jev.on_uncertain` | 外壳只回 `p(split)`；accept / uncertain / escalate 的判定在内核，故阈值必须可热更新 |
| | `split.gap_minutes` / `split.max_event_tokens` / `split.max_messages` / `split.mark_end` | 调模型前后生效的切分参数：时间间隔（**软先验**，超限先问模型，判 CONTINUE 即不切）、事件正文 token 预算（主限制）、消息条数（仅紧急兜底）、任务终点 |
| | `search.limit_max` / `search.min_score` / `search.default_scope` | 检索通道参数 |
| | `store.db_path` / `store.busy_timeout_ms` / `store.queue_max` | 存储与队列 |
| | `log.level` | stderr 日志级别 |

#### 帧样例

```json
{"data": {"patch": {"jev": {"accept_confidence": 0.8}}, "reload": false}}
{"data": {}}
{"data": {}}
{"data": {"drain_ms": 3000}}
```

#### `config.set` 请求字段

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data.patch` | object | 是 | — | 配置补丁，只写要改的叶子节点，深度合并 |
| `data.reload` | boolean | 否 | `false` | `true` 时重建**检索索引**（FTS / 向量）并重读配置文件，会短暂阻塞。**不涉及模型连接**——模型由外壳持有 |

`config.set` 应答（下）：

```json
{"data": {"applied": ["jev.accept_confidence"], "revision": 12, "reload_ms": null}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.applied` | string[] | 实际生效的叶子路径，按补丁顺序 |
| `data.revision` | integer | 配置版本号，递增；`config.get` 与 `notify` 用它判断新旧 |
| `data.reload_ms` | integer \| null | 仅 `reload=true` 时有值，本次重建耗时 |

校验规则：

| 情形 | 行为 |
|---|---|
| 未知键（如把 `accept_confidence` 拼错） | **拒绝整个补丁**，回 `-32602`，`detail` 指出第一个未知路径；不做部分生效 |
| 类型 / 范围不符 | 同上，拒绝整个补丁 |
| `reload=true` 但重建失败 | 保留旧索引继续服务，回 `-32603`，`detail` 说明失败阶段 |

#### `config.get`（上 → 下）

请求 `data` 为 `{}`；应答返回当前生效配置与版本号：

```json
{"data": {"config": {"jev": {"accept_confidence": 0.8, "on_uncertain": "escalate"}}, "revision": 12}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.config` | object | 当前生效的配置树（只含内核侧参数，不含模型侧） |
| `data.revision` | integer | 与 `config.set` 应答中的 `revision` 同源 |

#### `health`（上 → 下）

请求 `data` 为 `{}`；应答：

```json
{"data": {"ok": true, "revision": 12, "queue_depth": 0, "queue_dropped": 0, "db_ok": true}}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.ok` | boolean | 内核可服务为 `true`；`false` 时外壳应重启插件 |
| `data.revision` | integer | 配置版本号，用于判断配置是否被外部改动 |
| `data.queue_depth` | integer | 待发帧数量，持续增大说明输出泵或外壳消费卡住 |
| `data.queue_dropped` | integer | 因队列满被丢弃的帧数（背压指标）；持续增长说明外壳没有及时读 stdout |
| `data.db_ok` | boolean | 存储可读写为 `true` |

#### `shutdown`（上）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `data.drain_ms` | integer | 否 | `3000` | 退出前等待在途任务的最长时间，0–30000 |

---

### 3.13 `llm.request` / `llm.result`（下 → 上，请求/应答配对）

> **作用**：内核没有模型能力，也不持有 provider / model / key。需要模型时（JEV 边界判定、事件摘要与经验提取），内核发包给外壳，外壳调用模型后按同一 `request_id` 回包。

**线程约束**：`llm.request` 由 worker 线程发出并等待；主线程继续读 stdin，收到 `llm.result` 时按 `request_id` 唤醒等待方。主循环不允许同步等模型，否则会卡住 stdin。

#### `llm.request` 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data.request_id` | string | 是 | 本次调用 id，形如 `llm_000001`；`llm.result` 必须原样回传 |
| `data.purpose` | enum | 是 | `jev`（边界判定）/ `summary`（事件摘要与经验提取） |
| `data.messages` | array | 是 | `[{"role": "system\|user\|assistant", "content": string}]`，OpenAI 兼容形态 |
| `data.response_format` | object \| null | 否 | 例 `{"type": "json_object"}`；`null` 表示不限格式 |
| `data.timeout_ms` | integer | 否 | 外壳侧超时，缺省取内核客户端配置（默认 120000） |
| `data.think` | boolean | 否 | 是否请求深思（G2）；外壳可忽略 |
| `data.model_hint` | string | 否 | 模型提示，外壳可忽略；内核不强制选型 |

#### `llm.result` 字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data.request_id` | string | 是 | 与请求一致；对不上时内核记 `llm.result.unmatched` 并丢弃 |
| `data.content` | string | 是* | 模型原文；解析（JSON / 标签）由内核按 `purpose` 负责 |
| `data.model` | string | 否 | 实际使用的模型名，写入日志与判定结果 |
| `data.usage` | object | 否 | `{"prompt_tokens": n, "completion_tokens": n}`，用于统计 |
| `data.error` | object \| null | 否 | 失败时给 `{"code": 1004, "message": "...", "detail": "..."}`；有 `error` 时忽略 `content` |

\* `error` 为空时 `content` 必填，缺失按 `1004` 处理。

#### 示例

```json
{"type": "llm.request", "data": {"request_id": "llm_000001", "purpose": "jev",
  "messages": [{"role": "system", "content": "你是会话事件边界判定器…"},
               {"role": "user", "content": "{\"current\":{...}}"}],
  "response_format": {"type": "json_object"}, "timeout_ms": 120000, "think": false}}

{"type": "llm.result", "data": {"request_id": "llm_000001",
  "content": "{\"decision\":\"SPLIT\",\"confidence\":0.91}",
  "model": "startlux-decision-27b-autothink",
  "usage": {"prompt_tokens": 812, "completion_tokens": 24}, "error": null}}
```

#### 约束

- 一个 `request_id` 只应答一次；重复应答按未命中处理。
- 超时由内核侧判定（`data.timeout_ms`），超时后丢弃该 `request_id` 的待应答项，调用方收到 `1004`。
- `content` 不做任何改写；内核侧的解析（`parse_judgement` / 摘要 JSON）负责容错与重试。
- 模型侧参数（provider / model / key / retry）只在外壳；内核配置里没有这些键。

---

## 4. 最小链路

```
拉取：  JS ─ tools.schema(请求) ─▶ Py
        Py ─ tools.schema(应答) ─▶ JS
        JS ─ commands(请求)     ─▶ Py
        Py ─ commands(应答)     ─▶ JS
写入：  JS ─ ingest.request ─▶ Py
        Py ─ ingest.receipt ─▶ JS      （立即）
        Py ─ llm.request ─▶ JS         （切分与摘要，可多次）
        JS ─ llm.result  ─▶ Py         （按 request_id 配对）
        Py ─ ingest.completed ─▶ JS    （异步，含 digest）
查看：  JS ─ library.query ─▶ Py
        Py ─ library.data  ─▶ JS
变更：  Py ─ notify(memory.updated) ─▶ JS
```

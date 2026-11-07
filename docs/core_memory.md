# 核心记忆与自我认知注入

> 目标：让模型在**每一轮**都看见「我是谁、我的边界、少量最稳定的全局规则」，
> 而不是每次都靠检索碰运气；同时不把几百条记忆塞进上下文。

## 1. 分层与通道

| 层 | 内容 | 通道 | 变化频率 |
|---|---|---|---|
| L0 设置的自我 | 身份、边界、工作方式（人工维护） | **系统提示词** `ctx.systemPrompt.section()` | 很少 |
| L1 核心规则 | 洞见 + 少量全局经验/认知（自动选材） | **系统提示词**（同一段） | 偶尔 |
| L2 自述自我 | 模型自己写的反思/提醒（`self_memory.source=agent`） | **动态上下文** `ctx.systemPrompt.context()`（user 快照） | 按 revision |
| L3 其余记忆 | 事件、经验、认知、洞见 | 检索（`memory_search`） | 每轮 |

为什么这样分（宿主与主流一致）：

- 系统提示词是**稳定前缀**，改了会从第一个变化的 token 起击穿 KV cache；
  所以只放「设置 + 精选规则」，且只在版本变化时更新。
- 自述会变，放系统提示词会频繁击穿缓存；放**动态上下文**后，宿主按文本去重，
  只有内容变化才追加一条 user 快照（`Current runtime context. This snapshot supersedes earlier runtime-context snapshots.`）。
- 这与 Letta 的 `persona` / agent-managed blocks、MemGPT 的 core memory、
  Anthropic 的「最小高信号集合 + CLAUDE.md 前置于上下文、其余按需检索」一致。

系统提示词段的 `order` 默认 **-2000**：排在 harness 身份（`-1000`）之前，即**整个系统提示词最前面**。
代价是核心块一旦变化，会从第一个 token 起击穿 KV cache；因此版本门控（内容不变不更新）必须保留。

## 2. 标签约定（不同内容必须用专属标签）

系统提示词段：

```xml
<long_term_memory>
<self_configured>
<self_item kind="identity" id="1">我是……</self_item>
<self_item kind="limitation" id="2">模型侧只读；写入口在设置页。</self_item>
</self_configured>
<core_memory>
<insight id="12">……</insight>
<experience id="34">……</experience>
<cognition id="56">……</cognition>
</core_memory>
</long_term_memory>
```

动态上下文：

```xml
<self_notes source="assistant" revision="2">
<note kind="behavior" id="3">长会话里先检索再回答。</note>
<note kind="goal" id="4">把重复做法归纳成洞见。</note>
</self_notes>
```

规则：

- 每类内容用自己的标签（`self_configured` / `core_memory` / `insight` / `experience` /
  `cognition` / `self_notes` / `note`），不要混在一个无标签的文本块里；
- 正文做 XML 转义（`&` / `<` / `>`），避免记忆内容破坏标签结构；
- 带 `id`，便于模型引用与人工审计；`self_notes` 带 `source="assistant"` 与 `revision`，
  明确「这是模型自己写的，不是用户说的」。

## 3. 选材与预算

| 来源 | 条件 | 上限（默认） |
|---|---|---|
| `self_memory(source=configured)` | active | 20 条 |
| `insight` | `scope=global`、`importance≥4` | 6 条 |
| `experience` | `scope=global`、`importance=5` | 4 条 |
| `cognition` | `origin_project IS NULL`、`importance=5`、非自我主体 | 4 条 |
| `self_memory(source=agent)` | active | 10 条（自述，进动态上下文） |

- 总预算 `core.max_chars`（默认 2000 字符）：按上表优先级装配，**整条丢弃**，
  不截半句；标签始终闭合。
- 排序：importance ↓、use_count ↓、id ↓（确定性，保证同输入同版本号）。
- `version = sha256(text)[:12]`；文本不变则版本不变，外壳不更新、不击穿缓存。

## 4. 刷新时机

- 外壳启动、内核重启后：`core.get` → 更新 section/context 缓存；
- `ingest.completed` 后：重新编译（版本不变则内部不更新）；
- `/memory` 命令最终也会触发 `ingest.completed`，因此无需额外钩子。

## 5. 写入边界

两类自我各有写入口，物理隔离：

| 接口 | 谁能调 | 写哪 | 生效到 |
|---|---|---|---|
| 管理端 `self.set / self.list / self.disable`（`POST /longmemory/api`，仅回环） | 用户/管理员 | `source=configured` | 系统提示词 `<self_configured>` |
| 工具 `memory_self_write` | 模型 | `source=agent` | 动态上下文 `<self_notes>` |

- `self.set` 按 `kind` 取代：旧行 `superseded`、新行 `revision+1`（只追加、可停用回滚）；
  拒绝 `source=agent`（那是模型工具的区）。
- `memory_self_write` 拒绝写 `configured`，单条 ≤300 字、总量受 `core.max_self_notes` 限制。
- 两边写入后都推 `notify {kind:"core"}`，外壳立即 `core.get` 刷新，下一轮生效。
- 其余记忆（经验/认知/洞见）保持只读，核心块只是它们的**只读视图**。

## 6. 已知限制

- 旧库迁移时 `experience.scope` 按 `origin_project IS NULL` 回填为 `global`，
  历史数据的「是否真的跨项目适用」可能不准；核心块因此可能混入项目专属条目。
  需要时用设置页把该条 `scope` 改回 `project`，它会自动退出核心块。
- 动态上下文是 **user 角色快照**，不是 assistant 消息；内容里已标注来源，
  模型不会把它当成自己说过的话。
- 宿主没有「终端用户编辑系统提示词」的 API；这里的内容由插件数据驱动，
  属于插件贡献，不是用户级提示词编辑。

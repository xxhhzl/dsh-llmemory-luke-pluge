# 记忆可见范围模型设计（Kimi 独立设计方）

> 状态：设计评审稿。配套批次 P0-P4 中，P0/P1 落「范围对齐」，P2+ 立项增强。
> 证据锚点：Mem0 实体范围文档、Letta/MemGPT MemFS、Zep/Graphiti group_id 命名空间、A-MEM（arXiv:2502.12110）、LangMem 命名空间语义；认知科学：ACT-R 基础激活、编码特异性、提取练习。

---

## 0. 项目现状复核（以源码为准）

| 断言 | 源码/文档证据 | 结论 |
|---|---|---|
| `event` 表有 `session_id`，无 `origin_project` | [src/database.py#L56-L66](src/database.py#L56-L66) | 「只看本项目」对事件**结构性失效** |
| `experience`/`cognition` 有 `origin_project`，NULL=通用 | [src/database.py#L75-L114](src/database.py#L75-L114) | l3 已有项目归属字段 |
| `_eligible()` 只有 `all` 与「origin 匹配」两支 | [src/retrieval.py#L90-L99](src/retrieval.py#L90-L99) | 无 `session` 分支；`scope="session"` 会走到 `origin is None or origin == origin_project` 的默认分支，语义漂移 |
| `retrieval.search` 默认 `scope="project"`，但 `memory_search` 工具默认 `scope="all"` | [src/retrieval.py#L244-L246](src/retrieval.py#L244-L246) vs [src/tools.py#L63-L64](src/tools.py#L63-L64) | 五处口径不一致的根因之一 |
| `docs/tools.md` 声称 `scope` 枚举为 `session/all` 且默认 `session` | [docs/tools.md#L46](docs/tools.md#L46) | 与代码完全脱节 |
| `config.search.default_scope` 枚举 `{session, all}`，默认 `"all"` | [src/config.py#L107](src/config.py#L107)、[L135](src/config.py#L135) | 配置与工具层、检索层三方不一致 |
| `tools_call` 不透传 `session_id`/`origin_project` | [src/tools.py#L118-L150](src/tools.py#L118-L150) | 信封里有 `session_id`/`agent_id`（[src/config.py#L37-L38](src/config.py#L37-L38)），但执行时丢失 |
| `memory_fts` 只有 `ref_kind/ref_id/title/body` | [src/database.py#L132-L138](src/database.py#L132-L138) | 无法在 FTS 层按 session/project 过滤，必须回表过滤 |
| `digest` 按 `session_id` 隔离（PK） | [src/database.py#L117-L123](src/database.py#L117-L123) | 工作记忆天然是会话级 |

---

## 1. 记忆的可见范围模型

### 结论

采用 **三层结构 + 一个特例**，不采用四层：

| 层 | 语义 | 是否必要 | 理由 |
|---|---|---|---|
| **会话（session）** | 一次对话的临时上下文 | ✅ 必要，但**只用于工作记忆 digest 与时间窗查询**，不作为长期记忆的可见性边界 | 会话是情景记忆的「拍摄机位」，不是长期资产的保险箱；长期记忆一旦巩固就应脱离单次会话 |
| **项目（project）** | 一个工作空间/代码库/业务域 | ✅ **核心边界**，长期记忆的默认可见性单位 | 对应人类「工作情境」；技能与知识在项目间复用是常态，但默认召回应偏向当前情境 |
| **全局/通用（global）** | 跨所有项目的通用知识、用户偏好、元认知 | ✅ 必要，通过 `origin_project IS NULL` 表示 | 对应语义记忆中的通用事实与程序记忆中的通用技能 |
| ~~用户（user）~~ | 多租户/多用户隔离 | ❌ **当前伪需求** | DSH 是单机个人插件，信封里只有 `agent_id` 没有 `user_id`；强行加用户层等于为不存在的多租户做架构 |

**为什么不是「session 作为长期记忆边界」？**

- **认知依据**：情景记忆（episodic memory）天然绑定「何时何地」，但其价值在于被提取为语义/程序记忆后脱离具体情境（Tulving 的编码特异性原则说明提取情境与编码情境匹配度影响回忆成功率，但**不等于**记忆被锁死在该情境）。参考 [Tulving & Thomson 1973, Psychological Review](https://psycnet.apa.org/doiLanding?doi=10.1037%2Fh0020071)。
- **工程依据**：Mem0 把 `run_id`（≈session）定位为「短生命周期、可过期」的流（[Entity-Scoped Memory](https://mem0.mintlify.app/platform/features/entity-scoped-memory)），长期记忆默认按 `user_id`/`agent_id` 持久化；Graphiti 用 `group_id` 做命名空间隔离，不鼓励用 episode 的临时 ID 做查询边界（[Graph Namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing)）。Letta 的 MemFS 以 agent 为仓库单位，会话只是对话线程，不切割记忆所有权（[MemFS](https://docs.letta.com/concepts/memfs/index.md)）。
- **本项目实际**：`event` 表已有 `session_id`，事件作为「原始情景记录」可溯源到会话；但 `experience`/`cognition` 是从事件中提炼的「可迁移知识」，用 `origin_session` 记录出处即可，可见性应看 `origin_project`。

**项目层的粒度选择**：Graphiti 文档明确提醒「namespace 粒度太细会碎片化，太粗则隔离不足」（[Best Practices](https://help.getzep.com/graphiti/core-concepts/graph-namespacing)）。本项目以「一个 DSH 工作区 = 一个 project」为粒度，与 `origin_project` 的现有写入路径（`ingest.request` 信封带 `origin_project`）一致，不引入子项目或团队层。

### 依据汇总

| 来源 | 关键论述 | 链接 |
|---|---|---|
| Mem0 | 用 `user_id/agent_id/app_id/run_id` 四维过滤；`run_id` 用于短生命周期流，长期记忆按 user/agent 归属 | [Entity-Scoped Memory](https://mem0.mintlify.app/platform/features/entity-scoped-memory) |
| Letta | MemFS 以 agent 为 git 仓库单位；memory 文件通过路径组织，不按 conversation 隔离 | [MemFS](https://docs.letta.com/concepts/memfs/index.md) |
| Zep/Graphiti | `group_id` 是图命名空间，用于多租户/团队/领域隔离；查询时按 `group_ids` 过滤 | [Graph Namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing) |
| LangMem | 所有记忆带 namespace，常见做法用 `user_id` 防交叉；可按应用路由、用户、团队分层共享 | [LangMem SDK launch](https://www.langchain.com/blog/langmem-sdk-launch) |
| A-MEM | Zettelkasten 式动态链接，记忆网络跨笔记演化，无硬隔离边界 | [arXiv:2502.12110](https://arxiv.org/abs/2502.12110) |
| 本项目 DEEPSEEK.md | 已明确「人类记忆没有可见范围，项目隔离下沉到检索层」 | [DEEPSEEK.md#L94](DEEPSEEK.md#L94)、[docs/database.md#L212](docs/database.md#L212) |

---

## 2. 范围是「数据固有属性」还是「检索时参数」？

### 结论

**归属（origin）是数据的固有属性；可见性（visibility）是检索时的策略。** 二者不矛盾：数据写入时记录 `origin_project` / `origin_session`（不可变），检索时由调用方给定「当前上下文」+ 范围模式决定可见集。

这等价于 Graphiti 的 `group_id` 模式（写入时打标，查询时过滤），也等价于 Mem0 的 filters 模式。完全摈弃「数据上有 scope 字段」或完全摈弃「检索时可调」都会走极端：

- 若 scope 只是检索参数、数据无归属 → 无法做「默认只看本项目」，也无法审计来源。
- 若 scope 是数据属性、检索不可改 → 跨项目复用知识（如「pnpm 比 npm 快」这种通用经验）会被错误锁死。

### 落地方案（最小迁移成本）

**表结构：零迁移。** `experience`/`cognition` 已有 `origin_project`（NULL=通用）；`event` 表**不**加 `origin_project` 列，因为事件永远属于一个会话，会话属于一个项目，通过 `session_id` 可推导（见 §3）。`digest` 已有 `session_id` 主键，天然会话级。

**需要补齐的是「会话→项目」的映射**：当前 `event` 表无 `project` 列，而 `ingest.request` 信封里带了 `origin_project`。建议**不改表**，而是：

1. 在 `ingest` 写入 `event` 时，把 `origin_project` 暂存到内核侧的一张轻量映射表 `session_project(session_id TEXT PRIMARY KEY, project TEXT)`（仅内存或 SQLite 一行，不进入 FTS）。迁移成本 ≈ 0，因为只在 `ingest` 事务里多一次 INSERT OR IGNORE。
2. 检索时，若需要「当前项目的事件」，先通过 `session_project` 把 `current_project` 翻译成 `session_id` 集合，再 `event.session_id IN (...)`。这避免了给 `event` 加列带来的迁移与索引重建。

**校验与默认值**：

- `scope` 枚举统一为 `{"project", "all"}`，**删除 `session`**（该语义在代码中不存在，保留只会误导）。
- 默认值：`memory_search` 与 `memory_recent` 的**工具层默认 `project`**，`retrieval.search` 内部默认也改为 `project`（与工具层对齐，消除 [tools.py#L63](src/tools.py#L63) vs [retrieval.py#L245](src/retrieval.py#L245) 的分歧）。
- `config.search.default_scope` 同步改为 `{"project", "all"}`，默认 `"project"`。
- 非法 scope 值**报错**（-32602），不再静默吞掉。

---

## 3. 检索入口的范围分工

### 结论

三个入口各自有明确的「默认正确」范围，模型**不需要**显式选范围，除非要突破默认：

| 入口 | 默认范围 | 是否允许显式扩大 | 理由 |
|---|---|---|---|
| `memory_search` | `project`（当前项目 + 通用） | 是，`scope="all"` 显式跨项目 | 语义检索是「找知识」，默认偏向当前工作情境；跨项目需要明确意图，避免噪声 |
| `memory_recent` | `session`（当前会话的时间窗） | 是，`scope="project"` 可扩展到本项目最近事件 | 时间窗查询的典型问题是「刚才聊了啥」，天然是会话内回顾；扩展到项目级用于「最近这个项目发生了啥」 |
| `digest` | `session`（当前会话） | 否，**不可扩大** | 工作记忆是「当前任务的便签」，注入上下文时若混入其他会话的 digest 会污染当前推理 |

**模型需要显式选范围吗？**

**默认不需要，但保留显式扩大的能力。** 依据：

- Mem0 的搜索默认要求至少给一个实体过滤（`user_id`），否则全库混杂（[Search Memory](https://mem0.mintlify.app/core-concepts/memory-operations/search)）；这对应「默认收窄」。
- Letta 的 memory 文件在系统提示中始终在场（`MEMORY.md` 根目录），相当于「默认注入当前 agent 的核心记忆」，深层记忆按需读取；不依赖模型每次声明范围。
- 人类记忆提取是「线索驱动」而非「范围声明」：我们不会先决定「现在只搜工作记忆」再回忆，而是情境线索自动偏向当前项目。`w_project` 权重（[retrieval.py#L198-L199](src/retrieval.py#L198-L199)）已体现这一点。

**信封透传**：`tools_call` 必须把信封里的 `session_id` 与 `origin_project` 注入工具执行上下文，否则「默认只看当前上下文」永远无法实现。这是 P0 修复项。

---

## 4. 三类记忆与遗忘/巩固机制的交互

### 语义 / 情景 / 程序的范围差异

| 记忆类型 | 本项目对应 | 范围特性 | 共享策略 |
|---|---|---|---|
| **情景记忆** | `event` | **强绑会话**：事件发生在一个 session 中，时间、对话上下文不可剥离 | 默认按会话/项目召回；跨项目召回事件价值低（细节多、噪声大），且 `event` 无 `origin_project`，结构性不支持 |
| **语义记忆** | `cognition` | **弱绑项目**：事实与偏好可跨项目复用（如「用户喜欢简洁输出」） | `origin_project IS NULL` 表示通用；非 NULL 时项目内优先，跨项目可通过 `all` 显式获取 |
| **程序记忆** | `experience` | **情境触发 + 跨场景迁移**：技能（怎么做）在相似情境下可跨项目复用 | 同 cognition；`context` 字段（触发条件）是检索线索，`origin_project` 是归属而非枷锁 |

依据：LangMem 明确区分三类记忆——语义记忆存事实/偏好、程序记忆存行为模式、情景记忆存过去交互（[LangMem SDK launch](https://www.langchain.com/blog/langmem-sdk-launch)）；A-MEM 的 Zettelkasten 网络允许记忆跨主题链接，不设硬边界（[arXiv:2502.12110](https://arxiv.org/abs/2502.12110)）。

### ACT-R 已落地，范围模型如何交互

当前 ACT-R 基础激活 `B = ln(1+use_count) − 0.5·ln(1+days)`（[retrieval.py#L60-L66](src/retrieval.py#L60-L66)）已体现「频率 × 新近」的遗忘曲线。范围模型应在此基础上加**情境衰减**：

- **跨项目召回应有额外衰减**：编码特异性原则表明，编码情境与提取情境越匹配，回忆越容易（[Tulving & Thomson 1973](https://psycnet.apa.org/doiLanding?doi=10.1037%2Fh0020071)）。当前 `w_project` 权重（0.10）是**静态加分**，建议改为**情境不匹配时的惩罚项**：
  - 同项目：`project_match = 1.0`（现状）
  - 通用（NULL）：`0.5`（现状，合理）
  - 跨项目：从 `0.0` 改为**负值或衰减因子**，如 `0.0 → -0.15`，使得跨项目条目在默认 `project` scope 下更难进入 top-k，但 `all` scope 下仍可被检索（只是排序靠后）。

- **提取练习已正确实现**：`_touch()` 只对最终返回的条目计数（[retrieval.py#L270-L280](src/retrieval.py#L270-L280)），符合「测试效应」（testing effect）——提取本身加强记忆（[Roediger & Karpicke 2006](https://scholar.google.nl/citations?view_op=view_citation&hl=zh-CN&user=5t5lgCgAAAAJ&citation_for_view=5t5lgCgAAAAJ:u5HHmVD_uO8C)）。**不要**把「召回但未返回」的候选也计入 use_count，否则会稀释提取练习的信号。

- **间隔效应（spacing）**：ACT-R 的 `days` 项已天然处理时间衰减；不需要额外实现「间隔重复调度器」，但可在 `consolidate` 阶段利用 `last_used_at` 做巩固决策（如长期未用的 l3 降权或标记 `status='archived'`）。

---

## 5. 分阶段落地方案

### P0（本轮必修，阻塞性）

| 项 | 内容 | 迁移成本 |
|---|---|---|
| 统一 `scope` 枚举 | 删除 `session`，保留 `project`/`all`；`docs/tools.md`、`docs/retrieval.md`、`src/config.py`、`src/tools.py`、`src/retrieval.py` 五处对齐 | 文档 + 枚举值修改，无数据迁移 |
| `tools_call` 透传上下文 | 把信封 `session_id`/`agent_id`/`origin_project` 传入工具执行，供 `memory_search`/`memory_recent` 做默认范围 | ~10 行代码 |
| 非法 scope 报错 | 不再静默返回 0 条，返回 -32602 | ~3 行代码 |

### P1（本轮应修，体验关键）

| 项 | 内容 | 迁移成本 |
|---|---|---|
| `memory_search` 默认 `project` | 工具层与检索层默认对齐，消除「传 all 却按 project 过滤」或反之的歧义 | 改两个默认值 |
| `session_project` 映射 | 新增轻量表/映射，让 `event` 可按项目过滤（不改 `event` 表结构） | 新增 1 张 2 列的表，ingest 时写入 |
| `memory_recent` 默认 `session` | 时间窗查询默认只看当前会话；`scope="project"` 时通过 `session_project` 扩展 | 修改 `recent()` 签名与 SQL |
| 跨项目衰减 | `_project_match` 跨项目返回负值（如 -0.15），替代硬过滤 | 改 1 个函数返回值，更新 `docs/retrieval.md` |

### P2（立项，增强）

| 项 | 内容 | 理由 |
|---|---|---|
| `memory_note` 实现 | 显式写入 l3，需带 `origin_project` 与 `origin_session` | 当前未实现，补全写路径 |
| digest 项目级聚合 | 当前 digest 按 session 隔离；可探索「项目级 digest」作为会话启动时的预注入 | 类似 Letta 的 `MEMORY.md` 根目录始终在场 |
| 巩固调度器 | 基于 `last_used_at`/`use_count` 的定期巩固（把高频经验提升为 cognition，或把陈旧 l3 标记 archived） | ACT-R 的自然延伸，对应人类「睡眠巩固」 |
| 向量召回通道 | `docs/retrieval.md` 已规划 v2 向量 + RRF；范围过滤应在向量召回后、精排前生效 | 与现有流水线兼容 |

### 明确不做

| 项 | 理由 |
|---|---|
| `user` 层隔离 | 单机个人插件，无多租户需求 |
| `event` 表加 `origin_project` 列 | 事件→会话→项目的推导足够，加列冗余且迁移成本高 |
| `session` 作为长期记忆可见性边界 | 语义不存在，且与人类记忆模型相悖 |
| 记忆删除/修改接口 | 本项目设计哲学为只追加（[docs/tools.md#L20](docs/tools.md#L20)） |

---

## 附：关键引用清单

1. Mem0 Entity-Scoped Memory — https://mem0.mintlify.app/platform/features/entity-scoped-memory
2. Mem0 Search Memory — https://mem0.mintlify.app/core-concepts/memory-operations/search
3. Letta MemFS — https://docs.letta.com/concepts/memfs/index.md
4. Zep/Graphiti Graph Namespacing — https://help.getzep.com/graphiti/core-concepts/graph-namespacing
5. LangMem SDK launch — https://www.langchain.com/blog/langmem-sdk-launch
6. A-MEM: Agentic Memory for LLM Agents — https://arxiv.org/abs/2502.12110
7. Tulving & Thomson (1973) Encoding specificity — https://psycnet.apa.org/doiLanding?doi=10.1037%2Fh0020071
8. Roediger & Karpicke (2006) Test-enhanced learning — https://scholar.google.nl/citations?view_op=view_citation&hl=zh-CN&user=5t5lgCgAAAAJ&citation_for_view=5t5lgCgAAAAJ:u5HHmVD_uO8C
9. ACT-R 官方教程（基础激活）— http://act-r.psy.cmu.edu/tutorials/unit5.pdf

---

## 附：采纳与偏离（2026-11-03 主代理记录）

本文与 GLM 设计稿、主代理方案三方比对后的处置：

| 本文结论 | 处置 | 说明 |
|---|---|---|
| 废弃 `session` 语义，统一 `project/all` | ✅ 采纳 | 进入批3 |
| 范围是数据固有属性 + 检索可显式扩大 | ✅ 采纳 | 与 GLM、Mem0/LangMem/Graphiti 一致 |
| `tools_call` 透传 `session_id`/`origin_project` | ✅ 采纳 | 批3 前置项 |
| 非法 scope 返回 -32602 | ✅ 采纳 | 批3 |
| 默认值取 `project` | ✅ 采纳（分两步） | 先补透传再切默认值，避免静默降召回 |
| `experience` 跨场景可迁移、`cognition` 按 subject 分层 | ✅ 采纳 | 写入端分类，不需要新字段 |
| 跨项目召回改负分（-0.15）替代硬过滤 | ⏸ 立项 | 需固定 query 集 A/B，先不动打分分布 |
| `session_project` 映射表替代加列 | ❌ 偏离 | 见下 |
| `memory_recent` 默认 `session`、`scope=project` 扩展 | ❌ 偏离 | 工具语义改为「时间窗内的事件」，不引入 session 边界 |
| 不做 `user` 层、不做删除接口 | ✅ 采纳 | 单机个人插件 |

**为什么偏离「不给 event 加列」**：本文主张用 `session_project(session_id, project)` 映射表推导。评审后选加列
（`ALTER TABLE event ADD COLUMN origin_project TEXT`），理由三条：

1. SQLite 的 `ADD COLUMN` 是元数据级操作，迁移成本≈0，不存在本文担心的「索引重建」——`event` 上的索引是
   `idx_event_session(session_id, id)`，加列不触碰。
2. 映射表并没有「省掉」持久化，只是把 `session→project` 换个地方存；而事件本身就是「某会话在某项目里发生的事」，
   归属写在事件行上是最直接的表达，检索时不必多一次翻译与 `IN (...)` 展开。
3. 老库补列后既有行为 NULL = 全局可见，与本文「事件默认可见」的语义一致，向后兼容。

**保留的分歧**：本文认为「跨项目事件召回价值低」，故不需要列；实际产品里跨项目找「我以前是不是在别的项目里
踩过这个坑」是长期记忆的主要用法之一，事件带归属后可以既默认收窄、又显式扩大。


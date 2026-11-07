# 事件摘要系统提示词（v0.2，形态待定）

> 状态：**草案**。摘要形态（单段 / 结构化）仍待定，用 `{SUMMARY_FORMAT}` 占位；
> 经验与认知的字段已定稿（见 §5）。
> **实现进度**：调用 A（摘要 + 新条目抽取）已落地于 `src/summary.py`；落库链路在 `src/ingest.py`；调用 B（批量裁决）待做。
>
> 调用方：外壳代理（`llm.request` / `llm.result`，帧定义见 [Transmission.md §3.13](Transmission.md#313-llmrequest--llmresult下--上请求应答配对)），内核不持有端点与凭据。
> 生成时机：事件闭合后，**每个事件一次调用**，同时产出摘要、经验与认知条目。

## 1. 设计依据

| 依据 | 取用的部分 |
|---|---|
| [Chain of Density (Adams et al., 2023)](https://arxiv.org/abs/2309.04269) | 固定长度下「先骨架、再补实体」能提升信息密度，且人类更偏好稠密摘要 → 摘要写法采用两段式，不增加长度只提高密度 |
| [Recursively Summarizing (Wang et al., 2023)](https://arxiv.org/abs/2308.15022) | 摘要应是「旧摘要 + 新上下文」的递归结果 → 预留 `previous_summary` 输入位（v1 不启用） |
| [MemGPT (Packer et al., 2023)](https://arxiv.org/abs/2310.08560) | 摘要只是压缩视图，细节留在原文 → 摘要不追求覆盖全部细节，事实性优先 |
| [A-MEM (Xu et al., 2025)](https://arxiv.org/abs/2502.12110) | 记忆条目要带上下文、关键词、标签等结构化属性 → 经验字段预留 `tags` |
| [Reflexion (Shinn et al., 2023)](https://arxiv.org/abs/2303.11366) | 经验是「对结果的言语反思」，不是流水账 → 经验只写可复用判断 |
| [LangMem 概念文档](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) | 记忆分语义/情景/程序三类；后台形成；召回应结合相似度、重要性与新旧 → 摘要走后台（事件闭合后），经验带 `importance` |

## 2. 硬约束（不随形态变化）

| 约束 | 值 | 来源 |
|---|---|---|
| 摘要长度 | ≤ 200 字符 | [tools.md:59](tools.md#L59) |
| 标题长度 | ≤ 24 字符，动词开头 | 现有 `summarize_event` 占位口径 |
| 输出 | 单个 JSON 对象，无 markdown 包裹、无解释 | 便于解析与失败重试 |
| 事实性 | 只写输入中出现的内容；不推测、不补全、不评价 | 记忆是历史事实，不可编造 |
| 原样保留 | 路径、命令、参数、数字、错误码、URL | 这些是后续检索的锚点 |
| 语言 | 跟随事件主要语言；技术名词保留原文 | 中文会话不应输出英文摘要 |
| 失败 | 不拼正文；`summary=NULL` + `needs_review` | [tools.md:36](tools.md#L36) |

## 3. 系统提示词（system）

> 提示词唯一来源是 `src/summary.py` 的 `SUMMARY_SYSTEM_PROMPT`，最新原文见
> [prompt.md](prompt.md)「事件归档器」一节（由 `scripts/export_prompts.py` 同步生成）。
> 本节只记录设计取舍，不再复制全文，避免文档与代码漂移。

v0.4（2026-11-07）重写了提取判据部分，结构借鉴四个已验证来源：

| 来源 | 借鉴的做法 |
|---|---|
| [mem0 `FACT_RETRIEVAL_PROMPT`](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py)（[论文 arXiv:2504.19413](https://arxiv.org/abs/2504.19413)） | 枚举「值得提取的类型」清单 + few-shot 示例**含空输出例**（纯问答给 `[]`） |
| [LangMem 概念文档](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) | 情景记忆要含「情境/做法/为何有效」；过度提取会降低检索精度（官方明确点名） |
| [Reflexion（NeurIPS 2023，arXiv:2303.11366）](https://arxiv.org/abs/2303.11366) | 经验 = 对成败的言语反思（为什么有效/为什么失败），不是过程复述 |
| [A-MEM（NeurIPS 2025，arXiv:2502.12110）](https://arxiv.org/abs/2502.12110) | 关键词/标签随条目产出（对应 cues） |

v0.4 相对 v0.2 的具体改动：

1. **判据从一句话扩成枚举清单**：经验三类（technique/pitfall/command）与认知三类
   （preference/constraint/fact）各带定义与反例（「不写」清单）；
2. **反问式自检**：经验问「换项目/换天还成立吗」，认知问「一周后还成立吗」，拦推测型凑数；
3. **数量与打分锚点**：明确「大多数事件 0–2 条」；importance 1–5 每档给语义；
   confidence 区分「直接观察（≈0.9）」与「推断（≤0.6）」；
4. **few-shot 从一个空例扩成三个**：有提取/纯问答空例/一次性干扰对照例，
   第三个示例附「说明」点破哪些内容不该提取。

替换前经 `.tmp/prompt_compare.py` 用真实模型（glm-5.3）四用例对比验证：
纯问答不再误提推测条目、当次变更不再被记成稳定事实、同一约束不再重复落两条、
打分拉开区分度；标题/摘要质量与 JSON 解析成功率持平。

v0.5（2026-11-07）补「认知主体（subject）的写法」一节：subject 是「对什么事物的认知」，
必须是具体事物（用户本人/项目/工具/配置/机制），用稳定全名、不加「本项目」「该」等指代前缀；
用户属性要落到具体领域（如「用户的语言偏好」），不要都堆在「用户」下；用词太泛宁可不写。
配合调用 B 的主体归一（见 §6.1），解决同一事物多种写法互相召回不到、同义条目堆积的问题。

v0.6（2026-11-07）补「经验的可迁移性」一节（借鉴 AWM，[arXiv:2409.07429](https://arxiv.org/abs/2409.07429)）：
AWM 归纳 workflow 时把实例专属值抽象成占位符（「dry cat food」→ `{product-name}`），
并专门对比过「抽象子流程 vs 具体经验」。我们的做法：**经验正文抽象、具体值放 cues**——
正文写「用项目虚拟环境的解释器」，cues 保留 `.venv`；正文写「发布构建脚本」，
cues 保留 `build_release`。摘要不受影响（仍原样保留路径/命令）。
真实会话四片段对比：正文含项目专属值的经验 **3 → 0**，条数不变（9 → 9）。

## 4. 用户消息模板（user）

```text
[事件] #{event_id}｜会话 {session_id}｜{message_count} 条消息｜{first_at} → {last_at}
[规模] 约 {tokens} token{truncated_note}
[正文]
{transcript}
```

- `transcript`：按 `jev._render_message(mode="digest")` 渲染；工具调用压成
  `[tool-call] 工具名 | 关键参数`，工具结果压成 `[tool-result] ok|error 大小B | 头尾摘录`。
- 超预算（默认 24,000 字符）：保留首条 user 与最后一条 assistant 的完整段落，
  中间从两端各取若干条，被裁处写 `…（中间省略 N 条）…`，并在 `[规模]` 标注「已裁剪」。
- `truncated_note`：未裁剪为空串，裁剪时为「（已裁剪）」。

## 5. 输出契约

```json
{
  "title": "调 drawio 第二张图边框",
  "summary": "把第二张图边框从 2px 改为 1px，配色统一为 #4A90D9，导出 export.svg 成功。",
  "experiences": [
    {
      "context": "编辑 drawio 图形样式并导出 SVG 时",
      "content": "导出前统一 strokeWidth，导出后再改无效",
      "kind": "technique",
      "importance": 4,
      "confidence": 0.9,
      "cues": ["drawio", "svg", "导出"],
      "action": "new"
    }
  ],
  "cognitions": [
    {
      "subject": "dsh-longmemory-plugin",
      "content": "事件摘要上限 200 字符，由外壳代理的模型生成",
      "kind": "constraint",
      "importance": 5,
      "confidence": 0.95,
      "cues": ["摘要", "限制"],
      "action": "new"
    }
  ]
}
```

### 字段说明

| 字段 | 取值 | 说明 |
|---|---|---|
| `context`（经验） | 字符串 ≤120 | 触发条件：什么情形下适用 |
| `subject`（认知） | 字符串 ≤40 | 命题主体：`用户` / 项目名 / 工具名 |
| `content` | 字符串 ≤200 | 做法或事实，一句话 |
| `kind` | 经验：`technique/pitfall/command`；认知：`fact/preference/constraint` | 子类，检索时过滤 |
| `importance` | 1–5 | 显著性 |
| `confidence` | 0–1 | 经验=可靠性；认知=确信度 |
| `cues` | 字符串数组 ≤10 | 检索线索 |
| `action` | `new` / `supersede` / `invalidate` | 写入动作 |
| `target_id` | 整数，`action != new` 时必填 | 被取代/失效的旧条目 id（来自写入前检索的候选） |
| `reason` | 字符串，`invalidate` 时必填 | 失效原因 |

解析与校验（`summary.py`）：

| 情况 | 处理 |
|---|---|
| JSON 解析失败 / 缺字段 | 重试 1 次（追加「只输出 JSON」提示）；仍失败 → `ok=False` |
| 传输层瞬时错误（超时 / 1004 模型不可用 / 队列满） | 重试 1 次，**降半输入**重跑（不追加解析失败提示） |
| 传输层确定性错误（参数 / 鉴权） | 不重试，直接 `ok=False` + `needs_review` |
| 时间预算 | 两次尝试共享 240s；单次尝试上限 180s，避免堵死单线程 worker |
| `title` 超长 | 本地截断到 24 字符，不重新调用 |
| `summary` 超长 | 本地截断到 200 字符，记 `truncated=True` |
| `summary` 为空 | 视为失败（`summary=NULL` + `needs_review`） |
| `experiences` / `cognitions` 非数组 | 丢弃该字段，摘要照常入库，记 `needs_review` |
| `action=supersede` 但 `target_id` 无效 | 降级为 `new`，记 `needs_review` |
| `action=invalidate` 缺 `reason` | 丢弃该条，摘要照常入库 |

## 6. 调用 B：合并裁决（`src/consolidate.py`）

调用 A 拿到的条目一律是候选（`action=new`），真正落库前由调用 B 决定它和旧记忆的关系。

```
gather_candidates  ── 认知按 subject（双向包含）+ content 召回；经验按 cues/content 召回
      │               （只取 active、未过期、同 origin 可见范围，top-5）
      ▼
build_messages     ── 一次请求装多条「新条目 + 各自候选」
      ▼
parse_decisions    ── action ∈ new / supersede / invalidate；target 必须在候选里
      │               + 认知主体归一：subject 只能取自候选或原值（防新造变体）
      ▼
ingest.apply_summary(decisions=...)  ── 新行 / 取代 / 失效 + FTS + digest
```

| 情况 | 处理 |
|---|---|
| 没有任何候选 | 不调用模型，直接按 new 落库（省一次调用） |
| 模型调用失败 / JSON 解析失败 | 全部按 new 落库，标记 `needs_review`（宁可重复，不丢记忆） |
| `target_id` 不在候选里 / 动作非法 | 该条降级为 new |
| `invalidate` 缺 reason | 降级为 new |
| 主体归一给出的写法既不是候选的、也不是原值 | 忽略（保持原样）——防止模型又造一个变体 |
| 落库 | `supersede` = 新行 + 旧行 `status='superseded'`；`invalidate` = 旧行 `status='invalid'` |

调用 B 的 prompt 约束：只有候选里明确重复或冲突才动旧条目，不确定一律 `new`；不得编造 `target_id`。

### 6.1 主体归一（v0.5）

同一事物在库里有多种写法（`dsh-longmemory-plugin` / `dsh-longmemory-plugin 项目` / `记忆插件`）时，
等值匹配召回不到彼此，同义条目只会不断新增。采用 Graphiti（[arXiv:2501.13956](https://arxiv.org/abs/2501.13956)，
[`dedupe_nodes` 实现](https://github.com/getzep/graphiti/blob/main/graphiti_core/prompts/dedupe_nodes.py)）的思路：
**判重时顺带判「是不是同一个现实事物」，并把规范写法带出来**。落在两处：

1. 召回放宽：`find_l3_candidates` 的 subject 从等值改为**双向包含**（新写法含旧写法，或旧写法含新写法）；
2. 裁决归一：调用 B 对认知额外输出 `subject`（只能是候选的写法或原值），
   落库时用它替换新条目的主体——新认知向已有规范写法收敛，等价于「规范名更新」的反向用法。

## 7. 待定项

| 项 | 说明 |
|---|---|
| `{SUMMARY_FORMAT}` | 摘要形态：单段 200 字 / 结构化字段（目标·进展·结论·未决）。**单独讨论** |
| ~~`previous_summary`~~ | **已启用**：已闭合事件被续写且超过阈值时补跑，输入带 `[已有摘要]`，走递归摘要 |
| ~~示例（few-shot）~~ | **已启用**（v0.4）：三个示例（有提取/空例/一次性干扰对照），每次调用约多 1.5K token，对比验证后确认收益大于成本 |

## 8. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.6 | 2026-11-07 | 经验可迁移性：正文抽象（项目专属值→通用说法），具体值放 cues |
| v0.5 | 2026-11-07 | 认知主体写法规范 + 调用 B 主体归一（召回双向包含、裁决带 `subject`）|
| v0.4 | 2026-11-07 | 按 mem0/LangMem/Reflexion/A-MEM 调研重写提取判据：类型枚举 + 反问自检 + 打分锚点 + 三个 few-shot；真实模型四用例对比验证后替换 |
| v0.3 | 2026-11-02 | 补调用 B（合并裁决）流程与失败策略；实现进度更新到 ingest |
| v0.2 | 2026-11-02 | 输出契约补 `experiences` / `cognitions` 字段与 `action/target_id/reason`；经验/认知字段定稿 |
| v0.1 | 2026-11-02 | 初稿：硬约束、系统提示词、输入模板、输出契约；形态与经验字段待定 |

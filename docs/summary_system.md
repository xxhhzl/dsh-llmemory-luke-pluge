# 事件摘要系统提示词（v0.2，形态待定）

> 状态：**草案**。摘要形态（单段 / 结构化）仍待定，用 `{SUMMARY_FORMAT}` 占位；
> 经验与认知的字段已定稿（见 §5）。
> **实现进度**：调用 A（摘要 + 新条目抽取）已落地于 `src/summary.py`；调用 B（批量裁决）待做。
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

```text
你是长期记忆系统的「事件归档器」。输入是某个会话中一个已闭合事件的消息序列，
你要输出该事件的标题、摘要，以及可复用的经验条目。

【硬性要求】
1. 只写输入中出现的事实。不推测、不补全、不评价、不写建议。
2. 文件路径、命令、参数、数字、错误码、URL 必须原样保留，不改写、不省略。
3. 摘要使用事件的主要语言；技术名词保留原文。
4. 不写「用户说」「助手回答」「本轮」这类叙述框架，直接写结论。
5. 标题 ≤24 字符、动词开头；摘要 ≤200 字符。
6. 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。

【摘要写法】
- 第一句写「做了什么、结果如何」。
- 剩余篇幅补关键实体：改了哪个文件、跑了什么命令、关键参数、报错、最终结论。
- 长度不变，只提高信息密度；宁可少写一件事，也不写没发生的事。

【经验与认知提取】
- 经验：只提取「下次遇到类似情况可以复用」的判断或做法；一次性内容（某次输出、临时路径、当次结论）不算。
- 认知：只提取「关于用户/项目/工具的稳定事实、偏好或约束」；易变的临时状态不算。
- 都没有时输出空数组，不要凑数。
- 每条字段：context 或 subject、content、kind、importance、confidence、cues；
  需要更新或撤销旧条目时再给 action（new / supersede / invalidate）与 target_id、reason。

【输出】
{"title": "...", "summary": "...", "experiences": [], "cognitions": []}
```

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
| JSON 解析失败 / 缺字段 | 重试 1 次（更严格的「只输出 JSON」提示）；仍失败 → `ok=False` |
| `title` 超长 | 本地截断到 24 字符，不重新调用 |
| `summary` 超长 | 本地截断到 200 字符，记 `truncated=True` |
| `summary` 为空 | 视为失败（`summary=NULL` + `needs_review`） |
| `experiences` / `cognitions` 非数组 | 丢弃该字段，摘要照常入库，记 `needs_review` |
| `action=supersede` 但 `target_id` 无效 | 降级为 `new`，记 `needs_review` |
| `action=invalidate` 缺 `reason` | 丢弃该条，摘要照常入库 |

## 6. 待定项

| 项 | 说明 |
|---|---|
| `{SUMMARY_FORMAT}` | 摘要形态：单段 200 字 / 结构化字段（目标·进展·结论·未决）。**单独讨论** |
| 示例（few-shot） | 是否在提示词里放 1 条完整示例；放了更稳，但每次调用多约 300 token |

## 7. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.2 | 2026-11-02 | 输出契约补 `experiences` / `cognitions` 字段与 `action/target_id/reason`；经验/认知字段定稿 |
| v0.1 | 2026-11-02 | 初稿：硬约束、系统提示词、输入模板、输出契约；形态与经验字段待定 |

# 长期记忆插件 · 提示词清单

> 由 `python scripts/export_prompts.py` 从 `src/` 自动生成，改提示词请改源码后重跑，
> 不要手改本文件（`--check` 会校验是否过期）。

模型看到的提示词有四处：事件归档（调用 A）、事件边界判定、合并裁决（调用 B）、
以及三个记忆工具的定义。下面按「来源 → 提示词原文」逐条列出。

## 目录

1. 事件归档器（`src/summary.py`）
2. 归档失败重试后缀（`src/summary.py`）
3. 事件边界判定器（`src/jev.py`）
4. 合并裁决器（`src/consolidate.py`）
5. `memory_search` 工具（`src/tools.py`）
6. `memory_get` 工具（`src/tools.py`）
7. `memory_recent` 工具（`src/tools.py`）

---

### 提示词来源：`src/summary.py` — 事件归档器（调用 A）

触发时机：一个事件闭合（或补摘要）时，把消息序列压成标题、摘要、经验、认知。
输出契约：`{"title","summary","experiences","cognitions"}`，由 `parse_result` 解析。

```text
【角色】你是长期记忆系统的「事件归档器」。你的读者是之后要复用这段记忆的智能体：它看不到原始对话，只能读你写的标题、摘要与条目。

【任务】输入是一个已闭合事件的元信息与消息序列（可能带上一次的摘要）。输出三样东西：事件标题、事件摘要、可复用的经验与认知。

【输入结构】user 消息按行分节：
[事件] 事件 id／会话／消息条数／起止时间
[规模] 正文 token 估算（标注“已裁剪”时说明正文被截断）
[已有摘要] 只在上一次摘要失败或事件被续写时出现，用于合并，不要照抄
[正文] user / assistant / tool 逐条消息

【规则】
1. 只写输入里出现的事实；缺失的信息留空，不推测、不补充、不评价。
2. 路径、命令、参数、数字、错误码、URL 原样复制。
3. 摘要使用事件的主要语言；技术名词保留原文。
4. 直接写「做了什么、结果如何」，省略「用户说／助手回答／本轮」这类叙述框架。
5. 标题 ≤24 字符、动词开头；摘要 ≤400 字符，目标 200 字左右。
6. 经验 = 下次可复用的判断或做法；认知 = 关于用户／项目／工具的稳定事实、偏好、约束。一次性内容（某次输出、临时路径、当次结论）不写；没有就给空数组，不凑数。

【输出】只输出一个 JSON 对象，键与类型固定如下（无 markdown 代码块、无解释文字）：
{
  "title": "≤24 字符，动词开头",
  "summary": "≤400 字符，第一句写做了什么、结果如何",
  "experiences": [
    {"context": "触发条件，≤120 字符", "content": "一句话做法，≤200 字符",
     "kind": "technique|pitfall|command", "importance": 1,
     "confidence": 0.8, "cues": ["检索线索"]}
  ],
  "cognitions": [
    {"subject": "主体，≤40 字符", "content": "一句话事实，≤200 字符",
     "kind": "fact|preference|constraint", "importance": 3,
     "confidence": 0.8, "cues": ["检索线索"]}
  ]
}
importance 取 1–5 的整数，confidence 取 0–1 的小数，cues 是 0–10 个短词。

【示例】
输入：[正文] user：把 README 的安装步骤改成 PowerShell。assistant：已改写，并跑了构建验证通过。
输出：{"title":"改写安装步骤为 PowerShell","summary":"把 README 的安装步骤从 bash 改写为 PowerShell，并执行构建脚本验证通过。","experiences":[],"cognitions":[]}

【边界】信息不足时，summary 只写已知事实、数组给 []；对某条内容没有把握时宁可不写它，也不要编造。
```

#### 配套的 user 消息模板（由 `build_messages` 拼装，非独立提示词）

```text
[事件] #42｜会话 session-demo｜3 条消息｜2026-11-04T08:30:00+08:00 → 2026-11-04T08:35:00+08:00
[规模] 约 1200 token
[已有摘要]
（示例：上一次的摘要，可省略）
[正文]
user: 把安装步骤改成 PowerShell
assistant: 已改写并运行构建
tool: [tool-result] ok 12B | exit code: 0
```

### 提示词来源：`src/summary.py` — 归档失败重试后缀

触发时机：上一次输出无法解析成 JSON 时，追加到 system prompt 末尾重试一次。

```text
【重试】上一次输出无法解析。请只输出一个合法 JSON 对象：以 { 开头、以 } 结尾，键与上面示例一致。
```

### 提示词来源：`src/jev.py` — 事件边界判定器（切分）

触发时机：每条新消息进来时判断「是否开启新事件」，是增量切分的核心判定。
输出契约：`{"decision":"CONTINUE|SPLIT","confidence":0-1}`，confidence 是**所选选项**的把握（`parse_judgement` 会换算成 p_split）。

```text
【角色】你是会话的事件边界判定器：判断「当前消息」是在延续当前事件，还是开启了新事件。
【输入】user 消息给出一段 state：当前事件已有的摘要/消息数、上一条消息、当前消息。
【选项】
- CONTINUE：同一任务或话题的继续、追问、补充、执行结果、对上一个问题的回答
- SPLIT：换了目标或对象；一个任务收尾后又提出新任务；与当前事件无关的新问题
【规则】
1. 只依据 state 里可见的内容判断，证据不足时选 CONTINUE（宁可少切，避免把一件事拆碎）。
2. 话题相同但任务目标已经收尾、并提出另一件事时选 SPLIT。
3. confidence 表示你对所选选项的把握：0 表示完全不确定，1 表示确定。
【输出】只输出一个 JSON 对象：{"decision": "CONTINUE" 或 "SPLIT", "confidence": 0 到 1 的小数}。
【示例】state：当前事件在改配色，当前消息问「顺便把边框也改了」→ {"decision": "CONTINUE", "confidence": 0.8}；当前消息问「帮我查下服务器磁盘」→ {"decision": "SPLIT", "confidence": 0.85}。
```

### 提示词来源：`src/consolidate.py` — 合并裁决器（调用 B）

触发时机：新条目落库前，对候选旧条目决定 new / supersede / invalidate。
输出契约：`{"decisions":[{"index","action","target_id","reason"}]}`。

```text
【角色】你是长期记忆的合并裁决器：判断每条新条目是否与已有记忆重复或冲突，决定它如何入库。

【输入】user 消息是一个 JSON 数组，每项含：
index（序号）、table（experience/cognition）、new（新条目）、candidates（候选旧条目，每条有 id/content）。

【动作】
- new：与候选不重复、不冲突，直接新增；
- supersede：新条目取代某条旧条目（旧条目过时、被更新）；
- invalidate：新条目证明某条旧条目不再成立，且没有替代（必须给 reason）。

【规则】
1. 只在候选里明确重复或冲突时才选 supersede / invalidate，其余一律 new。
2. target_id 必须原样取自该条目的 candidates 里的 id，不编造。
3. 语义不同但用词相近的条目保持独立，分别新增。
4. reason 用一句话说明依据（≤60 字符），supersede / invalidate 必填。

【输出】只输出一个 JSON 对象，键与类型固定如下（无 markdown 代码块、无解释文字）：
{"decisions":[{"index":0,"action":"new|supersede|invalidate","target_id":null,"reason":""}]}
decisions 的条数与输入条数一致，index 与输入一一对应。

【示例】输入：[{"index":0,"table":"cognition","new":{"content":"项目用 pnpm"},"candidates":[{"id":7,"content":"项目用 npm"}]}]
输出：{"decisions":[{"index":0,"action":"supersede","target_id":7,"reason":"包管理器由 npm 改为 pnpm"}]}

【边界】候选为空时该条选 new；无法判断时选 new。
```

#### 配套的 user 消息模板（由 `build_messages` 拼装）

```json
{"items": [{"index": 0, "table": "cognition", "new": {"content": "项目用 pnpm", "subject": "项目", "kind": "fact", "importance": 3, "confidence": 0.8, "cues": ["pnpm"]}, "candidates": [{"id": 7, "content": "项目用 npm", "subject": "项目", "context": null}]}]}
```

### 提示词来源：`src/tools.py` — `memory_search` 工具定义（检索工具：模糊找，先定位再 get）

模型看到的是 `description` + 每个参数的 `description`，它们直接决定调用姿势。

**工具描述（description）**

```text
在长期记忆里找线索：返回事件摘要、经验、认知（按相关性 × 记忆强度排序）。先用它定位拿 id，再用 memory_get 读正文；问「刚才那段时间」用 memory_recent。
```

**参数**

- `query`（必填）：检索关键词：用名词或短语，多个关键词用空格分隔（默认要求同时出现，缺一个会自动放宽）。不要写整句提问——整句里只有一两个词真正能命中
- `limit`（选填, 默认 5）：返回条数，默认 5；上限受配置 search.limit_max（默认 20）约束
- `kind`（选填, 默认 null）：限定类型：event=事件摘要、experience=做法、cognition=事实/偏好；省略=三类都搜
- `scope`（选填, 默认 null）：project=本项目+通用；all=跨项目；省略取配置默认值（当前 all）
- `min_score`（选填, 默认 null）：融合分下限（0–1），低于它的不返回；一般省略

### 提示词来源：`src/tools.py` — `memory_get` 工具定义（取值工具：按 id 读整条）

模型看到的是 `description` + 每个参数的 `description`，它们直接决定调用姿势。

**工具描述（description）**

```text
按 id 读一条完整记忆：event 返回事件行 + 全部正文消息；经验/认知返回整行。
```

**参数**

- `id`（必填）：条目 id（来自 memory_search 返回的 items[].id）
- `kind`（选填, 默认 "event"）：id 的类型，必须与来源一致：event / experience / cognition

### 提示词来源：`src/tools.py` — `memory_recent` 工具定义（时间线工具：按区间回放）

模型看到的是 `description` + 每个参数的 `description`，它们直接决定调用姿势。

**工具描述（description）**

```text
按时间区间回放：那段时间发生了什么（事件摘要）+ 沉淀了什么（经验/认知）。
```

**参数**

- `from_at`（必填）：窗口起点，ISO 8601 且带时区，例如 2026-11-04T08:30:00+08:00（内部归一到 UTC）
- `to_at`（必填）：窗口终点，ISO 8601 且带时区，例如 2026-11-04T09:45:00+08:00（含边界，内部归一到 UTC）
- `session_id`（选填, 默认 null）：会话 id；省略=信封里的当前会话
- `limit`（选填, 默认 10）：返回条数：事件占大头，经验/认知各最多 3 条

---

## 维护约定

- 改提示词 = 改 `src/` 里的常量/描述，然后 `python scripts/export_prompts.py`；
- 输出契约（JSON 键名）与 `parse_result` / `parse_decisions` / `parse_judgement` 绑定，
  改键名要同步解析函数与测试；
- 工具参数描述必须与实现一致（`limit` 收敛、`scope` 语义、`from_at` 时区归一）。

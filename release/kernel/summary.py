# -*- coding: utf-8 -*-
"""事件摘要器（调用 A）：一次调用产出摘要 + 经验 + 认知。

设计见 docs/summary_system.md：
- 每个事件闭合后调用一次，输入是事件的对话正文（digest 渲染 + 预算裁剪）；
- 输出 `{title, summary, experiences[], cognitions[]}`，解析与校验在本模块；
- 只做「看见什么」：所有条目一律 `action="new"`，与旧记忆的合并/取代/失效
  交给调用 B（consolidate），见 docs/database.md 的 l3 生命周期。

模型调用走外壳代理（`llm.request` / `llm.result`），本模块不持有端点与凭据。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from jev import render_message, truncate
from llm import LlmError

TITLE_MAX = 24
SUMMARY_MAX = 200          # l3 条目（经验/认知）的一句话上限
EVENT_SUMMARY_MAX = 400    # 事件摘要：要给检索与界面读，需要一段而不是一句
CONTEXT_MAX = 120
SUBJECT_MAX = 40
CUE_MAX = 10
CUE_LEN_MAX = 20
DEFAULT_MAX_INPUT_CHARS = 24000
LINE_MAX = 800

SUMMARY_SYSTEM_PROMPT = """【角色】你是长期记忆系统的「事件归档器」。你的读者是之后要复用这段记忆的智能体：它看不到原始对话，只能读你写的标题、摘要与条目。

【任务】输入是一个已闭合事件的元信息与消息序列（可能带上一次的摘要）。输出三样东西：事件标题、事件摘要、可复用的经验与认知。

【输入结构】user 消息按行分节：
[事件] 事件 id／会话／消息条数／起止时间
[规模] 正文 token 估算（标注“已裁剪”时说明正文被截断，被省略的中间部分不要推测）
[已有摘要] 只在上一次摘要失败或事件被续写时出现，用于合并，不要照抄
[正文] user / assistant / tool 逐条消息

【标题与摘要】
1. 只写输入里出现的事实；缺失的信息留空，不推测、不补充、不评价。
2. 路径、命令、参数、数字、错误码、URL 原样复制。
3. 摘要使用事件的主要语言；技术名词保留原文。
4. 直接写「做了什么、结果如何」，省略「用户说／助手回答／本轮」这类叙述框架。
5. 标题 ≤24 字符、动词开头；摘要 ≤400 字符，第一句写做了什么、结果如何。
   第二句写关键产出/结论：保留回答历史追问所必需的具体名称、数字、版本、路径、URL 与错误码；
   不确定的细节不要补，正文未出现的信息仍不写。

【经验提取】经验 = 下次遇到类似情况可直接复用的判断或做法。只从以下三类提取：
- technique：被验证有效的做法（含「为什么有效」——知其然也要知其所以然，才是可迁移的经验）；
- pitfall：踩过的坑及规避方式（报错原因、错误假设、返工的教训）；
- command：可复用的命令、配置或代码片段。
不写：某次具体的输出内容、一次性结论、临时路径、只适用于本事件上下文的中间状态；
      也不要写「记忆系统自身如何分类/归档/提取」的元讨论——那是系统设计，不是可复用做法。
提取时问一句：换一个项目／换一天，这条还成立吗？不成立就不写。

【经验的可迁移性】经验正文写成**换个项目也成立**的通用表述（摘要仍按上面的规则原样保留路径/命令）：
- 项目专属的值换成通用说法或占位符：项目名 → 「项目」或省略；绝对路径 → 「项目的…目录」；
  解释器路径 → 「项目虚拟环境的解释器」；具体脚本名 → 「发布构建脚本」；版本号 → 「当前版本」；
- **具体值放进 cues**，检索时仍能用（cues 保留 build_release、.venv、dsh-llmemory-luke 这类词）；
- 只有这条经验确实只对某个项目/版本成立时才保留具体值（如「Django 4.2 的 X 行为」）。
- `scope` 标出适用范围：**global** = 换个项目仍成立（做法不依赖本项目的目录结构、脚本名、配置）；
  **project** = 依赖本项目结构或约定，别处照搬会出错。正文写得再通用，只要依赖本项目约定就标 project。
  判断时问：**把这条经验原样搬到一个陌生项目，会不会因为「这里的规矩不一样」而失败？**会 → project。
  典型 project：依赖本项目的配置写法/目录约定/模块边界/构建流程/命名规范。
反例：❌「先设 PYTHONIOENCODING 再用 C:/proj/.venv/Scripts/python.exe 跑测试」
正例：✅「Windows 下跑 Python 测试先设 PYTHONIOENCODING=utf-8，用项目虚拟环境的解释器」

【认知提取】认知 = 关于用户／项目／工具的稳定事实、偏好或约束。只从以下三类提取：
- preference：用户明确表达的偏好（语言、风格、工作方式、工具选择）；
- constraint：项目或环境的硬约束（版本、目录结构、禁令、审批流程）；
- fact：稳定事实（架构选型、依赖关系、负责人的角色）。
不写：临时状态（“今天在做 X”“刚报了错 Y”）、推测出来的偏好、一次性的任务进展。
提取时问一句：一周后这条还成立吗？不成立就不写。

【认知主体（subject）的写法】subject 是「这是对什么事物的认知」，必须是**具体事物**：
用户本人、项目、工具、配置、某个机制。
- 用稳定全名，不加「本项目」「该」「这个」等指代前缀（写「项目文档」，不写「本项目文档」）；
- 同一事物始终用同一写法：项目用项目名、工具用工具名，不要为同一次对话另造同义主体；
- 关于用户的稳定属性不要都塞进「用户」：能落到具体领域就写领域（如「用户的语言偏好」「用户的提交流程要求」）；
- 用词太泛（「系统」「相关配置」）说明还没想清事物是什么——宁可不写这条认知。

【数量与打分】
- 大多数事件的经验和认知都是 0–2 条；宁可空数组，不凑数。拿不准的不写。
- importance：5=跨项目长期有效（如开发约束、反复出现的偏好）；4=本项目长期有效；3=本项目内有用；2=偶尔有用；1=勉强相关（1–2 分的基本不要写）。
- confidence：直接观察到的原话或命令输出给 0.9 左右；从上下文推断的 ≤0.6；没有把握的不写。
- cues：3–6 个检索用短词，取条目里的关键实体（工具名、文件名、概念），不要泛词。

【已有记忆与动作】user 消息可能带 [已有记忆] 段：列出库里可能相关的条目（带 id）。
- 新条目与其中某条重复或冲突时，用 action 指回它：supersede（新条目取代旧条目，如更新、更完整的说法）或 invalidate（旧条目不再成立，必须给 reason）；
- **同一主体 + 同一断言，仅措辞不同也算重复**（如「上网查找」vs「互联网查找」），必须 supersede，不要 new；
- target_id 必须原样取自 [已有记忆] 里的 id，不编造；
- 没有 [已有记忆] 段时，每条 action 一律 new；
- 只是话题相近、并非同一件事时，保持 new，不要硬合并。

【输出】只输出一个 JSON 对象，键与类型固定如下（无 markdown 代码块、无解释文字）：
{
  "title": "≤24 字符，动词开头",
  "summary": "≤400 字符，第一句写做了什么、结果如何",
  "experiences": [
    {"context": "触发条件，≤120 字符", "content": "一句话做法，≤200 字符",
     "kind": "technique|pitfall|command", "scope": "global|project",
     "importance": 1, "confidence": 0.8, "cues": ["检索线索"],
     "action": "new|supersede|invalidate", "target_id": null, "reason": ""}
  ],
  "cognitions": [
    {"subject": "主体，≤40 字符", "content": "一句话事实，≤200 字符",
     "kind": "fact|preference|constraint", "importance": 3,
     "confidence": 0.8, "cues": ["检索线索"],
     "action": "new|supersede|invalidate", "target_id": null, "reason": ""}
  ]
}
importance 取 1–5 的整数，confidence 取 0–1 的小数，cues 是 0–10 个短词。
每条经验/认知可选带 `action`（默认 new）、`target_id`（action 非 new 时必填，取自 [已有记忆]）、`reason`（invalidate 必填）。

【示例一：有可复用经验与稳定认知】
输入：[正文]
user：把 README 的安装步骤从 bash 改成 PowerShell。
assistant：改完了。Windows 上 bash 示例新用户跑不了，统一成 pwsh 命令。另外跑了 python scripts/build_release.py --check 验证产物一致。
user：以后这个项目文档都保持 PowerShell 优先。
输出：{"title":"改写安装步骤为 PowerShell","summary":"把 README 的安装步骤从 bash 改写为 PowerShell（Windows 用户跑不了 bash 示例），并用 build_release.py --check 验证产物一致；项目文档此后保持 PowerShell 优先。","experiences":[{"context":"发布前验证产物时","content":"用发布构建脚本的 --check 做逐字节比对，能拦住源码改了但产物没同步的问题","kind":"technique","scope":"global","importance":4,"confidence":0.9,"cues":["build_release","--check","产物校验","发布"]}],"cognitions":[{"subject":"项目文档","content":"文档命令示例保持 PowerShell 优先","kind":"preference","importance":4,"confidence":0.95,"cues":["文档","PowerShell"]}]}

【示例二：纯问答，没有可提取内容】
输入：[正文]
user：帮我解释一下 Python 的 GIL 是什么。
assistant：GIL 是 CPython 的全局解释器锁……（解释若干句）
user：明白了，谢谢。
输出：{"title":"解释 Python GIL","summary":"解释了 CPython 全局解释器锁的作用：同一时刻只允许一个线程执行字节码，CPU 密集任务应改用多进程。","experiences":[],"cognitions":[]}

【示例三：有一次性内容干扰，不要把当次结论当经验】
输入：[正文]
user：把边框改成 1px 然后导出 export.svg 给我。
assistant：已改成 1px，导出 export.svg 成功，文件在桌面。
user：这个插件的检索一直搜不到旧事件，你查下。
assistant：查到原因：FTS 表在旧库结构里缺 origin_project 列，scope=project 的过滤发生在召回之后，60 条别项目条目把本项目 5 条全挤出 top-50。已修：范围过滤下推到召回层。
输出：{"title":"导出 SVG 并修复检索范围过滤","summary":"把边框改为 1px 并导出 export.svg；随后定位检索漏召回的原因——FTS 表缺 origin_project 列导致范围过滤在召回后执行，范围外条目挤占 top-50——已把过滤下推到召回层修复。","experiences":[{"context":"FTS 检索带范围过滤时","content":"范围过滤必须下推到召回层（MATCH 查询里带条件），召回后再筛会让范围外条目挤占名额","kind":"pitfall","scope":"global","importance":4,"confidence":0.9,"cues":["FTS","召回","范围过滤"]}],"cognitions":[]}
说明：「导出 export.svg 到桌面」是当次产物，不提取；「边框 1px」是当次参数，不提取；只有「召回层下推」这类可迁移的判断才是经验。

【示例四：命中已有记忆 → supersede，不要 new】
输入：[已有记忆]
- cognition #28 [preference] 用户的信息检索偏好：遇到不了解的内容先上网查并核实来源真实性
[正文]
user：再说一次，不了解的内容都要去互联网查资料，并核实真实性，网上信息不一定对。
输出：{"title":"重申检索与核实偏好","summary":"用户重申：不了解的内容先联网查资料并核实真实性，不盲信网上信息。","experiences":[],"cognitions":[{"subject":"用户的信息检索偏好","content":"不了解的内容先联网查资料并核实真实性，不盲信网上信息","kind":"preference","importance":5,"confidence":0.95,"cues":["联网检索","资料核实"],"action":"supersede","target_id":28,"reason":"同一偏好的更完整表述"}]}

【边界】信息不足时，summary 只写已知事实、数组给 []；对某条内容没有把握时宁可不写它，也不要编造。"""

RETRY_SUFFIX = "\n\n【重试】上一次输出无法解析。请只输出一个合法 JSON 对象：以 { 开头、以 } 结尾，键与上面示例一致。"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

_EXPERIENCE_KINDS = ("technique", "pitfall", "command")
_COGNITION_KINDS = ("fact", "preference", "constraint")
_ACTIONS = ("new", "supersede", "invalidate")


class SummaryError(RuntimeError):
    '''
    摘要调用失败（外壳回错、超时、应答为空）。

    :param retryable: 是否值得重试——只有超时/模型不可用/队列满这类**瞬时**错误才重试；
                      参数错误、鉴权失败重试只会浪费一次预算。
    '''

    def __init__(self, message: str, *, retryable: bool = False,
                 code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


# 单次事件摘要的总时间预算（秒）：两次尝试共享，避免单线程 worker 被一个事件堵死
TOTAL_BUDGET_SECONDS = 240.0
ATTEMPT_TIMEOUT_SECONDS = 180.0
RETRYABLE_CODES = frozenset({1004})     # 模型不可用（docs/Transmission.md 错误码表）


@dataclass(frozen=True)
class EventMeta:
    '''事件元信息，写进 user 消息头。'''
    event_id: int
    session_id: str
    message_count: int
    first_at: str = ""
    last_at: str = ""
    tokens: int = 0


@dataclass(frozen=True)
class SummaryResult:
    ok: bool
    title: str = ""
    summary: str = ""
    experiences: tuple = ()
    cognitions: tuple = ()
    needs_review: bool = False
    error: str = ""
    raw: str = ""
    truncated: bool = False


class SummaryTransport(Protocol):
    def complete(self, messages: list, *, timeout: float | None = None) -> str:
        ...


# ---------------- 输入构造 ----------------

def _render_lines(messages: list) -> list:
    lines = []
    for message in messages:
        role = message.get("role", "")
        body = render_message(message, "digest")
        lines.append(f"{role}: {truncate(body, LINE_MAX // 2, LINE_MAX // 2)}")
    return lines


def _fit_transcript(lines: list, budget: int) -> tuple:
    '''按预算保留头尾，返回 (文本, 是否裁剪, 省略条数)。'''
    total = sum(len(line) + 1 for line in lines)
    if total <= budget:
        return "\n".join(lines), False, 0

    half = max(1, budget // 2)
    head, used = [], 0
    for line in lines:
        if used + len(line) + 1 > half:
            break
        head.append(line)
        used += len(line) + 1

    tail, used = [], 0
    for line in reversed(lines[len(head):]):
        if used + len(line) + 1 > half:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()

    omitted = len(lines) - len(head) - len(tail)
    middle = f"…（中间省略 {omitted} 条）…" if omitted > 0 else ""
    text = "\n".join(head + ([middle] if middle else []) + tail)
    return text, True, omitted


def _render_existing(existing: list | None) -> str:
    '''把「可能相关的已有记忆」渲染成 [已有记忆] 段（供调用 A 直接判重）。'''
    if not existing:
        return ""
    lines = []
    for item in existing:
        kind = item.get("kind") or ""
        sub = item.get("subkind") or ""
        label = f"{kind} #{item.get('id')}"
        if sub:
            label += f" [{sub}]"
        if item.get("scope"):
            label += f" scope={item['scope']}"
        content = str(item.get("content") or "")
        if kind == "cognition" and item.get("subject"):
            content = f"{item['subject']}：{content}"
        lines.append(f"- {label} {content}")
    return "[已有记忆]\n" + "\n".join(lines)


def build_messages(meta: EventMeta, messages: list, *,
                   max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
                   previous_summary: str = "",
                   existing: list | None = None) -> list:
    '''把事件元信息 + 对话正文拼成 system + user 两条消息。'''
    lines = _render_lines(messages)
    transcript, truncated, _ = _fit_transcript(lines, max(1000, int(max_input_chars)))

    header = (f"[事件] #{meta.event_id}｜会话 {meta.session_id}｜{meta.message_count} 条消息"
              f"｜{meta.first_at or '?'} → {meta.last_at or '?'}")
    scale = f"[规模] 约 {meta.tokens} token" + ("（已裁剪）" if truncated else "")
    parts = [header, scale]
    if previous_summary:
        parts.append(f"[已有摘要]\n{truncate(previous_summary, 200, 0)}")
    block = _render_existing(existing)
    if block:
        parts.append(block)
    parts.append("[正文]")
    parts.append(transcript)

    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ---------------- 输出解析 ----------------

def _clip(text, limit: int) -> str:
    '''硬截断（不追加省略标记），用于字段长度上限。'''
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit]


def _as_int(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_float(value, default: float = 0.8) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _as_cues(value) -> tuple:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip()[:CUE_LEN_MAX]
                 for item in value[:CUE_MAX] if str(item).strip())


def _resolve_action(item: dict, allow_actions: bool,
                    allowed_targets: set | None = None) -> tuple:
    '''返回 (action, target_id, reason)；非法动作按文档降级或丢弃。'''
    if not allow_actions:
        return "new", None, ""
    action = str(item.get("action") or "new").strip().lower()
    target = item.get("target_id")
    reason = str(item.get("reason") or "").strip()
    if action not in _ACTIONS:
        return "new", None, ""
    if action == "invalidate":
        if not reason:
            return None, None, ""
        if not isinstance(target, int):
            return "new", None, ""
        if allowed_targets is not None and target not in allowed_targets:
            return "new", None, ""
        return action, target, reason
    if action == "supersede":
        if not isinstance(target, int):
            return "new", None, ""
        if allowed_targets is not None and target not in allowed_targets:
            return "new", None, ""
        return action, target, reason
    return "new", None, ""


def _normalize_experience(item: dict, allow_actions: bool,
                          allowed_targets: set | None = None) -> dict | None:
    content = str(item.get("content") or "").strip()
    if not content:
        return None
    kind = str(item.get("kind") or "technique").strip().lower()
    scope = str(item.get("scope") or "project").strip().lower()
    action, target, reason = _resolve_action(item, allow_actions, allowed_targets)
    if action is None:
        return None
    return {
        "context": _clip(item.get("context"), CONTEXT_MAX),
        "content": _clip(content, SUMMARY_MAX),
        "kind": kind if kind in _EXPERIENCE_KINDS else "technique",
        # 适用范围：缺省 project（保守）；只有明确 global 才跨项目
        "scope": scope if scope in ("global", "project") else "project",
        "importance": _as_int(item.get("importance"), 3, 1, 5),
        "confidence": _as_float(item.get("confidence")),
        "cues": _as_cues(item.get("cues")),
        "action": action,
        "target_id": target,
        "reason": reason,
    }


def _normalize_cognition(item: dict, allow_actions: bool,
                         allowed_targets: set | None = None) -> dict | None:
    subject = str(item.get("subject") or "").strip()[:SUBJECT_MAX]
    content = str(item.get("content") or "").strip()
    if not subject or not content:
        return None
    kind = str(item.get("kind") or "fact").strip().lower()
    action, target, reason = _resolve_action(item, allow_actions, allowed_targets)
    if action is None:
        return None
    return {
        "subject": _clip(item.get("subject"), SUBJECT_MAX),
        "content": _clip(content, SUMMARY_MAX),
        "kind": kind if kind in _COGNITION_KINDS else "fact",
        "importance": _as_int(item.get("importance"), 3, 1, 5),
        "confidence": _as_float(item.get("confidence")),
        "cues": _as_cues(item.get("cues")),
        "action": action,
        "target_id": target,
        "reason": reason,
    }


def _load_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None
    return data if isinstance(data, dict) else None


def _targets_for(kind: str, allowed_targets):
    '''allowed_targets 可以是 {kind: set(ids)}，也可以是单一集合（向后兼容）。'''
    if isinstance(allowed_targets, dict):
        return allowed_targets.get(kind)
    return allowed_targets


def parse_result(raw: str, *, allow_actions: bool = False,
                 allowed_targets=None) -> SummaryResult:
    '''解析模型输出；摘要缺失即失败，条目非法只丢弃该条。'''
    data = _load_json(raw)
    if data is None:
        return SummaryResult(ok=False, error="json_parse", raw=raw, needs_review=True)

    title = _clip(data.get("title"), TITLE_MAX)
    summary = _clip(data.get("summary"), EVENT_SUMMARY_MAX)
    if not summary:
        return SummaryResult(ok=False, title=title, error="empty_summary",
                             raw=raw, needs_review=True)

    review = False
    experiences, cognitions = [], []
    raw_experiences = data.get("experiences")
    if raw_experiences is not None and not isinstance(raw_experiences, list):
        review = True
    for item in raw_experiences if isinstance(raw_experiences, list) else []:
        if not isinstance(item, dict):
            review = True
            continue
        normalized = _normalize_experience(
            item, allow_actions, _targets_for("experience", allowed_targets))
        if normalized is None:
            review = True
            continue
        experiences.append(normalized)

    raw_cognitions = data.get("cognitions")
    if raw_cognitions is not None and not isinstance(raw_cognitions, list):
        review = True
    for item in raw_cognitions if isinstance(raw_cognitions, list) else []:
        if not isinstance(item, dict):
            review = True
            continue
        normalized = _normalize_cognition(
            item, allow_actions, _targets_for("cognition", allowed_targets))
        if normalized is None:
            review = True
            continue
        cognitions.append(normalized)

    return SummaryResult(
        ok=True,
        title=title,
        summary=summary,
        experiences=tuple(experiences),
        cognitions=tuple(cognitions),
        needs_review=review,
        raw=raw,
        truncated=len(str(data.get("summary") or "")) > EVENT_SUMMARY_MAX,
    )


# ---------------- 传输 ----------------

class FakeSummaryTransport:
    '''离线替身：依次返回预设输出（字符串），或抛预设异常。'''

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list] = []

    def complete(self, messages: list, *, timeout: float | None = None) -> str:
        self.calls.append(messages)
        if not self.responses:
            return "{}"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ShellSummaryTransport:
    '''真实调用：交给外壳代理（llm.request，purpose=summary）。'''

    def __init__(self, client=None, *, timeout: float = ATTEMPT_TIMEOUT_SECONDS,
                 purpose: str = "summary"):
        self._client = client
        self.timeout = timeout
        self.purpose = purpose

    @property
    def client(self):
        if self._client is None:
            from llm import get_client
            self._client = get_client()
        return self._client

    def complete(self, messages: list, *, timeout: float | None = None) -> str:
        try:
            result = self.client.request(
                messages,
                purpose=self.purpose,
                response_format={"type": "json_object"},
                timeout=self.timeout if timeout is None else float(timeout),
            )
        except LlmError as exc:
            raise SummaryError(str(exc), retryable=_retryable(exc),
                               code=getattr(exc, "code", None)) from exc
        return result.content


def _retryable(exc: Exception) -> bool:
    '''判断 LlmError 是否属于「重试有意义」的瞬时错误。'''
    code = getattr(exc, "code", None)
    if code in RETRYABLE_CODES:
        return True
    text = str(exc)
    return ("超时" in text) or ("队列已满" in text) or ("timeout" in text.lower())


# ---------------- 入口 ----------------

def extract(meta: EventMeta, messages: list, transport: SummaryTransport, *,
            max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
            previous_summary: str = "",
            existing: list | None = None) -> SummaryResult:
    '''
    调用 A：摘要 + 经验 + 认知；解析失败或瞬时错误重试一次，仍失败返回 ok=False。

    :param existing: 可能相关的已有记忆（[{"kind","id","subkind","scope","subject","content"}]）；
                     给出时允许模型用 action/target_id 直接判重（target 必须来自该列表）

    重试策略（2026-11-03 定稿）：
    - **只重试瞬时错误**（超时 / 模型不可用 1004 / 队列满）——参数与鉴权类重试没意义；
    - 传输失败时**降半输入重跑**（真重试），不加「上次输出无法解析」的提示后缀；
    - 解析失败才追加 `RETRY_SUFFIX`；
    - 两次尝试共享 `TOTAL_BUDGET_SECONDS`，单事件不能无限占用单线程 worker。
    '''
    import time

    allowed_targets = None
    if existing:
        allowed_targets = {"experience": set(), "cognition": set()}
        for item in existing:
            bucket = allowed_targets.get(item.get("kind"))
            if bucket is not None and item.get("id") is not None:
                bucket.add(int(item["id"]))
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    payload = build_messages(meta, messages, max_input_chars=max_input_chars,
                             previous_summary=previous_summary, existing=existing)
    hint_chars = max_input_chars
    last_error = ""
    for attempt in (1, 2):
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            last_error = last_error or "budget_exhausted"
            break
        try:
            raw = transport.complete(payload,
                                     timeout=min(ATTEMPT_TIMEOUT_SECONDS, remaining))
        except Exception as exc:          # 外壳超时/回错/测试替身异常
            last_error = str(exc)
            if not getattr(exc, "retryable", False) or attempt == 2:
                break
            hint_chars = max(1000, hint_chars // 2)     # 降半输入再试
            payload = build_messages(meta, messages, max_input_chars=hint_chars,
                                     previous_summary=previous_summary,
                                     existing=existing)
            continue
        result = parse_result(raw, allow_actions=bool(existing),
                              allowed_targets=allowed_targets)
        if result.ok:
            return result
        last_error = result.error
        if attempt == 2:
            break
        payload = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT + RETRY_SUFFIX},
            payload[-1],
        ]
    return SummaryResult(ok=False, error=last_error or "unknown", needs_review=True)

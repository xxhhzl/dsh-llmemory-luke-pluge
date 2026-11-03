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

SUMMARY_SYSTEM_PROMPT = """你是长期记忆系统的「事件归档器」。输入是某个会话中一个已闭合事件的消息序列，
你要输出该事件的标题、摘要，以及可复用的经验与认知条目。

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
- 经验：下次遇到类似情况可以复用的判断或做法；一次性内容（某次输出、临时路径、当次结论）不算。
- 认知：关于用户/项目/工具的稳定事实、偏好或约束；易变的临时状态不算。
- 都没有时输出空数组，不要凑数。
- 每条经验字段：context（触发条件）、content、kind（technique/pitfall/command）、
  importance（1-5）、confidence（0-1）、cues（字符串数组）。
- 每条认知字段：subject（主体）、content、kind（fact/preference/constraint）、
  importance、confidence、cues。

【输出】
{"title": "...", "summary": "...", "experiences": [], "cognitions": []}
"""

RETRY_SUFFIX = "\n\n【重试】上一次输出无法解析。只输出一个合法的 JSON 对象，不要任何其他文字。"

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


def build_messages(meta: EventMeta, messages: list, *,
                   max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
                   previous_summary: str = "") -> list:
    '''把事件元信息 + 对话正文拼成 system + user 两条消息。'''
    lines = _render_lines(messages)
    transcript, truncated, _ = _fit_transcript(lines, max(1000, int(max_input_chars)))

    header = (f"[事件] #{meta.event_id}｜会话 {meta.session_id}｜{meta.message_count} 条消息"
              f"｜{meta.first_at or '?'} → {meta.last_at or '?'}")
    scale = f"[规模] 约 {meta.tokens} token" + ("（已裁剪）" if truncated else "")
    parts = [header, scale]
    if previous_summary:
        parts.append(f"[已有摘要]\n{truncate(previous_summary, 200, 0)}")
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


def _resolve_action(item: dict, allow_actions: bool) -> tuple:
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
        return action, target, reason
    if action == "supersede":
        if not isinstance(target, int):
            return "new", None, ""
        return action, target, reason
    return "new", None, ""


def _normalize_experience(item: dict, allow_actions: bool) -> dict | None:
    content = str(item.get("content") or "").strip()
    if not content:
        return None
    kind = str(item.get("kind") or "technique").strip().lower()
    action, target, reason = _resolve_action(item, allow_actions)
    if action is None:
        return None
    return {
        "context": _clip(item.get("context"), CONTEXT_MAX),
        "content": _clip(content, SUMMARY_MAX),
        "kind": kind if kind in _EXPERIENCE_KINDS else "technique",
        "importance": _as_int(item.get("importance"), 3, 1, 5),
        "confidence": _as_float(item.get("confidence")),
        "cues": _as_cues(item.get("cues")),
        "action": action,
        "target_id": target,
        "reason": reason,
    }


def _normalize_cognition(item: dict, allow_actions: bool) -> dict | None:
    subject = str(item.get("subject") or "").strip()[:SUBJECT_MAX]
    content = str(item.get("content") or "").strip()
    if not subject or not content:
        return None
    kind = str(item.get("kind") or "fact").strip().lower()
    action, target, reason = _resolve_action(item, allow_actions)
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


def parse_result(raw: str, *, allow_actions: bool = False) -> SummaryResult:
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
        normalized = _normalize_experience(item, allow_actions)
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
        normalized = _normalize_cognition(item, allow_actions)
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
            previous_summary: str = "") -> SummaryResult:
    '''
    调用 A：摘要 + 经验 + 认知；解析失败或瞬时错误重试一次，仍失败返回 ok=False。

    重试策略（2026-11-03 定稿）：
    - **只重试瞬时错误**（超时 / 模型不可用 1004 / 队列满）——参数与鉴权类重试没意义；
    - 传输失败时**降半输入重跑**（真重试），不加「上次输出无法解析」的提示后缀；
    - 解析失败才追加 `RETRY_SUFFIX`；
    - 两次尝试共享 `TOTAL_BUDGET_SECONDS`，单事件不能无限占用单线程 worker。
    '''
    import time

    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    payload = build_messages(meta, messages, max_input_chars=max_input_chars,
                             previous_summary=previous_summary)
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
                                     previous_summary=previous_summary)
            continue
        result = parse_result(raw)
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

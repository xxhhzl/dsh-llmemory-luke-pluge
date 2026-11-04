# -*- coding: utf-8 -*-
"""G1/G2 判定客户端：走外壳代理的模型调用（`llm.request` / `llm.result`）。

本模块负责「问一次模型」的三件事：
- 状态拼装（`build_state`）：把当前消息、事件摘要、最近消息压成结构化 state；
- 请求构造与发送（`ShellTransport`）：把 state 转成 system + user 消息，交给 `llm.LlmClient`；
- 应答解析（`parse_judgement`）：从模型输出里取 `decision` 与 `p_split`。

内核不持有 provider / model / key；离线测试用 `FakeTransport`，不联网。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

from llm import LlmClient, LlmError

HEAD_CHARS = 200
TAIL_CHARS = 200

INSTRUCTIONS = "这条消息是否开启了一个新事件？"

CRITERIA = {
    "CONTINUE": "同一任务或话题的继续、追问、补充、执行结果、对上一个问题的回答",
    "SPLIT": "换了目标或对象；一个任务收尾后又提出新任务；与当前事件无关的新问题",
}

SYSTEM_PROMPT = (
    "【角色】你是会话的事件边界判定器：判断「当前消息」是在延续当前事件，还是开启了新事件。\n"
    "【输入】user 消息给出一段 state：当前事件已有的摘要/消息数、上一条消息、当前消息。\n"
    "【选项】\n"
    f"- CONTINUE：{CRITERIA['CONTINUE']}\n"
    f"- SPLIT：{CRITERIA['SPLIT']}\n"
    "【规则】\n"
    "1. 只依据 state 里可见的内容判断，证据不足时选 CONTINUE（宁可少切，避免把一件事拆碎）。\n"
    "2. 话题相同但任务目标已经收尾、并提出另一件事时选 SPLIT。\n"
    "3. confidence 表示你对所选选项的把握：0 表示完全不确定，1 表示确定。\n"
    "【输出】只输出一个 JSON 对象：{\"decision\": \"CONTINUE\" 或 \"SPLIT\", \"confidence\": 0 到 1 的小数}。\n"
    "【示例】state：当前事件在改配色，当前消息问「顺便把边框也改了」→ {\"decision\": \"CONTINUE\", \"confidence\": 0.8}；"
    "当前消息问「帮我查下服务器磁盘」→ {\"decision\": \"SPLIT\", \"confidence\": 0.85}。"
)


class JudgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class JudgeResult:
    decision: str                 # CONTINUE / SPLIT
    p_split: float
    stage: str = "g1"
    model: str = ""
    latency_ms: int = 0
    raw: str = ""
    meta: dict = field(default_factory=dict)


def parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def gap_minutes(prev_ts: str, cur_ts: str) -> Optional[float]:
    """两条消息的时间间隔（分钟）；任一时间缺失/非法时返回 None。"""
    prev, cur = parse_ts(prev_ts), parse_ts(cur_ts)
    if prev is None or cur is None:
        return None
    return (cur - prev).total_seconds() / 60.0


def truncate(text: str, head: int = HEAD_CHARS, tail: int = TAIL_CHARS) -> str:
    """头 N + …（省略 K 字）… + 尾 M；tail<=0 表示只保留头部。"""
    text = str(text or "")
    if tail <= 0:
        if len(text) <= head:
            return text
        return f"{text[:head]}…（省略 {len(text) - head} 字）…"
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}…（省略 {omitted} 字）…{text[-tail:]}"


TOOL_CALL_PREFIX = "[tool-call] "
GENERIC_CALL = "调用工具"
_ARG_KEYS = ("command", "path", "query", "pattern", "description", "url")
_ARG_DIGEST_LIMIT = 120


def _tool_call_digest(line: str) -> str:
    """[tool-call] pwsh {...} → [tool-call] pwsh | command=... （≤120 字）。"""
    rest = line[len(TOOL_CALL_PREFIX):].strip()
    name, _, args = rest.partition(" ")
    digest = ""
    try:
        data = json.loads(args)
        digest = " ".join(f"{key}={data[key]}" for key in _ARG_KEYS if key in data)
    except (ValueError, TypeError):
        digest = args
    digest = " ".join(str(digest).split())
    if len(digest) > _ARG_DIGEST_LIMIT:
        digest = digest[:_ARG_DIGEST_LIMIT] + "…"
    return f"[tool-call] {name} | {digest}".rstrip(" |")


def _render_assistant(text: str, mode: str) -> str:
    """按 mode 处理 assistant 文本里的 [tool-call] 行。"""
    if mode == "raw":
        return text
    lines = []
    for line in text.splitlines():
        if line.startswith(TOOL_CALL_PREFIX):
            lines.append(GENERIC_CALL if mode == "generic" else _tool_call_digest(line))
        else:
            lines.append(line)
    return "\n".join(lines)


def _tool_status(message: dict) -> str:
    error = message.get("error")
    if error:
        code = error.get("code") if isinstance(error, dict) else str(error)
        return f"error={code or 'error'}"
    text = str(message.get("content") or "")[:400]
    if re.search(r"Traceback|Error:|error:|\[exit code: [1-9]", text):
        return "error"
    return "ok"


def _render_tool(message: dict, mode: str) -> str:
    """digest 模式：工具结果压成「状态 + 大小 + 头 200 + 尾 200」。"""
    text = str(message.get("content") or "")
    if mode != "digest":
        return truncate(text, HEAD_CHARS, TAIL_CHARS)
    body = truncate(text, HEAD_CHARS, TAIL_CHARS)
    return f"[tool-result] {_tool_status(message)} {len(text)}B | {body}"


def _render_message(message: dict, mode: str) -> str:
    role = message.get("role")
    text = str(message.get("content") or "")
    if role == "assistant" and mode in ("generic", "digest"):
        return truncate(_render_assistant(text, mode), HEAD_CHARS, TAIL_CHARS)
    if role == "tool":
        return _render_tool(message, mode)
    return truncate(text, HEAD_CHARS, TAIL_CHARS)


def render_message(message: dict, mode: str = "digest") -> str:
    """把一条消息渲染成可读文本（摘要器与判定器共用）。"""
    return _render_message(message, mode)


def build_state(message: dict, prev_message: Optional[dict], *,
                event_message_count: int, event_summary: str = "",
                event_title: str = "", history: list | None = None,
                mode: str = "raw") -> dict:
    """一次判定的结构化 state（作为 llm.request 的 user 内容）。

    :param mode: raw（原文）/ generic（调用工具）/ digest（工具名+参数摘要、结果带状态）
    """
    gap = None
    if prev_message is not None:
        gap = gap_minutes(prev_message.get("ts", ""), message.get("ts", ""))
    state = {
        "event": {
            "title": event_title or "",
            "summary": truncate(event_summary, 400, 0) if event_summary else "",
            "message_count": event_message_count,
        },
        "gap_minutes": None if gap is None else round(gap, 1),
        "recent": [
            {"role": item.get("role"), "content": _render_message(item, mode)}
            for item in (history or [])
        ],
        "current": {
            "role": message.get("role"),
            "content": _render_message(message, mode),
        },
    }
    return state


def render_prompt(state: dict) -> str:
    """人类可读视图，仅用于日志/回放。"""
    lines = [f"[事件] {state['event']['title'] or '（新事件）'}；"
             f"已有 {state['event']['message_count']} 条；"
             f"距上一条 {state['gap_minutes']} 分钟"]
    if state["event"]["summary"]:
        lines.append(f"[摘要] {state['event']['summary']}")
    if state["recent"]:
        lines.append("[最近消息]")
        lines.extend(f"- {item['role']}: {item['content']}" for item in state["recent"])
    lines.append(f"[当前消息] {state['current']['role']}: {state['current']['content']}")
    return "\n".join(lines)


class Transport(Protocol):
    def judge(self, state: dict, *, think: bool = False) -> JudgeResult:
        ...


class FakeTransport:
    """离线替身：按脚本或启发式给 p_split，不联网。"""

    def __init__(self, scores: Optional[list] = None, decision_fn=None):
        self._scores = list(scores or [])
        self._index = 0
        self._fn = decision_fn
        self.calls: list[dict] = []

    def judge(self, state: dict, *, think: bool = False) -> JudgeResult:
        self.calls.append({"state": state, "think": think})
        if self._fn is not None:
            p = float(self._fn(state, think))
        elif self._scores:
            p = float(self._scores[min(self._index, len(self._scores) - 1)])
            self._index += 1
        else:
            content = state["current"]["content"]
            p = 0.9 if any(word in content for word in ("另外", "新任务", "换个")) else 0.08
        p = max(0.0, min(1.0, p))
        return JudgeResult(
            decision="SPLIT" if p >= 0.5 else "CONTINUE",
            p_split=p,
            stage="g2" if think else "g1",
            model="fake",
            latency_ms=0,
            raw="",
        )


def build_judge_messages(state: dict) -> list:
    """把 state 转成外壳可代理的 chat messages。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(state, ensure_ascii=False)},
    ]


class ShellTransport:
    """真实判定：模型调用交给外壳代理（llm.request / llm.result）。

    think=False → G1 快判；think=True → 请求深思（G2），由外壳决定是否启用。
    """

    def __init__(self, client: Optional[LlmClient] = None, *,
                 timeout: float = 300.0, purpose: str = "jev"):
        self._client = client
        self.timeout = timeout
        self.purpose = purpose

    @property
    def client(self) -> LlmClient:
        if self._client is None:
            from llm import get_client
            self._client = get_client()
        return self._client

    def judge(self, state: dict, *, think: bool = False) -> JudgeResult:
        import time
        started = time.monotonic()
        try:
            result = self.client.request(
                build_judge_messages(state),
                purpose=self.purpose,
                response_format={"type": "json_object"},
                timeout=self.timeout,
                think=think,
            )
        except LlmError as exc:
            raise JudgeError(str(exc)) from exc

        decision, p_split = parse_judgement(result.content)
        return JudgeResult(
            decision=decision,
            p_split=p_split,
            stage="g2" if think else "g1",
            model=result.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            raw=result.content[:500],
            meta={"usage": result.usage},
        )


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_JSON_RE = re.compile(r"\{.*?\}", re.S)
_LABEL_RE = re.compile(r"\b(CONTINUE|SPLIT)\b")


def parse_judgement(content: str) -> tuple:
    """解析模型输出 → (decision, p_split)。

    期望 `{"decision": "SPLIT", "confidence": 0.9}`；confidence 是「所选选项的把握」，
    所以 CONTINUE 的 p_split = 1 - confidence。纯文本标签作为兜底（p_split=0.5）。
    """
    text = (content or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    match = _JSON_RE.search(text)
    if match:
        try:
            data = json.loads(match.group(0))
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            decision = str(data.get("decision") or data.get("choice") or "").upper()
            if decision not in CRITERIA:
                raise JudgeError(f"未知选项: {decision!r}")
            confidence = data.get("confidence", data.get("p_split"))
            confidence = clamp(confidence if confidence is not None else 0.5)
            p_split = confidence if decision == "SPLIT" else 1.0 - confidence
            return decision, round(max(0.0, min(1.0, p_split)), 4)

    label = _LABEL_RE.search(text.upper())
    if label:
        return label.group(1), 0.5

    raise JudgeError(f"无法解析模型输出: {text[:200]!r}")


def clamp(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))

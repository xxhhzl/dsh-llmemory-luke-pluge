# -*- coding: utf-8 -*-
"""事件切分器：把一段消息序列切成线性、不重叠、不留空的事件序列。

流程（命中即短路）：
    G0 硬规则      非 user/assistant → 归入当前事件；无事件 → 新事件；
                   时间间隔 ≥ gap_minutes → 软先验（交模型判，`gap_soft=False` 才硬切）；
                   事件正文 token ≥ max_event_tokens → 新事件；
                   事件消息数 ≥ max_messages → 新事件（仅紧急兜底）；
                   user 命中转折词 → 新事件；
                   assistant 应答 / 承接工具结果 → 归入当前事件。
    G1 快判        JEV 一次判定（jev.DecisionTransport / FakeTransport）
    G2 深思        灰带升级（use_autothink）
    G3 兜底        仍不确定 → CONTINUE + needs_review
    吸附           判定点不是 user 时顺延到下一条 user；没有后续 user 则合并

增量：`run(..., open_event=..., prev_message=...)` 支持只喂「新消息」——open_event 提供
已归属事件的 id / 点数 / token / 标题摘要，续写段沿用其 id；没有新消息的空段会被丢弃。

不做的：语义关联、摘要模型调用（summarize_event 目前是占位实现）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from jev import (
    FakeTransport,
    JudgeError,
    Transport,
    build_state,
    gap_minutes,
    truncate,
)

DEFAULT_MARKERS = (
    "换个话题", "换一个话题", "新任务", "另一个问题", "下一个问题",
    "重新开始", "先放一边", "另外问一下", "接下来做",
)

JUDGED_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class RuleHit:
    boundary_type: str     # session / time / time_soft / length / explicit
    reason: str
    soft: bool = False     # True：不直接切，交给模型判（软先验）


def check_rules(message: dict, prev_message: Optional[dict], *,
                has_open_event: bool, open_event_messages: int,
                gap_limit_minutes: int = 120,
                gap_soft: bool = True,
                max_event_tokens: int = 128000,
                open_event_tokens: int = 0,
                max_messages: int = 1000,
                markers=DEFAULT_MARKERS,
                assistant_reply_continues: bool = True,
                reply_window_minutes: int = 10,
                tool_followup_continues: bool = True) -> Optional[RuleHit]:
    """返回 RuleHit 表示 G0 已判定；返回 None 表示要交给模型。"""
    if message.get("role") not in JUDGED_ROLES:
        return RuleHit("tool", "非 user/assistant，归入当前事件")

    if not has_open_event:
        return RuleHit("session", "还没有未闭合事件")

    prev_gap = None
    soft_hit = None
    if prev_message is not None:
        prev_gap = gap_minutes(prev_message.get("ts", ""), message.get("ts", ""))
        if prev_gap is not None and prev_gap >= gap_limit_minutes:
            reason = f"间隔 {prev_gap:.0f} 分钟 ≥ {gap_limit_minutes}"
            if gap_soft:
                # 软先验：长间隔不等于换任务（隔夜继续同一件事很常见）→ 交模型判
                soft_hit = RuleHit("time_soft", f"{reason}，交模型判断", soft=True)
            else:
                return RuleHit("time", reason)

    # 主限制：事件正文的 token 预算
    if open_event_tokens >= max_event_tokens:
        return RuleHit("tokens", f"事件已达 {open_event_tokens} token ≥ {max_event_tokens}")

    # 紧急兜底：只在异常长的事件上触发（正常由 token 预算先命中）
    if open_event_messages >= max_messages:
        return RuleHit("length", f"事件已有 {open_event_messages} 条 ≥ {max_messages}")

    # 转折词只在 user 发言上生效：assistant 会复述用户原话（总结里出现「换个话题」
    # 「新任务」），误命中会把边界顺延到下一条 user，凭空造出假边界（实测 22 次）。
    if message.get("role") == "user":
        content = str(message.get("content", ""))
        for marker in markers:
            if marker in content:
                return RuleHit("explicit", f"命中转折词「{marker}」")

    # 结构性先验：assistant 紧跟 user 的回答，默认属于同一事件。
    # 实测中 assistant 应答常被模型判成 SPLIT，把「回答」从「提问」里拆出去。
    if (assistant_reply_continues and message.get("role") == "assistant"
            and prev_message is not None
            and prev_message.get("role") == "user"
            and (prev_gap is None or prev_gap <= reply_window_minutes)):
        return RuleHit("reply", "assistant 直接应答上一条 user，归入同一事件")

    # 工具结果之后的 assistant 是对工具输出的加工，属于同一过程。
    if (tool_followup_continues and message.get("role") == "assistant"
            and prev_message is not None
            and prev_message.get("role") == "tool"):
        return RuleHit("tool_followup", "assistant 承接工具结果，归入同一事件")

    return soft_hit


_CJK_START = "\u4e00"
_CJK_END = "\u9fff"


def estimate_tokens(text: str) -> int:
    """轻量 token 估算：CJK 1 字≈1 token，其它 4 字符≈1 token，向上取整。"""
    text = str(text or "")
    if not text:
        return 0
    cjk = sum(1 for ch in text if _CJK_START <= ch <= _CJK_END)
    other = len(text) - cjk
    return cjk + (other + 3) // 4


@dataclass
class SegmentConfig:
    gap_limit_minutes: int = 120
    gap_soft: bool = True              # 长间隔只作软先验：交模型判，不直接切
    max_event_tokens: int = 128000     # 主限制：事件正文 token 预算
    max_messages_per_event: int = 1000  # 紧急兜底：只在异常长事件触发

    tau_user: float = 0.75          # user 判 SPLIT 的门槛
    tau_assistant: float = 0.85     # assistant 判 SPLIT 的门槛（更高，避免过切）
    tau_continue: float = 0.35      # 低于它直接 CONTINUE

    tau2_split: float = 0.70        # G2（深思）后的 SPLIT 门槛
    tau2_continue: float = 0.40

    use_autothink: bool = True      # 灰带时是否开 G2
    context_history: int = 6        # 上下文里带多少条最近消息
    markers: tuple = DEFAULT_MARKERS

    # 结构性先验：assistant 紧跟 user 的回答默认同事件（实测可避免把回答拆出去）
    assistant_reply_continues: bool = True
    reply_window_minutes: int = 10
    # assistant 承接工具结果，默认同事件
    tool_followup_continues: bool = True

    # 工具信息的呈现方式：raw（原文）/ generic（调用工具）/ digest（工具名+参数摘要、结果带状态）
    context_mode: str = "raw"

    # token 预算的计数范围：dialog（只数 user/assistant 文本）/ all（所有消息，含工具输出）
    token_scope: str = "dialog"

    # 边界吸附：判定点不是 user 时，顺延到下一条 user；没有后续 user 则合并
    snap_to_user: bool = True


@dataclass
class Decision:
    message_id: str
    role: str
    seq: int
    decision: str                   # CONTINUE / SPLIT
    source: str                     # rule / jev / escalated / fallback
    stage: str                      # g0 / g1 / g2 / g3
    p_split: Optional[float] = None
    model: str = ""
    latency_ms: int = 0
    needs_review: bool = False
    boundary_type: str = ""         # session / time / time_soft / length / explicit / snap
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "seq": self.seq,
            "decision": self.decision,
            "source": self.source,
            "stage": self.stage,
            "p_split": self.p_split,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "needs_review": self.needs_review,
            "boundary_type": self.boundary_type,
            "reason": self.reason,
        }


@dataclass
class Event:
    order: int
    message_ids: list = field(default_factory=list)
    seqs: list = field(default_factory=list)
    title: str = ""
    summary: str = ""
    closed: bool = False
    tokens: int = 0          # 事件正文的 token 估算累计
    event_id: int | None = None   # 非 None 表示这是「已存在事件的续写段」

    @property
    def start_seq(self) -> int:
        return self.seqs[0]

    @property
    def end_seq(self) -> int:
        return self.seqs[-1]

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "event_id": self.event_id,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "message_ids": list(self.message_ids),
            "message_count": len(self.message_ids),
            "tokens": self.tokens,
            "title": self.title,
            "summary": self.summary,
            "closed": self.closed,
            "phase_marks": {
                "start": self.message_ids[0] if self.message_ids else None,
                "end": self.message_ids[-1] if self.message_ids else None,
            },
        }


@dataclass
class SegmentationResult:
    events: list
    decisions: list

    def to_dict(self) -> dict:
        return {
            "events": [event.to_dict() for event in self.events],
            "decisions": [decision.to_dict() for decision in self.decisions],
        }

    def boundaries(self) -> list:
        """预测出的边界位置（事件首条的 seq，跳过第一个事件）。"""
        return [event.start_seq for event in self.events[1:]]


def summarize_event(messages: list) -> tuple:
    """占位摘要：事件闭合时调用一次。真实实现应换成外壳代理的摘要模型。"""
    if not messages:                       # 公开函数，空表返回空标题/摘要
        return "", ""
    first_user = next((m for m in messages if m.get("role") == "user"), messages[0])
    last_assistant = next(
        (m for m in reversed(messages) if m.get("role") == "assistant"), messages[-1])
    title = truncate(str(first_user.get("content", "")).replace("\n", " "), 24, 0)
    summary = (f"{len(messages)} 条消息；起：{truncate(first_user.get('content', ''), 60, 0)}"
               f"；止：{truncate(last_assistant.get('content', ''), 60, 0)}")
    return title, summary


class Segmenter:
    def __init__(self, transport: Optional[Transport] = None,
                 config: Optional[SegmentConfig] = None):
        self.transport = transport or FakeTransport()
        self.config = config or SegmentConfig()
        self._by_id: dict = {}

    def _message_tokens(self, message: dict) -> int:
        """token 预算的计数口径：dialog 只数 user/assistant，all 连工具输出一起数。"""
        role = message.get("role")
        if self.config.token_scope == "all" or role in ("user", "assistant"):
            return estimate_tokens(message.get("content", ""))
        return 0

    # ---- 单条判定 ----

    def _judge(self, message: dict, prev_message: Optional[dict],
               event: Optional[Event]) -> Decision:
        cfg = self.config
        hit = check_rules(
            message, prev_message,
            has_open_event=event is not None,
            open_event_messages=len(event.message_ids) if event else 0,
            open_event_tokens=event.tokens if event else 0,
            gap_limit_minutes=cfg.gap_limit_minutes,
            gap_soft=cfg.gap_soft,
            max_event_tokens=cfg.max_event_tokens,
            max_messages=cfg.max_messages_per_event,
            markers=cfg.markers,
            assistant_reply_continues=cfg.assistant_reply_continues,
            reply_window_minutes=cfg.reply_window_minutes,
            tool_followup_continues=cfg.tool_followup_continues,
        )

        base = dict(message_id=message.get("msg_id", ""),
                    role=message.get("role", ""),
                    seq=int(message.get("seq", -1)))

        if hit is not None and not hit.soft:
            split = hit.boundary_type not in ("tool", "reply", "tool_followup")
            if not split:
                return Decision(decision="CONTINUE", source="rule", stage="g0",
                                boundary_type=hit.boundary_type,
                                reason=hit.reason, **base)
            return Decision(decision="SPLIT", source="rule", stage="g0",
                            boundary_type=hit.boundary_type, reason=hit.reason, **base)

        history = event.message_ids[-cfg.context_history:] if event else []
        # 这里只需要「最近几条消息」的对象，由调用方在 run() 里裁剪后传入
        state = build_state(
            message, prev_message,
            event_message_count=len(event.message_ids) if event else 0,
            event_summary=event.summary if event else "",
            event_title=event.title if event else "",
            history=[self._by_id[mid] for mid in history if mid in self._by_id],
            mode=cfg.context_mode,
        )

        decision = self._gate(state, base, message.get("role"))
        if hit is not None:                     # 软先验命中：时间间隔交模型判
            decision = replace(
                decision, reason=f"{hit.reason}；模型判 {decision.decision}")
        return decision

    def _gate(self, state: dict, base: dict, role: str) -> Decision:
        cfg = self.config
        tau_split = cfg.tau_user if role == "user" else cfg.tau_assistant

        try:
            first = self.transport.judge(state, think=False)
        except JudgeError as exc:
            return Decision(decision="CONTINUE", source="fallback", stage="g1",
                            needs_review=True, reason=f"jev_unavailable: {exc}", **base)

        if first.p_split < cfg.tau_continue:
            return Decision(decision="CONTINUE", source="jev", stage="g1",
                            p_split=first.p_split, model=first.model,
                            latency_ms=first.latency_ms, **base)
        if first.p_split > tau_split:
            return Decision(decision="SPLIT", source="jev", stage="g1",
                            p_split=first.p_split, model=first.model,
                            latency_ms=first.latency_ms, **base)

        if not cfg.use_autothink:
            return Decision(decision="CONTINUE", source="fallback", stage="g3",
                            p_split=first.p_split, needs_review=True, **base)

        try:
            second = self.transport.judge(state, think=True)
        except JudgeError as exc:
            return Decision(decision="CONTINUE", source="fallback", stage="g2",
                            p_split=first.p_split, needs_review=True,
                            reason=f"jev_unavailable: {exc}", **base)

        latency = first.latency_ms + second.latency_ms
        if second.p_split < cfg.tau2_continue:
            return Decision(decision="CONTINUE", source="escalated", stage="g2",
                            p_split=second.p_split, model=second.model,
                            latency_ms=latency, **base)
        if second.p_split > cfg.tau2_split:
            return Decision(decision="SPLIT", source="escalated", stage="g2",
                            p_split=second.p_split, model=second.model,
                            latency_ms=latency, **base)

        return Decision(decision="CONTINUE", source="fallback", stage="g3",
                        p_split=second.p_split, model=second.model,
                        latency_ms=latency, needs_review=True, **base)

    # ---- 主流程 ----

    def run(self, messages: list, *, open_event: Optional[dict] = None,
            prev_message: Optional[dict] = None) -> SegmentationResult:
        '''
        把消息序列切成事件。

        :param open_event: 增量切分时「前一段已归属事件」的状态
                           {event_id, message_count, tokens, title, summary}；
                           给出时本次输入被视为该事件的续写段，第一个事件沿用其 id
        :param prev_message: 输入之前的最后一条消息（用于 gap 与结构性先验）
        '''
        cfg = self.config
        kept = [m for m in messages if m.get("role") != "system"]   # system 不入库
        self._by_id = {m.get("msg_id"): m for m in kept}

        events: list[Event] = []
        decisions: list[Decision] = []
        current: Optional[Event] = None
        prev: Optional[dict] = prev_message
        pending_snap = False

        if open_event is not None:
            current = Event(order=1,
                            title=open_event.get("title") or "",
                            summary=open_event.get("summary") or "",
                            tokens=int(open_event.get("tokens") or 0),
                            event_id=open_event.get("event_id"))
            events.append(current)

        for index, message in enumerate(kept):
            role = message.get("role")
            base = dict(message_id=message.get("msg_id", ""), role=role,
                        seq=int(message.get("seq", -1)))

            if pending_snap and role == "user":
                # 吸附点：上一条 SPLIT 落点不是 user，边界挪到这里
                decision = Decision(decision="SPLIT", source="rule", stage="g0",
                                    boundary_type="snap",
                                    reason="边界吸附：上一条判定落点不是 user", **base)
                pending_snap = False
            elif pending_snap:
                # 吸附等待期间不再调模型，直接归入当前事件
                decision = Decision(decision="CONTINUE", source="rule", stage="g0",
                                    boundary_type="snap_wait",
                                    reason="等待吸附到下一条 user", **base)
            else:
                decision = self._judge(message, prev, current)

                if (cfg.snap_to_user and current is not None
                        and decision.decision == "SPLIT" and role != "user"):
                    has_later_user = any(m.get("role") == "user"
                                         for m in kept[index + 1:])
                    if has_later_user:
                        decision = replace(
                            decision, decision="CONTINUE",
                            boundary_type=decision.boundary_type or "snap_deferred",
                            reason=f"{decision.reason}；边界吸附到下一条 user".strip("；"))
                        pending_snap = True
                    else:
                        decision = replace(
                            decision, decision="CONTINUE",
                            boundary_type=decision.boundary_type or "snap_merged",
                            reason=f"{decision.reason}；无后续 user，合并到当前事件".strip("；"))

            decisions.append(decision)

            if current is None or decision.decision == "SPLIT":
                current = Event(order=len(events) + 1)
                events.append(current)

            current.message_ids.append(message.get("msg_id"))
            current.seqs.append(int(message.get("seq", -1)))
            current.tokens += self._message_tokens(message)
            prev = message

        # 增量切分时，续写段可能一条新消息都没有（首条就 SPLIT）：丢掉空段
        events = [event for event in events if event.message_ids]

        for event in events:
            event.closed = True
            messages_of_event = [self._by_id[mid] for mid in event.message_ids]
            # 续写段沿用原事件标题/摘要；新事件才生成占位摘要
            if event.event_id is None:
                event.title, event.summary = summarize_event(messages_of_event)

        result = SegmentationResult(events=events, decisions=decisions)
        validate(result, kept)
        return result


# ---- 不变量校验 ----

def validate(result: SegmentationResult, messages: list):
    """I1 覆盖性 / I2 连续性 / I3 闭合性。"""
    events = result.events
    if not events:
        if messages:
            raise AssertionError("有消息但没有事件")
        return

    covered = []
    for event in events:
        if not event.message_ids:
            raise AssertionError(f"事件 {event.order} 为空")
        if not event.closed:
            raise AssertionError(f"事件 {event.order} 未闭合")
        if event.seqs != sorted(set(event.seqs)):
            raise AssertionError(f"事件 {event.order} 的 seq 非严格升序")
        # seq 允许跳号：宿主的 seq 是会话内原始序号，外壳可能过滤过消息
        covered.extend(event.message_ids)

    if covered != [m.get("msg_id") for m in messages]:
        raise AssertionError("事件覆盖的消息序列与输入不一致（重复/丢失/乱序）")

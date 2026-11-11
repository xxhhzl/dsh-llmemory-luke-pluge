# -*- coding: utf-8 -*-
"""逐事件检视：把某个会话切出来的事件打出来，人工核对是否符合。

用法：
    python tests/segmentation/inspect_events.py --session <SESSION_ID> --live
    python tests/segmentation/inspect_events.py --session <SESSION_ID>     # 离线
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from segmentation.dsh_sessions import discover, load_messages
from segmentation.direct import DecisionTransport
from jev import FakeTransport
from segmenter import SegmentConfig, Segmenter


def clip(text: str, size: int = 70) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= size else text[: size - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True, help="会话 id 片段")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--think", action="store_true")
    parser.add_argument("--max-messages", type=int, default=0)
    parser.add_argument("--events", type=int, default=0, help="只看前 N 个事件")
    args = parser.parse_args()

    sessions = [s for s in discover() if args.session in s.session_id]
    if not sessions:
        print("没有匹配的会话")
        return 1
    session = sessions[0]
    messages = load_messages(session)
    if args.max_messages:
        messages = messages[: args.max_messages]
    print(f"会话: {session.project}::{session.session_id}  消息 {len(messages)} 条")

    if args.live:
        from segmentation.env import load_api_config
        transport = DecisionTransport(load_api_config())
    else:
        transport = FakeTransport()
    config = SegmentConfig(use_autothink=args.think)
    result = Segmenter(transport=transport, config=config).run(messages)

    by_id = {m["msg_id"]: m for m in messages}
    # 每个事件的首条判定就是它的边界来源
    first_decision = {}
    for decision in result.decisions:
        first_decision.setdefault(decision.message_id, decision)

    events = result.events[: args.events] if args.events else result.events
    for event in events:
        head = by_id[event.message_ids[0]]
        tail = by_id[event.message_ids[-1]]
        boundary = first_decision.get(head["msg_id"])
        source = f"{boundary.source}/{boundary.boundary_type or boundary.stage}" if boundary else "?"
        cap = "  [token上限]" if event.tokens >= config.max_event_tokens else ""
        print(f"\n#{event.order:<3} seq {event.start_seq}-{event.end_seq}  "
              f"{len(event.message_ids):>3} 条 / {event.tokens:>5} token  边界={source}{cap}")
        print(f"    首: {head['role']}: {clip(head['content'])}")
        print(f"    尾: {tail['role']}: {clip(tail['content'])}")
        print(f"    题: {clip(event.title, 40)}")

    sizes = [len(e.message_ids) for e in result.events]
    print(f"\n事件 {len(result.events)} 个；大小 min={min(sizes)} "
          f"p50={sorted(sizes)[len(sizes)//2]} max={max(sizes)}；"
          f"边界 {len(result.boundaries())} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

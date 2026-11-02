# -*- coding: utf-8 -*-
"""把一个会话的事件切分导出成逐行文本。

格式（每条消息一行）：
    角色：正文 ---- 事件N 开始/过程/结束

正文会压缩换行、按 head+tail 截断，保证一行一条。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from segmentation.dsh_sessions import discover, load_messages
from segmentation.direct import DecisionTransport
from jev import FakeTransport, truncate
from segmenter import SegmentConfig, Segmenter


def clip(text: str, head: int, tail: int) -> str:
    text = " ".join(str(text or "").replace("\r", " ").split())
    return truncate(text, head, tail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True, help="会话 id 片段")
    parser.add_argument("--live", action="store_true", help="用真实 StartLux 判定")
    parser.add_argument("--out", default=None)
    parser.add_argument("--head", type=int, default=200)
    parser.add_argument("--tail", type=int, default=200)
    parser.add_argument("--max-messages", type=int, default=0)
    args = parser.parse_args()

    sessions = [s for s in discover() if args.session in s.session_id]
    if not sessions:
        print("没有匹配的会话")
        return 1
    session = sessions[0]
    messages = load_messages(session)
    if args.max_messages:
        messages = messages[: args.max_messages]
    if not messages:
        print("会话没有可解析消息")
        return 1

    if args.live:
        from segmentation.env import load_api_config
        transport = DecisionTransport(load_api_config())
    else:
        transport = FakeTransport()

    result = Segmenter(transport=transport,
                       config=SegmentConfig(use_autothink=False)).run(messages)

    status = {}
    for event in result.events:
        total = len(event.message_ids)
        for index, msg_id in enumerate(event.message_ids):
            if total == 1:
                phase = "开始/结束"
            elif index == 0:
                phase = "开始"
            elif index == len(event.message_ids) - 1:
                phase = "结束"
            else:
                phase = "过程"
            status[msg_id] = f"事件{event.order} {phase}"

    lines = []
    for message in messages:
        state = status.get(message["msg_id"], "未归属")
        body = clip(message["content"], args.head, args.tail)
        lines.append(f"{message['role']}：{body} ---- {state}")

    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / "tests" / "output"
        / f"segmentation_{session.session_id[:20]}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"会话: {session.project}::{session.session_id}")
    print(f"消息 {len(messages)} 条，事件 {len(result.events)} 个，边界 {len(result.boundaries())} 个")
    print(f"输出: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

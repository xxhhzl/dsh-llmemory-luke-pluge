# -*- coding: utf-8 -*-
"""读取 DSH 的会话日志（~/.dsh/sessions/**/session.v*.jsonl.zstd）并转成切分输入。

事件映射（只取与「对话消息」有关的类型）：
    user/message      → role=user（只保留 source.kind == "user"）
    assistant/message → role=assistant（text 块 + tool-call 摘要行，跳过 reasoning）
    tool/result       → role=tool
    system/message    → 丢弃（按协议：system 不入库、不绑事件）

content 保留原文（不预先截断）；发送给模型时由 judge.build_state 统一「头 200 + 尾 200」。

zstd 解压走外部命令（msys64 自带 zstd），逐行流式读取，不落中间文件。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TOOL_ARGS_HEAD = 200       # 工具调用参数的展示上限（只截参数，不截工具名）
_TZ = timezone(timedelta(hours=8))


def find_zstd() -> str:
    for candidate in ("zstd", r"C:\msys64\ucrt64\bin\zstd.exe",
                      r"C:\msys64\usr\bin\zstd.exe"):
        found = shutil.which(candidate) if not os.path.isabs(candidate) else (
            candidate if os.path.exists(candidate) else None)
        if found:
            return found
    raise RuntimeError("找不到 zstd 可执行文件")


@dataclass(frozen=True)
class SessionFile:
    path: Path
    project: str
    session_id: str


def default_sessions_root() -> Path:
    return Path(os.path.expanduser("~")) / ".dsh" / "sessions"


def discover(root: Path | None = None) -> list:
    root = Path(root) if root else default_sessions_root()
    found = []
    for path in sorted(root.glob("*/*/session.v*.jsonl.zstd")):
        project = path.parent.parent.name
        session_id = path.parent.name
        found.append(SessionFile(path=path, project=project, session_id=session_id))
    return found


def _iso_from_ms(value) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=_TZ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return datetime.now(tz=_TZ).isoformat(timespec="seconds")


def _text_of(content) -> str:
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts).strip()


def _tool_call_line(block: dict) -> str:
    """把 tool-call 块压成一行伪文本：[tool-call] 名称 参数（参数只截头部）。"""
    name = block.get("name") or "?"
    raw = block.get("arguments")
    if isinstance(raw, str):
        args = raw
    elif raw is None:
        args = ""
    else:
        args = json.dumps(raw, ensure_ascii=False)
    args = " ".join(args.split())
    if len(args) > _TOOL_ARGS_HEAD:
        args = args[:_TOOL_ARGS_HEAD] + "…"
    return f"[tool-call] {name} {args}".rstrip()


def _assistant_content(content) -> str:
    """assistant 的 text 块 + tool-call 摘要行；reasoning 不输出。"""
    texts, calls = [], []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool-call":
            calls.append(_tool_call_line(block))
    text = "\n".join(part for part in texts if part).strip()
    if calls:
        text = (text + "\n" if text else "") + "\n".join(calls)
    return text


# 只有 source.kind == "user" 才是真人输入；其余是系统注入
# （time-context / runtime-context / model-selection / skill-catalog / compact-checkpoint / goal …）
KEEP_SOURCE_KINDS = frozenset({"user"})


def _is_real_user_message(source) -> bool:
    """缺 kind 的老日志保留；已知的合成 kind 一律丢弃。"""
    kind = (source or {}).get("kind")
    if kind in (None, ""):
        return True
    return kind in KEEP_SOURCE_KINDS


def event_to_message(event: dict, session_id: str, index: int,
                     keep_all_users: bool = False):
    """把一个 DSH 日志事件转成消息；不该进切分的返回 None。

    :param keep_all_users: 诊断用：True 时不过滤合成 user 消息（对照组）
    """
    kind = event.get("type")
    data = event.get("data") or {}

    if kind == "user/message":
        if not keep_all_users and not _is_real_user_message(data.get("source")):
            return None
        text = _text_of(data.get("content"))
        role, msg_id = "user", data.get("id")
    elif kind == "assistant/message":
        message = data.get("message") or {}
        text = _assistant_content(message.get("content"))
        role, msg_id = "assistant", message.get("id")
    elif kind == "tool/result":
        message = data.get("message") or {}
        text = _text_of(message.get("content"))
        role, msg_id = "tool", data.get("toolCallId") or message.get("id")
        error = data.get("error") or message.get("error")
    else:
        return None

    if not text:
        return None
    record = {
        "msg_id": str(msg_id or f"{session_id}-{index}"),
        "seq": index,
        "role": role,
        "content": text,
        "ts": _iso_from_ms(event.get("time")),
    }
    if kind == "tool/result" and error:
        record["error"] = error
    return record


def load_messages(session: SessionFile, zstd: str | None = None,
                  keep_all_users: bool = False) -> list:
    """把一个会话日志解析成 [{msg_id, seq, role, content, ts}]。"""
    zstd = zstd or find_zstd()
    messages = []
    proc = subprocess.Popen([zstd, "-dc", str(session.path)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for raw in proc.stdout:
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            message = event_to_message(event, session.session_id, len(messages),
                                       keep_all_users=keep_all_users)
            if message is not None:
                messages.append(message)
    finally:
        proc.stdout.close()
        proc.wait()
    return messages


def to_fixture(sessions: list, zstd: str | None = None) -> list:
    """转成 run_demo 能吃的行：{id, messages, expected_boundaries}。"""
    rows = []
    for session in sessions:
        messages = load_messages(session, zstd=zstd)
        if not messages:
            continue
        rows.append({
            "id": f"{session.project}::{session.session_id}",
            "messages": messages,
            "expected_boundaries": [],      # 真实日志没有人工边界标注
            "source": str(session.path),
        })
    return rows

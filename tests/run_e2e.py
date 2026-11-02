# -*- coding: utf-8 -*-
"""端到端跑通路：真实会话数据 + 本地模型 + 模拟外壳。

用法：
    python tests/run_e2e.py --session <SESSION_ID> --limit 30
    python tests/run_e2e.py --session <SESSION_ID> --limit 30 --keep

流程：加载真实会话 → ingest.request → 外壳代答 llm.request →
等 ingest.completed → tools.calls(memory_search / memory_get) → 直接查库核对。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from segmentation.dsh_sessions import discover, load_messages   # noqa: E402
from shell_sim import ShellSim                                   # noqa: E402


def pick_session(pattern: str):
    sessions = discover(None)
    matches = [s for s in sessions if pattern.lower() in s.session_id.lower()]
    if not matches:
        raise SystemExit(f"没有匹配 {pattern!r} 的会话（共 {len(sessions)} 个）")
    return matches[0]


def to_frame_message(item: dict) -> dict:
    return {
        "msg_id": item["msg_id"],
        "seq": int(item["seq"]),
        "role": item["role"],
        "content": item["content"],
        "ts": item["ts"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", default="<SESSION_ID>")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--model", default="glm-5.3")
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--keep", action="store_true", help="保留工作目录")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--contradiction", action="store_true",
                        help="追加一条与既有认知冲突的消息，验证 supersede")
    args = parser.parse_args()

    session = pick_session(args.session)
    messages = load_messages(session)
    if args.limit:
        messages = messages[: args.limit]
    if not messages:
        raise SystemExit("会话没有可解析消息")
    print(f"会话 {session.session_id}｜截取 {len(messages)} 条消息"
          f"（seq {messages[0]['seq']}–{messages[-1]['seq']}）")

    workdir = Path(args.workdir) if args.workdir else (
        ROOT / ".tmp" / "e2e" / datetime.now().strftime("%Y%m%d-%H%M%S") )
    workdir.mkdir(parents=True, exist_ok=True)

    if args.contradiction:
        # 预置一条旧认知，稍后用「换库」消息验证 supersede
        from database import Cognition, Database
        seed_db = Database(workdir / ".dsh-longmemory" / "memory.db")
        seed_db.insert_cognition(Cognition(
            subject="dsh-longmemory-plugin", content="数据库用 SQLite，尚未迁移 Postgres",
            kind="constraint", importance=5, confidence=0.9,
            origin_session="seed", source_event_id=1, cues=("数据库", "postgres"),
            created_at=datetime.now().astimezone().isoformat()))
        seed_db.close()
        last = messages[-1]
        seq = int(last["seq"])
        messages = messages + [
            {"msg_id": f"m_{seq + 1}", "seq": seq + 1, "role": "user",
             "content": "换个话题：项目数据库已经从 SQLite 换成 Postgres 了，以后都按 Postgres 来。",
             "ts": last["ts"]},
            {"msg_id": f"m_{seq + 2}", "seq": seq + 2, "role": "assistant",
             "content": "收到，已记录：数据库改用 Postgres，后续按 Postgres 处理。",
             "ts": last["ts"]},
        ]

    shell = ShellSim(workdir, model=args.model, session_id=session.session_id)
    shell.start()
    print(f"工作目录：{workdir}")

    try:
        payload = {
            "turn_id": "t_e2e",
            "trigger": "command",
            "full": True,
            "cursor": {"last_seq": -1},
            "messages": [to_frame_message(m) for m in messages],
        }
        shell.send("ingest.request", payload)
        receipt = shell.wait("ingest.receipt", timeout=30)
        print("receipt:", json.dumps(receipt["data"], ensure_ascii=False)
              if receipt else "（超时）")

        completed = shell.wait("ingest.completed", timeout=args.timeout)
        if completed is None:
            print("ingest.completed 超时，查看 kernel.stderr.log")
            return 2
        data = completed["data"]
        print("completed.summary:", json.dumps(data["summary"], ensure_ascii=False))
        print("display:", data["display"])
        print("digest_tokens:", data["digest_tokens"])
        print("digest:\n" + (data.get("digest") or "（空）")[:1200])

        db_path = workdir / ".dsh-longmemory" / "memory.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        counts = {name: conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
                  for name in ("message", "event", "experience", "cognition", "digest", "memory_fts")}
        print("库计数:", counts)
        first_event = conn.execute("SELECT id, title, summary FROM event ORDER BY id LIMIT 1").fetchone()
        conn.close()

        base_query = messages[0]["content"][:24]
        items = []
        for query in (base_query, base_query[:4], base_query[:2]):
            shell.send("tools.calls", {"calls": [
                {"call_id": "c1", "name": "memory_search",
                 "arguments": {"query": query}},
            ]})
            result = shell.wait("tools.result", timeout=120)
            if result:
                items = result["data"][0]["data"]["items"]
            print(f"memory_search({query!r}) -> {len(items)} 条")
            if items:
                for item in items[:5]:
                    print(f"  [{item['kind']}#{item['id']}] {item['title'][:24]} | "
                          f"{item['text'][:60]} | score={item['score']}")
                break

        if first_event:
            shell.send("tools.calls", {"calls": [
                {"call_id": "c2", "name": "memory_get",
                 "arguments": {"id": first_event["id"], "kind": "event"}},
            ]})
            got = shell.wait("tools.result", timeout=60)
            if got and got["data"][0]["ok"]:
                payload = got["data"][0]["data"]
                print(f"memory_get(event {first_event['id']}) -> "
                      f"{len(payload['messages'])} 条正文")
            else:
                print("memory_get 失败或超时")

        if args.contradiction:
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT id, content, status, superseded_id FROM cognition "
                "WHERE content LIKE '%Postgres%' OR content LIKE '%SQLite%' "
                "ORDER BY id").fetchall()
            print("supersede 检查:")
            for row in rows:
                print(f"  #{row['id']} [{row['status']}] superseded_id={row['superseded_id']} | "
                      f"{row['content'][:50]}")
            c.close()

        print(f"模型调用 {shell.model_calls} 次；错误 {len(shell.model_errors)} 次")
        if shell.model_errors:
            print("首个错误:", shell.model_errors[0][:200])
        return 0
    finally:
        shell.close()
        if args.keep:
            print(f"保留工作目录：{workdir}")
        else:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

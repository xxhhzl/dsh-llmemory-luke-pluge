# -*- coding: utf-8 -*-
"""统计工具调用情况：给「工具描述 / 系统提示规则」的 A/B 评估用。

用法：
    python scripts/tool_usage.py                 # 看全库
    python scripts/tool_usage.py --since 2026-11-08T17:00:00+08:00
    python scripts/tool_usage.py --json          # 机器可读（方便前后对比）

指标口径：
- 工具调用次数：从 message 正文里的 `[tool-call] <name>` 提取（会话原样落库）；
- 记忆使用：experience/cognition/insight 的 use_count 与 event.cnt
  （memory_search / memory_get 命中后会 +1，是最硬的「真的用上了」证据）；
- 只读：用 `mode=ro` 打开，不改库。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".dsh-longmemory" / "harness" / "memory.db"
MEMORY_TOOLS = ("memory_search", "memory_get", "memory_recent",
                "memory_report", "memory_self_write")
_CALL_RE = re.compile(r"\[tool-call\]\s+([A-Za-z0-9_\-]+)")


def collect(db_path: Path, since: str | None = None) -> dict:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = "SELECT content FROM message WHERE content LIKE '%[tool-call]%'"
        params: list = []
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        calls: Counter = Counter()
        for row in conn.execute(sql, params):
            for name in _CALL_RE.findall(row["content"]):
                calls[name] += 1

        used = {}
        for table in ("experience", "cognition", "insight", "event"):
            field = "cnt" if table == "event" else "use_count"
            row = conn.execute(
                f"SELECT COUNT(*) n, SUM(CASE WHEN {field} > 0 THEN 1 ELSE 0 END) used, "
                f"MAX({field}) mx, MAX(last_used_at) last FROM {table}").fetchone()
            used[table] = {"total": int(row["n"] or 0),
                           "used": int(row["used"] or 0),
                           "max_count": int(row["mx"] or 0),
                           "last_used_at": row["last"] or ""}

        total = conn.execute("SELECT COUNT(*) n FROM message").fetchone()["n"]
        return {
            "db": str(db_path),
            "since": since or "",
            "messages": int(total),
            "calls": dict(calls.most_common()),
            "memory_calls": sum(calls.get(name, 0) for name in MEMORY_TOOLS),
            "used": used,
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统计工具调用与记忆使用情况")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since", default="", help="只统计 created_at >= 该时间的消息")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return 2
    data = collect(db_path, args.since or None)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"库：{data['db']}（{data['messages']} 条消息"
          + (f"；since {data['since']}" if data["since"] else "") + "）")
    print("\n工具调用次数：")
    if not data["calls"]:
        print("  （没有工具调用记录）")
    for name, count in data["calls"].items():
        mark = "  ← 记忆工具" if name in MEMORY_TOOLS else ""
        print(f"  {name}: {count}{mark}")
    print(f"\n记忆工具合计：{data['memory_calls']} 次")
    print("\n记忆条目使用情况（检索命中才会 +1）：")
    for table, item in data["used"].items():
        print(f"  {table:11s} 共 {item['total']:4d} 条，被用过 {item['used']:4d} 条，"
              f"最大计数 {item['max_count']}，最近 {item['last_used_at'] or '—'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

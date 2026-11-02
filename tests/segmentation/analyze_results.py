# -*- coding: utf-8 -*-
"""分析 run_dsh.py 的产物：汇总、分位数、异常会话。"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))], 1)


def main(run_dir: str | None = None) -> int:
    base = Path(__file__).resolve().parents[2] / ".tmp" / "segmentation_live"
    target = Path(run_dir) if run_dir else sorted(base.glob("*"))[-1]
    rows = [json.loads(line) for line in
            (target / "sessions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = json.loads((target / "summary.json").read_text(encoding="utf-8")) if (
        target / "summary.json").exists() else {}

    latencies = [v for row in rows for v in row.get("latency_ms") or []]
    sources = {}
    for row in rows:
        for key, value in (row.get("sources") or {}).items():
            sources[key] = sources.get(key, 0) + value

    calls = sum(row.get("decisions", 0) - (row.get("sources") or {}).get("rule", 0) for row in rows)
    messages = sum(row.get("messages", 0) for row in rows)
    events = sum(row.get("events", 0) for row in rows)
    boundaries = sum(len(row.get("boundaries") or []) for row in rows)
    review = sum(row.get("needs_review", 0) for row in rows)
    errors = [row for row in rows if row.get("error")]

    print(f"目录: {target}")
    print(f"会话 {len(rows)}｜消息 {messages}｜事件 {events}｜边界 {boundaries}")
    print(f"模型调用 {calls}（占消息 {calls / messages:.1%}）｜需复核 {review}"
          f"（占调用 {review / calls:.1%}）" if calls else "无调用")
    print("来源分布:", sources)
    print(f"延迟 p50={percentile(latencies, .5)}ms p95={percentile(latencies, .95)}ms "
          f"p99={percentile(latencies, .99)}ms max={max(latencies) if latencies else 0}ms")
    if errors:
        print(f"失败会话 {len(errors)}：")
        for row in errors[:5]:
            print("  -", row.get("id"), "→", str(row.get("error"))[:160])

    print("\n事件粒度（messages/events）：")
    ratios = [row["messages"] / row["events"] for row in rows if row.get("events")]
    print(f"  p50={percentile(ratios, .5)} p95={percentile(ratios, .95)} max={max(ratios):.1f}")

    print("\n调用量 Top 8：")
    ranked = sorted(rows, key=lambda r: r.get("decisions", 0) - (r.get("sources") or {}).get("rule", 0),
                    reverse=True)
    for row in ranked[:8]:
        used = row.get("decisions", 0) - (row.get("sources") or {}).get("rule", 0)
        print(f"  {used:4d} 次  {row['id'][:70]}  事件={row['events']} 边界={len(row.get('boundaries') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))

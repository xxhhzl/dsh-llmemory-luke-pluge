# -*- coding: utf-8 -*-
"""用 DSH 的真实会话日志跑切分：先统计，再可选打真实模型。

用法：
    python tests/segmentation/run_dsh.py --stats              # 只统计（不联网）
    python tests/segmentation/run_dsh.py --live --jobs 6      # 打真实 StartLux
    python tests/segmentation/run_dsh.py --live --limit 5     # 先跑 5 个会话

产物：.tmp/segmentation_live/<时间戳>/sessions.jsonl + summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from segmentation.dsh_sessions import discover, load_messages
from segmentation.direct import DecisionTransport
from jev import JudgeResult, FakeTransport, JudgeError
from segmenter import SegmentConfig, Segmenter


def percentile(values: list, ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return round(ordered[index], 1)


class NullTransport:
    """只统计候选调用数：固定给低分，不联网、不存状态。"""

    def __init__(self):
        self.count = 0

    def judge(self, state, *, think=False):
        self.count += 1
        return JudgeResult(decision="CONTINUE", p_split=0.05, stage="g1",
                           model="null", latency_ms=0, raw="")


def summarize(result, latency: list, error: str | None = None) -> dict:
    sources = {}
    review = 0
    for decision in result.decisions if result else []:
        sources[decision.source] = sources.get(decision.source, 0) + 1
        review += 1 if decision.needs_review else 0
    return {
        "events": len(result.events) if result else 0,
        "boundaries": result.boundaries() if result else [],
        "decisions": len(result.decisions) if result else 0,
        "sources": sources,
        "needs_review": review,
        "latency_ms": latency,
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=None, help="会话根目录，默认 ~/.dsh/sessions")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个会话")
    parser.add_argument("--max-messages", type=int, default=0, help="每个会话截断到 N 条")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--think", action="store_true", help="允许 G2")
    parser.add_argument("--stats", action="store_true", help="只统计，不联网")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    sessions = discover(Path(args.root) if args.root else None)
    if args.limit:
        sessions = sessions[: args.limit]
    print(f"发现会话 {len(sessions)} 个")

    # 先解析（单线程，zstd 解压本身很快）
    batches = []
    for index, session in enumerate(sessions, 1):
        try:
            messages = load_messages(session)
        except Exception as exc:
            print(f"  [{index}/{len(sessions)}] 解析失败 {session.session_id}: {exc}")
            continue
        if args.max_messages:
            messages = messages[: args.max_messages]
        if messages:
            batches.append((session, messages))
    total_messages = sum(len(messages) for _, messages in batches)
    print(f"可解析会话 {len(batches)} 个，消息 {total_messages} 条")
    if not batches:
        return 0

    if args.stats or not args.live:
        transport = NullTransport()
        config = SegmentConfig(use_autothink=False)
    else:
        from segmentation.env import load_api_config
        transport = DecisionTransport(load_api_config())
        config = SegmentConfig(use_autothink=args.think)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[2] / ".tmp" / "segmentation_live" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "sessions.jsonl"

    started = time.monotonic()
    rows = []
    calls = 0

    def run_one(item):
        session, messages = item
        latencies = []
        error = None
        result = None
        for attempt in (1, 2):
            try:
                # 每个会话独立 Segmenter，共享无状态 transport
                seg = Segmenter(transport=transport, config=config)
                result = seg.run(messages)
                break
            except JudgeError as exc:
                error = str(exc)
                if attempt == 1:
                    time.sleep(2)
                    continue
                result = None
            except AssertionError as exc:
                error = f"invariant: {exc}"
                break
        if result is not None:
            latencies = [d.latency_ms for d in result.decisions if d.latency_ms]
            error = None
        return session, len(messages), result, latencies, error

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(run_one, item): item for item in batches}
        for done, future in enumerate(as_completed(futures), 1):
            session, count, result, latencies, error = future.result()
            row = {
                "id": f"{session.project}::{session.session_id}",
                "messages": count,
                "source": str(session.path),
                **summarize(result, latencies, error),
            }
            rows.append(row)
            if args.live and result is not None:
                calls += row["decisions"] - row["sources"].get("rule", 0)
            if done % 10 == 0 or done == len(futures):
                print(f"  进度 {done}/{len(futures)}", flush=True)

    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    latencies = [value for row in rows for value in row["latency_ms"]]
    sources = {}
    for row in rows:
        for key, value in row["sources"].items():
            sources[key] = sources.get(key, 0) + value
    errors = [row for row in rows if row["error"]]
    summary = {
        "mode": "live" if args.live else "stats",
        "sessions": len(rows),
        "messages": sum(row["messages"] for row in rows),
        "events": sum(row["events"] for row in rows),
        "boundaries": sum(len(row["boundaries"]) for row in rows),
        "decisions": sum(row["decisions"] for row in rows),
        "sources": sources,
        "needs_review": sum(row["needs_review"] for row in rows),
        "errors": len(errors),
        "error_samples": [row["error"] for row in errors[:5]],
        "latency_p50_ms": percentile(latencies, 0.5),
        "latency_p95_ms": percentile(latencies, 0.95),
        "elapsed_s": round(time.monotonic() - started, 1),
        "out": str(out_path),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总 ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

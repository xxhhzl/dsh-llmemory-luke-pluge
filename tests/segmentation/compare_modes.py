# -*- coding: utf-8 -*-
"""A/B/C 对比：工具信息三种呈现方式，真实调用切分模型。

    raw      工具调用原文 + 工具结果头200/尾200（现状）
    generic  工具调用 → 「调用工具」；结果头200/尾200（你的方案）
    digest   工具名+参数摘要；结果带状态（你的方案上加强）

同一批会话、同一模型、同一规则与阈值，只改模型输入。

用法：
    python tests/segmentation/compare_modes.py --sessions 8 --jobs 4
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from segmentation.dsh_sessions import discover, load_messages
from segmentation.env import load_api_config
from segmentation.direct import DecisionTransport
from jev import JudgeResult
from segmenter import SegmentConfig, Segmenter

MODES = ("raw", "generic", "digest")


class NullTransport:
    def judge(self, state, *, think=False):
        return JudgeResult("CONTINUE", 0.05)


def candidate_count(messages) -> int:
    seg = Segmenter(transport=NullTransport(), config=SegmentConfig(use_autothink=False))
    return sum(1 for d in seg.run(messages).decisions if d.source != "rule")


def jaccard(a, b) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return round(len(sa & sb) / len(sa | sb), 3)


def run_mode(transport, messages, mode: str) -> dict:
    started = time.monotonic()
    seg = Segmenter(transport=transport,
                    config=SegmentConfig(use_autothink=False, context_mode=mode))
    result = seg.run(messages)
    sources = {}
    p_values = []
    model_splits = []
    rule_splits = []
    detail = []
    for decision in result.decisions:
        sources[decision.source] = sources.get(decision.source, 0) + 1
        if decision.p_split is not None:
            p_values.append(decision.p_split)
        detail.append({
            "seq": decision.seq, "role": decision.role,
            "decision": decision.decision, "source": decision.source,
            "p_split": decision.p_split, "needs_review": decision.needs_review,
        })
        if decision.decision == "SPLIT":
            (rule_splits if decision.source == "rule" else model_splits).append(decision.seq)
    return {
        "events": len(result.events),
        "boundaries": result.boundaries(),
        "model_boundaries": model_splits,
        "rule_boundaries": rule_splits,
        "decisions": len(result.decisions),
        "model_splits": len(model_splits),
        "rule_splits": len(rule_splits),
        "sources": sources,
        "needs_review": sum(1 for d in result.decisions if d.needs_review),
        "p_mean": round(statistics.fmean(p_values), 4) if p_values else None,
        "latency_ms": [d.latency_ms for d in result.decisions if d.latency_ms],
        "detail": detail,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--min-candidates", type=int, default=5)
    parser.add_argument("--max-candidates", type=int, default=80)
    args = parser.parse_args()

    # 1) 选会话：候选数在区间内，按大小排序后均匀取样，避免只挑小会话
    pool = []
    for session in discover():
        messages = load_messages(session)
        if not messages:
            continue
        count = candidate_count(messages)
        if args.min_candidates <= count <= args.max_candidates:
            pool.append((count, session, messages))
    if not pool:
        print("没有符合条件的会话")
        return 1
    pool.sort(key=lambda item: item[0])
    step = max(1, len(pool) // args.sessions)
    picked = pool[::step][:args.sessions]
    print("选中会话：")
    for count, session, _ in picked:
        print(f"  {count:3d} 次候选  {session.project}::{session.session_id}")
    total_candidates = sum(c for c, _, _ in picked)
    print(f"合计候选 {total_candidates} 次 × {len(MODES)} 组 = {total_candidates * len(MODES)} 次调用")

    transport = DecisionTransport(load_api_config())
    results = {mode: {} for mode in MODES}
    tasks = [(mode, session, messages) for _, session, messages in picked for mode in MODES]

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool_exec:
        futures = {pool_exec.submit(run_mode, transport, messages, mode): (mode, session)
                   for mode, session, messages in tasks}
        for done, future in enumerate(as_completed(futures), 1):
            mode, session = futures[future]
            key = f"{session.project}::{session.session_id}"
            try:
                results[mode][key] = future.result()
            except Exception as exc:
                results[mode][key] = {"error": f"{type(exc).__name__}: {exc}"}
            if done % 6 == 0 or done == len(futures):
                print(f"  进度 {done}/{len(futures)}", flush=True)

    # 2) 汇总
    print("\n=== 对比 ===")
    header = (f"{'mode':8s} {'调用':>5s} {'模型SPLIT':>9s} {'规则SPLIT':>9s} {'灰带':>5s} "
              f"{'事件':>5s} {'p均值':>7s} {'延迟':>8s}")
    print(header)
    for mode in MODES:
        rows = [row for row in results[mode].values() if "error" not in row]
        calls = sum(row["decisions"] - row["sources"].get("rule", 0) for row in rows)
        model_splits = sum(row["model_splits"] for row in rows)
        rule_splits = sum(row["rule_splits"] for row in rows)
        review = sum(row["needs_review"] for row in rows)
        events = sum(row["events"] for row in rows)
        p_means = [row["p_mean"] for row in rows if row["p_mean"] is not None]
        latency = [v for row in rows for v in row["latency_ms"]]
        errors = sum(1 for row in results[mode].values() if "error" in row)
        print(f"{mode:8s} {calls:5d} {model_splits:9d} {rule_splits:9d} {review:5d} "
              f"{events:5d} {(statistics.fmean(p_means) if p_means else 0):7.3f} "
              f"{(statistics.fmean(latency) if latency else 0):7.0f}ms"
              + (f"  errors={errors}" if errors else ""))

    # 3) 只比较「模型判出的边界」（规则边界与输入模式无关）
    print("\n模型边界一致性（Jaccard，仅 source != rule，按会话）：")
    keys = sorted(results["raw"])
    for mode in ("generic", "digest"):
        scores = [jaccard(results["raw"][k]["model_boundaries"],
                          results[mode][k]["model_boundaries"])
                  for k in keys
                  if "error" not in results["raw"][k] and "error" not in results[mode][k]]
        if scores:
            print(f"  raw vs {mode:8s}: mean={statistics.fmean(scores):.3f} "
                  f"min={min(scores)} max={max(scores)}")
    print("\n各会话模型边界数（raw / generic / digest）：")
    for key in keys:
        counts = [len(results[m][key].get("model_boundaries", []))
                  for m in MODES]
        print(f"  {counts[0]:2d} / {counts[1]:2d} / {counts[2]:2d}  {key[:70]}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(__file__).resolve().parents[2] / ".tmp" / "segmentation_live" / f"modes-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps({"picked": [f"{s.project}::{s.session_id}" for _, s, _ in picked],
                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果: {out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

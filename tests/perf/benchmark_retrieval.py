# -*- coding: utf-8 -*-
"""在隔离合成库上测量 SQLite 热态检索性能。"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import retrieval  # noqa: E402
from database import Database, Experience  # noqa: E402

PERF_DIR = Path(__file__).resolve().parent


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * ratio
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def summarize(values: list[float]) -> dict:
    return {
        "samples": len(values),
        "mean_ms": round(statistics.fmean(values), 4) if values else 0.0,
        "p50_ms": round(percentile(values, 0.50), 4),
        "p95_ms": round(percentile(values, 0.95), 4),
        "p99_ms": round(percentile(values, 0.99), 4),
        "max_ms": round(max(values), 4) if values else 0.0,
    }


def build_database(path: Path, size: int) -> list[int]:
    db = Database(path)
    ids: list[int] = []
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        with db.transaction():
            for index in range(size):
                topic = index % 100
                content = f"性能基准主题{topic:04d}的记忆条目{index:07d}，执行索引重建后检索"
                item = Experience(
                    context=f"性能测试{topic:04d}", content=content,
                    kind="technique", confidence=0.9, importance=3,
                    origin_project="benchmark", source_event_id=index + 1,
                    cues=(f"主题{topic:04d}",), created_at=created_at)
                item_id = db.insert_experience(item)
                db.index_fts("experience", item_id, item.context or "", item.content)
                ids.append(item_id)
    finally:
        db.close()
    return ids


def _time_call(func) -> float:
    started = time.perf_counter_ns()
    func()
    return (time.perf_counter_ns() - started) / 1_000_000.0


def benchmark(path: Path, ids: list[int], *, iterations: int, warmup: int) -> dict:
    db = Database(path)
    queries = [f"性能基准主题{index % 100:04d}" for index in range(iterations + warmup)]
    try:
        for query in queries[:warmup]:
            retrieval.search(db, query, origin_project="benchmark", scope="project",
                             kinds=("experience",), limit=5, touch=False)
        search_times = [
            _time_call(lambda q=query: retrieval.search(
                db, q, origin_project="benchmark", scope="project",
                kinds=("experience",), limit=5, touch=False))
            for query in queries[warmup:]
        ]
        get_times = [
            _time_call(lambda item_id=ids[index % len(ids)]: retrieval.get(
                db, "experience", item_id, touch=False))
            for index in range(iterations)
        ]
        recent_times = [
            _time_call(lambda: retrieval.recent(
                db, None, from_at="2000-01-01T00:00:00+00:00",
                to_at="2100-01-01T00:00:00+00:00", limit=10, touch=False))
            for _ in range(iterations)
        ]
    finally:
        db.close()
    return {
        "search": summarize(search_times),
        "get": summarize(get_times),
        "recent": summarize(recent_times),
    }


def gate(result: dict, thresholds: dict, *, baseline: dict | None = None,
         max_regression: float = 0.20) -> list[str]:
    failures: list[str] = []
    for operation in ("search", "get", "recent"):
        actual = float(result[operation]["p95_ms"])
        limit = float(thresholds[f"{operation}_p95_ms"])
        if actual > limit:
            failures.append(f"{operation}_p95_ms={actual:.4f} > {limit:.4f}")
        if baseline and operation in baseline:
            old = float(baseline[operation]["p95_ms"])
            if old > 0 and actual > old * (1 + max_regression):
                failures.append(
                    f"{operation} P95 相对基线退化 {(actual / old - 1) * 100:.1f}%")
    return failures


def render_markdown(payload: dict) -> str:
    lines = ["# 检索性能基准", "",
             f"规模：{payload['size']}；迭代：{payload['iterations']}；"
             f"预热：{payload['warmup']}", "",
             "| 操作 | P50 ms | P95 ms | P99 ms | 最大 ms |",
             "|---|---:|---:|---:|---:|"]
    for name, values in payload["result"].items():
        lines.append(f"| {name} | {values['p50_ms']} | {values['p95_ms']} | "
                     f"{values['p99_ms']} | {values['max_ms']} |")
    lines.extend(["", "## 门禁", "", "通过" if not payload["failures"] else "失败："])
    lines.extend(f"- {item}" for item in payload["failures"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长期记忆 SQLite 热态性能基准")
    parser.add_argument("--size", type=int, default=1000)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--profile", default="smoke_1000")
    parser.add_argument("--thresholds", type=Path, default=PERF_DIR / "thresholds.json")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-regression", type=float, default=0.20)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    args = parser.parse_args(argv)
    if args.size < 1 or args.iterations < 1 or args.warmup < 0:
        parser.error("size/iterations 必须大于 0，warmup 不得小于 0")

    all_thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
    if args.profile not in all_thresholds:
        parser.error(f"未知性能门槛 profile: {args.profile}")
    baseline = None
    if args.baseline:
        baseline_payload = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline = baseline_payload.get("result", baseline_payload)

    tmp_dir = Path(tempfile.mkdtemp(prefix="dsh-perf-"))
    try:
        db_path = tmp_dir / "memory.db"
        build_started = time.perf_counter()
        ids = build_database(db_path, args.size)
        build_seconds = time.perf_counter() - build_started
        result = benchmark(db_path, ids, iterations=args.iterations, warmup=args.warmup)
        failures = gate(result, all_thresholds[args.profile], baseline=baseline,
                        max_regression=args.max_regression)
        payload = {
            "size": args.size, "iterations": args.iterations, "warmup": args.warmup,
            "profile": args.profile, "build_seconds": round(build_seconds, 4),
            "result": result, "failures": failures,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.output_json:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        if args.output_md:
            args.output_md.parent.mkdir(parents=True, exist_ok=True)
            args.output_md.write_text(render_markdown(payload), encoding="utf-8")
        return 1 if failures else 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

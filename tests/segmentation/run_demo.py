# -*- coding: utf-8 -*-
"""切分实验入口。

离线（默认）：用 FakeTransport 的启发式分数，验证流程与不变量。
在线：加 --live，走 tests/.env 里的 StartLux 配置。

用法：
    python tests/segmentation/run_demo.py                     # 离线跑样例
    python tests/segmentation/run_demo.py --live              # 打真实模型
    python tests/segmentation/run_demo.py --live --think      # 打开 G2（AutoThink）
    python tests/segmentation/run_demo.py --json              # 输出完整结构
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from segmentation.env import load_api_config
from segmentation.direct import HttpTransport
from jev import FakeTransport, JudgeError
from segmenter import SegmentConfig, Segmenter


def load_conversations(path: Path) -> list:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def boundary_scores(predicted: list, expected: list) -> dict:
    """按 seq 严格相等的边界 P/R/F1。"""
    predicted, expected = set(predicted), set(expected)
    hit = len(predicted & expected)
    precision = hit / len(predicted) if predicted else (1.0 if not expected else 0.0)
    recall = hit / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"tp": hit, "precision": round(precision, 3),
            "recall": round(recall, 3), "f1": round(f1, 3)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(
        Path(__file__).resolve().parents[1] / "fixtures" / "segmentation_sample.jsonl"))
    parser.add_argument("--live", action="store_true", help="调用真实 StartLux")
    parser.add_argument("--think", action="store_true", help="允许 G2（AutoThink）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", action="store_true", help="打印完整结构")
    args = parser.parse_args()

    conversations = load_conversations(Path(args.input))
    if args.limit:
        conversations = conversations[:args.limit]

    if args.live:
        config = load_api_config(model=args.model)
        transport = HttpTransport(config)
        print(f"[live] {config.decisions_url} model={config.model} key={config.masked_key}")
    else:
        transport = FakeTransport()

    segmenter = Segmenter(
        transport=transport,
        config=SegmentConfig(use_autothink=args.think if args.live else True),
    )

    totals = {"events": 0, "tp": 0, "pred": 0, "exp": 0, "review": 0, "fallback": 0}
    for row in conversations:
        try:
            result = segmenter.run(row["messages"])
        except JudgeError as exc:
            print(f"\n[{row.get('id')}] 判定失败：{exc}")
            continue
        except AssertionError as exc:
            print(f"\n[{row.get('id')}] 不变量被破坏：{exc}")
            continue

        predicted = result.boundaries()
        expected = list(row.get("expected_boundaries") or [])
        scores = boundary_scores(predicted, expected)

        totals["events"] += len(result.events)
        totals["tp"] += scores["tp"]
        totals["pred"] += len(predicted)
        totals["exp"] += len(expected)
        totals["review"] += sum(1 for d in result.decisions if d.needs_review)
        totals["fallback"] += sum(1 for d in result.decisions if d.source == "fallback")

        print(f"\n=== {row.get('id')} ===")
        print(f"输入 {len(row['messages'])} 条；切出 {len(result.events)} 个事件；"
              f"边界 预测={predicted} 期望={expected}  P={scores['precision']} "
              f"R={scores['recall']} F1={scores['f1']}")
        for event in result.events:
            print(f"  [#{event.order}] seq {event.start_seq}-{event.end_seq} "
                  f"({len(event.message_ids)} 条) {event.title}")
        stages = {}
        for decision in result.decisions:
            stages[decision.source] = stages.get(decision.source, 0) + 1
        print(f"  判定来源：{stages}；需复核 {sum(1 for d in result.decisions if d.needs_review)}")

        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

    if totals["exp"]:
        precision = totals["tp"] / totals["pred"] if totals["pred"] else 0.0
        recall = totals["tp"] / totals["exp"] if totals["exp"] else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
        print(f"\n[汇总] 对话={len(conversations)} 事件={totals['events']} "
              f"P={precision:.3f} R={recall:.3f} F1={f1:.3f} "
              f"需复核={totals['review']} 降级={totals['fallback']}")
    else:
        print(f"\n[汇总] 对话={len(conversations)} 事件={totals['events']}（样例无期望边界）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

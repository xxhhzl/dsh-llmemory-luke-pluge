# -*- coding: utf-8 -*-
from __future__ import annotations

import math
import re


def item_key(item: dict) -> str:
    return f"{item['kind']}:{int(item['id'])}"


def _normal(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").casefold())


def evaluate(records: list[dict], *, k: int = 5) -> dict:
    answerable = [record for record in records if not record["case"].get("should_abstain")]
    abstain = [record for record in records if record["case"].get("should_abstain")]
    recalls: list[float] = []
    precisions: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    forbidden_hits = 0
    duplicate_extra = 0
    returned_total = 0

    for record in records:
        case = record["case"]
        results = record["results"][:k]
        keys = [item_key(item) for item in results]
        forbidden = set(case.get("forbidden", []))
        forbidden_hits += sum(key in forbidden for key in keys)
        seen_text: set[tuple[str, str]] = set()
        for item in results:
            signature = (item["kind"], _normal(item.get("text", "")))
            if signature in seen_text:
                duplicate_extra += 1
            else:
                seen_text.add(signature)
        returned_total += len(results)

        if case.get("should_abstain"):
            continue
        expected = set(case.get("expected", []))
        relevant = [1 if key in expected else 0 for key in keys]
        hits = sum(relevant)
        recalls.append(hits / len(expected) if expected else 1.0)
        # 标准 Precision@k：不足 k 条的空位按不相关处理，便于不同版本横向比较。
        precisions.append(hits / k)
        first = next((index for index, value in enumerate(relevant, 1) if value), None)
        reciprocal_ranks.append(1.0 / first if first else 0.0)
        dcg = sum(value / math.log2(index + 1) for index, value in enumerate(relevant, 1))
        ideal_len = min(len(expected), k)
        idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_len + 1))
        ndcgs.append(dcg / idcg if idcg else 1.0)

    def average(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    false_positive = sum(bool(record["results"][:k]) for record in abstain)
    return {
        "cases": len(records),
        "answerable_cases": len(answerable),
        "abstain_cases": len(abstain),
        "recall_at_5": round(average(recalls), 6),
        "precision_at_5": round(average(precisions), 6),
        "mrr_at_5": round(average(reciprocal_ranks), 6),
        "ndcg_at_5": round(average(ndcgs), 6),
        "no_answer_false_positive_rate": round(
            false_positive / len(abstain) if abstain else 0.0, 6),
        "forbidden_leakage_rate": round(
            forbidden_hits / returned_total if returned_total else 0.0, 6),
        "duplicate_rate": round(
            duplicate_extra / returned_total if returned_total else 0.0, 6),
    }


def check_thresholds(metrics: dict, thresholds: dict) -> list[str]:
    failures: list[str] = []
    for name, rule in thresholds.items():
        actual = float(metrics.get(name, 0.0))
        if "min" in rule and actual < float(rule["min"]):
            failures.append(f"{name}={actual:.4f} < {float(rule['min']):.4f}")
        if "max" in rule and actual > float(rule["max"]):
            failures.append(f"{name}={actual:.4f} > {float(rule['max']):.4f}")
    return failures

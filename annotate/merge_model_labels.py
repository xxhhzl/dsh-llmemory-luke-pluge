# -*- coding: utf-8 -*-
"""合并三个模型的标注结果：多数投票 + 一致性统计 + 与人工标注对比。

用法：
    python annotate/merge_model_labels.py --candidates annotate/data/<SESSION_ID>_candidates.jsonl
    python annotate/merge_model_labels.py --models deepseek-v41-flash glm-53 kimi-k3-1
"""
from __future__ import annotations

import argparse
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "annotate" / "data"
DEFAULT_MODELS = {
    "deepseek-v41-flash": "deepseek-v4.1-flash",
    "glm-53": "glm-5.3",
    "kimi-k3-1": "kimi-k3-1",
}


def load_labels(path: pathlib.Path) -> dict:
    labels = {}
    if not path.exists():
        return labels
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        sample_id = str(item.get("sample_id", "")).strip()
        label = str(item.get("label", "")).upper()
        if sample_id and label in ("B", "N", "U"):
            labels[sample_id] = {
                "label": label,
                "reason": item.get("reason", ""),
                "confidence": item.get("confidence"),
            }
    return labels


def kappa(pairs) -> float:
    pairs = [(a, b) for a, b in pairs if a in ("B", "N") and b in ("B", "N")]
    if not pairs:
        return 0.0
    total = len(pairs)
    agree = sum(1 for a, b in pairs if a == b) / total
    expected = 0.0
    for label in ("B", "N"):
        pa = sum(1 for a, _ in pairs if a == label) / total
        pb = sum(1 for _, b in pairs if b == label) / total
        expected += pa * pb
    return round((agree - expected) / (1 - expected), 3) if expected < 1 else 1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=pathlib.Path, required=True,
                        help="由 annotate.py prepare 生成的候选 JSONL")
    parser.add_argument("--models", nargs="*", default=list(DEFAULT_MODELS))
    parser.add_argument("--out", default=str(DATA / "model_labels.jsonl"))
    args = parser.parse_args()

    candidates = {}
    for line in args.candidates.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            candidates[str(item["sample_id"])] = item

    model_labels = {}
    for slug in args.models:
        path = DATA / f"labels_{slug}.jsonl"
        labels = load_labels(path)
        model_labels[slug] = labels
        distribution = {label: sum(1 for v in labels.values() if v["label"] == label)
                        for label in ("B", "N", "U")}
        print(f"{DEFAULT_MODELS.get(slug, slug):28s} 条数={len(labels):3d}  {distribution}  ({path.name})")

    sample_ids = sorted(candidates)
    merged = []
    for sample_id in sample_ids:
        votes = {slug: model_labels[slug].get(sample_id) for slug in args.models}
        labels = {slug: (vote["label"] if vote else None) for slug, vote in votes.items()}
        valid = [label for label in labels.values() if label in ("B", "N")]
        b_votes = sum(1 for label in valid if label == "B")
        n_votes = sum(1 for label in valid if label == "N")
        if b_votes >= 2 and b_votes > n_votes:
            final, review = "B", False
        elif n_votes >= 2 and n_votes > b_votes:
            final, review = "N", False
        else:
            final, review = "U", True

        confidences = [vote["confidence"] for vote in votes.values()
                       if vote and isinstance(vote.get("confidence"), (int, float))]
        merged.append({
            "sample_id": sample_id,
            "labels": labels,
            "label_model": final,
            "votes": {"B": b_votes, "N": n_votes, "U": len(valid) - b_votes - n_votes},
            "confidence": round(sum(confidences) / len(confidences), 3) if confidences else None,
            "needs_review": review,
            "human": candidates[sample_id].get("label_a") or "",
        })

    pathlib.Path(args.out).write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in merged) + "\n",
        encoding="utf-8")

    distribution = {label: sum(1 for item in merged if item["label_model"] == label)
                    for label in ("B", "N", "U")}
    misses = len(sample_ids) - sum(1 for item in merged
                                  if all(item["labels"][slug] for slug in args.models))
    print(f"\n合并 {len(merged)} 条 → {args.out}")
    print(f"最终分布: {distribution}；needs_review={sum(1 for i in merged if i['needs_review'])}；缺票={misses}")

    slugs = args.models
    print("\n模型两两一致性（B/N，Cohen's kappa）：")
    for i in range(len(slugs)):
        for j in range(i + 1, len(slugs)):
            pairs = [(model_labels[slugs[i]].get(s, {}).get("label"),
                      model_labels[slugs[j]].get(s, {}).get("label")) for s in sample_ids]
            same = sum(1 for a, b in pairs if a and b and a == b)
            total = sum(1 for a, b in pairs if a and b)
            print(f"  {DEFAULT_MODELS.get(slugs[i], slugs[i])} vs {DEFAULT_MODELS.get(slugs[j], slugs[j])}: "
                  f"一致 {same}/{total}（{same / total:.1%}） kappa={kappa(pairs)}")

    compared = [item for item in merged if item["human"] in ("B", "N")
                and item["label_model"] in ("B", "N")]
    if compared:
        correct = sum(1 for item in compared if item["human"] == item["label_model"])
        print(f"\n与人工标注可比 {len(compared)} 条，一致率 {correct / len(compared):.1%}")
        for item in compared:
            if item["human"] != item["label_model"]:
                print(f"  分歧 sample={item['sample_id']} 人工={item['human']} "
                      f"模型={item['label_model']} votes={item['labels']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

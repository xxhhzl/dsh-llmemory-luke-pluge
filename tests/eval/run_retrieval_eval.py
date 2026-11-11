# -*- coding: utf-8 -*-
"""在固定数据库上运行召回质量评测；--sample 使用隔离样例库。"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import retrieval  # noqa: E402
from database import Cognition, Database, Experience  # noqa: E402
from tests.eval.metrics import check_thresholds, evaluate  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent


def load_jsonl(path: Path) -> list[dict]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
        if not case.get("case_id") or not isinstance(case.get("query"), str):
            raise ValueError(f"{path}:{line_number}: 缺少 case_id/query")
        if not case.get("should_abstain") and not case.get("expected"):
            raise ValueError(f"{path}:{line_number}: 可回答用例必须提供 expected")
        cases.append(case)
    return cases


def build_sample_db(path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db = Database(path)
    try:
        items = [
            Cognition(subject="用户", content="用户偏好中文回复", kind="preference",
                      confidence=0.95, importance=5, source_event_id=0,
                      cues=("中文回复",), created_at=now),
            Cognition(subject="dsh-llmemory-luke", content="项目使用 SQLite 数据库", kind="fact",
                      confidence=0.95, importance=5, source_event_id=0,
                      cues=("SQLite",), created_at=now),
            Cognition(subject="历史方案", content="MySQL 旧数据库已经停用", kind="fact",
                      confidence=0.9, importance=3, source_event_id=0,
                      cues=("MySQL",), created_at=now),
        ]
        for item in items:
            item_id = db.insert_cognition(item)
            db.index_fts("cognition", item_id, item.subject, item.content)
        exp = Experience(context="更换向量模型后", content="索引重建后再执行检索",
                         kind="technique", confidence=0.9, importance=4,
                         source_event_id=0, cues=("索引重建",), created_at=now)
        exp_id = db.insert_experience(exp)
        db.index_fts("experience", exp_id, exp.context or "", exp.content)
        db.invalidate_cognition(3, "样例中的过期事实")
    finally:
        db.close()


def run(db_path: Path, cases: list[dict], *, k: int = 5) -> tuple[list[dict], dict]:
    db = Database(db_path)
    records = []
    try:
        for case in cases:
            hits = retrieval.search(
                db, case["query"], limit=k, touch=False,
                scope=case.get("scope", "all"),
                kinds=tuple(case.get("kinds", ("event", "experience", "cognition", "insight"))),
                query_vector=case.get("query_vector"))
            records.append({"case": case, "results": [item.to_dict() for item in hits]})
    finally:
        db.close()
    return records, evaluate(records, k=k)


def render_markdown(metrics: dict, failures: list[str]) -> str:
    lines = ["# 召回质量评测", "", "| 指标 | 结果 |", "|---|---:|"]
    for name, value in metrics.items():
        lines.append(f"| `{name}` | {value} |")
    lines.extend(["", "## 门禁", "", "通过" if not failures else "失败："])
    lines.extend(f"- {item}" for item in failures)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长期记忆召回质量评测")
    parser.add_argument("--db", type=Path, help="只读评测数据库路径")
    parser.add_argument("--cases", type=Path, default=EVAL_DIR / "cases.sample.jsonl")
    parser.add_argument("--thresholds", type=Path, default=EVAL_DIR / "thresholds.json")
    parser.add_argument("--sample", action="store_true", help="生成隔离样例库")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)
    if not args.sample and args.db is None:
        parser.error("必须提供 --db 或 --sample")

    tmp_dir: Path | None = None
    db_path = args.db
    if args.sample:
        tmp_dir = Path(tempfile.mkdtemp(prefix="dsh-recall-eval-"))
        db_path = tmp_dir / "memory.db"
        build_sample_db(db_path)
    try:
        cases = load_jsonl(args.cases)
        records, metrics = run(db_path, cases, k=max(1, args.k))
        thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
        failures = check_thresholds(metrics, thresholds)
        report = {"metrics": metrics, "failures": failures, "records": records}
        print(json.dumps({"metrics": metrics, "failures": failures},
                         ensure_ascii=False, indent=2))
        if args.output_json:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        if args.output_md:
            args.output_md.parent.mkdir(parents=True, exist_ok=True)
            args.output_md.write_text(render_markdown(metrics, failures), encoding="utf-8")
        return 1 if failures else 0
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

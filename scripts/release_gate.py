# -*- coding: utf-8 -*-
"""执行本仓库可自动化的发布门禁；真实 DSH 验收仍需人工执行。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def commands(*, quick: bool, output_dir: Path) -> list[tuple[str, list[str]]]:
    python = sys.executable
    js_tests = [str(path.relative_to(ROOT)) for path in sorted(
        (ROOT / "tests" / "js").glob("*.test.mjs"))]
    result = [
        ("环境预检", [python, "scripts/preflight.py"]),
        ("公开快照", [python, "scripts/check_public_snapshot.py"]),
        ("项目一致性", [python, "scripts/check_project_consistency.py"]),
    ]
    if quick:
        result.append(("治理模块单测", [python, "-m", "unittest",
                      "tests.test_project_governance", "tests.test_eval_metrics",
                      "tests.test_perf_gate", "-q"]))
    else:
        result.append(("Python 全量测试", [python, "-m", "unittest", "discover",
                      "-s", "tests", "-p", "test_*.py", "-q"]))
    result.extend([
        ("JavaScript 全量测试", ["node", "--test", *js_tests]),
        ("召回质量样例", [python, "tests/eval/run_retrieval_eval.py", "--sample",
                        "--output-json", str(output_dir / "quality.json"),
                        "--output-md", str(output_dir / "quality.md")]),
        ("检索性能烟测", [python, "tests/perf/benchmark_retrieval.py",
                        "--size", "1000", "--iterations", "50", "--warmup", "10",
                        "--profile", "smoke_1000",
                        "--output-json", str(output_dir / "performance.json"),
                        "--output-md", str(output_dir / "performance.md")]),
        ("Python 编译检查", [python, "-m", "compileall", "-q", "src", "tests", "scripts"]),
        ("发布产物检查", [python, "scripts/build_release.py", "--check"]),
    ])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh-llmemory-luke 自动发布门禁")
    parser.add_argument("--quick", action="store_true", help="只跑治理模块单测")
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / ".tmp" / "release-gate")
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    process_tmp = args.output_dir / "tmp"
    process_tmp.mkdir(parents=True, exist_ok=True)
    child_env = os.environ.copy()
    # 某些受限 Windows 环境禁止 Node 在系统 Temp 下 mkdtemp；项目临时目录可写且已忽略。
    child_env["TEMP"] = str(process_tmp.resolve())
    child_env["TMP"] = str(process_tmp.resolve())
    records = []
    for name, command in commands(quick=args.quick, output_dir=args.output_dir):
        print(f"\n=== {name} ===", flush=True)
        started = time.perf_counter()
        completed = subprocess.run(command, cwd=ROOT, env=child_env)
        elapsed = time.perf_counter() - started
        records.append({"name": name, "command": command,
                        "exit_code": completed.returncode,
                        "seconds": round(elapsed, 3)})
        if completed.returncode:
            print(f"门禁失败：{name}（退出码 {completed.returncode}）")
            break
    summary = {
        "status": "PASS" if records and all(item["exit_code"] == 0 for item in records)
                  and len(records) == len(commands(quick=args.quick,
                                                   output_dir=args.output_dir)) else "FAIL",
        "manual_required": "按 docs/real_dsh_acceptance.md 执行真实 DSH 验收",
        "steps": records,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

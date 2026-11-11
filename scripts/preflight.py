# -*- coding: utf-8 -*-
"""检查 dsh-llmemory-luke 的本地运行环境，不修改任何配置。"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _package(module: str, distribution: str, *, optional: bool) -> Check:
    try:
        importlib.import_module(module)
        version = importlib.metadata.version(distribution)
        return Check(distribution, "PASS", version)
    except Exception as exc:  # 预检必须把二进制加载失败也报告出来
        status = "DEGRADED" if optional else "FAIL"
        return Check(distribution, status, f"{type(exc).__name__}: {exc}")


def _sqlite() -> list[Check]:
    checks = [Check("sqlite", "PASS", sqlite3.sqlite_version)]
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(content)")
        checks.append(Check("sqlite.fts5", "PASS", "available"))
    except sqlite3.Error as exc:
        checks.append(Check("sqlite.fts5", "FAIL", str(exc)))
    try:
        conn.execute("CREATE VIRTUAL TABLE trigram_probe USING fts5(content, tokenize='trigram')")
        checks.append(Check("sqlite.fts5_trigram", "PASS", "available"))
    except sqlite3.Error as exc:
        checks.append(Check("sqlite.fts5_trigram", "FAIL", str(exc)))
    finally:
        conn.close()
    return checks


def _node() -> Check:
    try:
        result = subprocess.run(["node", "--version"], check=True,
                                capture_output=True, text=True, timeout=10)
        version = result.stdout.strip()
        parts = tuple(int(item) for item in version.lstrip("v").split(".")[:2])
        return Check("node", "PASS" if (24, 9) <= parts < (25, 0) else "FAIL", version)
    except Exception as exc:
        return Check("node", "FAIL", f"{type(exc).__name__}: {exc}")


def run_checks() -> list[Check]:
    checks: list[Check] = []
    version = ".".join(str(item) for item in sys.version_info[:3])
    py_ok = sys.version_info[:2] == (3, 12)
    checks.append(Check("python", "PASS" if py_ok else "FAIL", version))
    checks.append(Check("project.version", "PASS" if (ROOT / "VERSION").exists() else "FAIL",
                        (ROOT / "VERSION").read_text(encoding="utf-8").strip()
                        if (ROOT / "VERSION").exists() else "missing VERSION"))
    checks.append(_package("pydantic", "pydantic", optional=False))
    checks.extend(_sqlite())
    checks.append(_package("sqlite_vec", "sqlite-vec", optional=True))
    checks.append(_package("numpy", "numpy", optional=True))
    checks.append(_package("usearch", "usearch", optional=True))
    checks.append(_node())
    return checks


def overall(checks: list[Check]) -> str:
    if any(item.status == "FAIL" for item in checks):
        return "FAIL"
    if any(item.status == "DEGRADED" for item in checks):
        return "DEGRADED"
    return "PASS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dsh-llmemory-luke 环境预检")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    checks = run_checks()
    state = overall(checks)
    if args.json:
        print(json.dumps({"status": state, "checks": [asdict(item) for item in checks]},
                         ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"{item.status:8} {item.name}: {item.detail}")
        print(f"OVERALL  {state}")
    return 1 if state == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())

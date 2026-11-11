# -*- coding: utf-8 -*-
"""检查当前 Git 索引是否适合作为公开仓库快照。"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = (
    "DEEPSEEK.md",
    "notes.md",
    "annotate/data/",
    "tests/output/",
    "test/docs/",
)
FORBIDDEN_TEXT = (
    "5375" + "a655",
    r"C:\Users" + r"\31176",
)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True,
        capture_output=True, text=True, encoding="utf-8")
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines()
            if line.strip()]


def collect_errors(files: list[str] | None = None) -> list[str]:
    files = files if files is not None else tracked_files()
    errors: list[str] = []
    for relative in files:
        if any(relative == prefix.rstrip("/") or relative.startswith(prefix)
               for prefix in FORBIDDEN_PATHS):
            errors.append(f"禁止公开的路径仍被跟踪: {relative}")
            continue
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for token in FORBIDDEN_TEXT:
            if token in content:
                errors.append(f"{relative} 包含本机标识: {token}")
    return errors


def main() -> int:
    errors = collect_errors()
    if errors:
        print("公开快照检查失败：")
        for item in errors:
            print(f"- {item}")
        return 1
    print("公开快照检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

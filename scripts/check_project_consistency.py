# -*- coding: utf-8 -*-
"""校验版本、工具清单、文档标记和发布产物的一致性。"""
from __future__ import annotations

import ast
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PLUGIN_ID = "dsh-llmemory-luke"
PACKAGE_NAME = "@local/dsh-llmemory-luke"
CURRENT_DOCS = (
    ROOT / "README.md",
    ROOT / "release" / "README.md",
    ROOT / "docs" / "项目全景.md",
)


def _match(path: Path, pattern: str, label: str) -> str:
    found = re.search(pattern, path.read_text(encoding="utf-8"))
    if found is None:
        raise ValueError(f"{path.relative_to(ROOT)} 缺少 {label}")
    return found.group(1)


def registered_tools(path: Path | None = None) -> list[str]:
    tree = ast.parse((path or SRC / "tools.py").read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
               and item.func.id == "tool" for item in node.decorator_list):
            names.append(node.name)
    return names


def collect_errors() -> list[str]:
    errors: list[str] = []
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        errors.append(f"VERSION 格式非法: {version!r}")

    values = {
        "src/config.py": _match(SRC / "config.py", r'PLUGIN_VERSION = "([^"]+)"',
                                "PLUGIN_VERSION"),
        "src/js/frames.js": _match(SRC / "js" / "frames.js",
                                   r"PLUGIN_VERSION = '([^']+)'", "PLUGIN_VERSION"),
        "release/package.json": json.loads(
            (ROOT / "release" / "package.json").read_text(encoding="utf-8"))["version"],
        "pyproject.toml": tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"],
    }
    for label, actual in values.items():
        if actual != version:
            errors.append(f"{label} 版本 {actual} ≠ VERSION {version}")

    package = json.loads((ROOT / "release" / "package.json").read_text(encoding="utf-8"))
    if package.get("name") != PACKAGE_NAME:
        errors.append(f"release/package.json 名称 {package.get('name')} ≠ {PACKAGE_NAME}")
    js_name = _match(SRC / "js" / "index.js", r"export const name = '([^']+)'",
                     "插件名称")
    if js_name != PLUGIN_ID:
        errors.append(f"src/js/index.js 名称 {js_name} ≠ {PLUGIN_ID}")
    patch_text = (ROOT / "release" / "cordis.patch.yml").read_text(encoding="utf-8")
    if f"id: {PLUGIN_ID}" not in patch_text or f"name: '{PACKAGE_NAME}'" not in patch_text:
        errors.append("release/cordis.patch.yml 插件 ID 或包名未同步")
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if not root_readme.startswith(f"# {PLUGIN_ID}\n"):
        errors.append(f"README.md 标题不是 {PLUGIN_ID}")

    tools = registered_tools()
    if len(tools) != len(set(tools)):
        errors.append("src/tools.py 存在重复工具名")
    readme = (ROOT / "release" / "README.md").read_text(encoding="utf-8")
    missing = [name for name in tools if f"`{name}`" not in readme]
    if missing:
        errors.append("release/README.md 缺少工具: " + ", ".join(missing))
    expected_count = f"{len(tools)} 个工具"
    if expected_count not in readme:
        errors.append(f"release/README.md 缺少当前工具数量标记“{expected_count}”")

    marker = f"<!-- CURRENT_VERSION: {version} -->"
    for path in CURRENT_DOCS:
        if marker not in path.read_text(encoding="utf-8"):
            errors.append(f"{path.relative_to(ROOT)} 缺少版本标记 {marker}")

    # 复用构建脚本的逐字节检查，避免复制另一套文件清单。
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_release  # noqa: PLC0415
    errors.extend(build_release.check())
    return errors


def main() -> int:
    try:
        errors = collect_errors()
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        errors = [str(exc)]
    if errors:
        print("项目一致性检查失败：")
        for item in errors:
            print(f"- {item}")
        return 1
    print("项目一致性检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

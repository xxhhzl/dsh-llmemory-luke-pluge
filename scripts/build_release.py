# -*- coding: utf-8 -*-
"""把源码装配成可安装的 Harness 插件包（release/）。

约定（见 Harness 内置 skill `cordis-plugin-development/references/host-plugin.md`）：
- 包根有 package.json，声明 `dsh.bundle.patch` 指向 cordis.patch.yml；
- cordis.patch.yml 用 `insert` 插入插件行；
- 运行时文件放 lib/ 与 kernel/，`files` 里列出（本地目录安装不过滤，但仍要写全）；
- 展示资源：locale/*.json（meta.title / meta.description）+ 包内 icon。

用法：
    python scripts/build_release.py            # 重新生成 lib/ 与 kernel/
    python scripts/build_release.py --check    # 只校验是否与源码一致
"""
from __future__ import annotations

import argparse
import filecmp
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
JS_SRC = SRC / "js"
RELEASE = ROOT / "release"

# 宿主侧模块 -> release/lib/；client.js 是浏览器入口，必须放包根（manifest 指向 ./client.js）
JS_FILES = ("index.js", "shell.js", "lifecycle.js", "trace.js", "kernel.js",
            "frames.js", "llm-bridge.js", "tools-bridge.js", "ingest-bridge.js",
            "model-config.js", "core-prompt.js", "commands.js", "stats-route.js",
            "services.js")
CLIENT_FILE = "client.js"


def build() -> list[str]:
    """复制 JS 与 Python 源码到 release/，返回变更文件列表。"""
    changed: list[str] = []
    lib = RELEASE / "lib"
    kernel = RELEASE / "kernel"
    lib.mkdir(parents=True, exist_ok=True)
    kernel.mkdir(parents=True, exist_ok=True)

    for name in JS_FILES:
        source = JS_SRC / name
        if not source.exists():
            raise SystemExit(f"缺少源码: {source}")
        target = lib / name
        if not target.exists() or not filecmp.cmp(source, target, shallow=False):
            shutil.copyfile(source, target)
            changed.append(f"lib/{name}")

    client_source = JS_SRC / CLIENT_FILE
    client_target = RELEASE / CLIENT_FILE
    if not client_target.exists() or not filecmp.cmp(client_source, client_target,
                                                     shallow=False):
        shutil.copyfile(client_source, client_target)
        changed.append(CLIENT_FILE)

    for source in sorted(SRC.glob("*.py")):
        target = kernel / source.name
        if not target.exists() or not filecmp.cmp(source, target, shallow=False):
            shutil.copyfile(source, target)
            changed.append(f"kernel/{source.name}")
    return changed


def check() -> list[str]:
    """返回与源码不一致的文件（空表示一致）。"""
    stale: list[str] = []
    for name in JS_FILES:
        source, target = JS_SRC / name, RELEASE / "lib" / name
        if not target.exists() or not filecmp.cmp(source, target, shallow=False):
            stale.append(f"lib/{name}")
    client_source = JS_SRC / CLIENT_FILE
    client_target = RELEASE / CLIENT_FILE
    if not client_target.exists() or not filecmp.cmp(client_source, client_target,
                                                     shallow=False):
        stale.append(CLIENT_FILE)
    for source in sorted(SRC.glob("*.py")):
        target = RELEASE / "kernel" / source.name
        if not target.exists() or not filecmp.cmp(source, target, shallow=False):
            stale.append(f"kernel/{source.name}")
    extras = [p.name for p in (RELEASE / "kernel").glob("*.py")
              if not (SRC / p.name).exists()] if (RELEASE / "kernel").exists() else []
    stale.extend(f"kernel/{name}（源码已删除）" for name in extras)
    stale.extend(_version_drift())
    return stale


def _version_drift() -> list[str]:
    """package.json / frames.js 的版本必须跟上内核（升级后显示旧版本就是这里漏了）。"""
    match = re.search(r'PLUGIN_VERSION = "([^"]+)"',
                      (SRC / "config.py").read_text(encoding="utf-8"))
    if match is None:
        return ["src/config.py 缺少 PLUGIN_VERSION"]
    expected = match.group(1)
    drift: list[str] = []
    manifest = RELEASE / "package.json"
    if manifest.exists():
        declared = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        if declared != expected:
            drift.append(f'package.json 版本 {declared} ≠ 内核 {expected}')
    js_match = re.search(r"PLUGIN_VERSION = '([^']+)'",
                         (JS_SRC / "frames.js").read_text(encoding="utf-8"))
    if js_match is None or js_match.group(1) != expected:
        drift.append(f"lib/frames.js 版本 {js_match.group(1) if js_match else '缺失'}"
                     f" ≠ 内核 {expected}")
    return drift


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="装配 release/ 插件包")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    if args.check:
        stale = check()
        if stale:
            print("与源码不一致：\n  " + "\n  ".join(stale))
            return 1
        print("release/ 与 src/ 一致")
        return 0

    changed = build()
    print("已更新：" + (", ".join(changed) if changed else "无变化"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

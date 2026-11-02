# -*- coding: utf-8 -*-
"""读取 StartLux 的接入配置。

查找顺序：进程环境变量 → tests/.env → 仓库根 .env。
只读，不写、不打印密钥。ApiConfig 本体在 tests/segmentation/direct.py（研究脚手架直连用）。
"""
from __future__ import annotations

import os
from pathlib import Path

from .direct import ApiConfig

_TESTS_DIR = Path(__file__).resolve().parents[1]
_REPO_DIR = _TESTS_DIR.parent


def _parse_env_file(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_api_config(model: str | None = None) -> ApiConfig:
    """从环境变量或 .env 读取 STARTLUX_API / STARTLUX_KEY（兼容 BAMBOO_API_KEY）。"""
    file_values = {}
    for candidate in (_TESTS_DIR / ".env", _REPO_DIR / ".env"):
        file_values.update(_parse_env_file(candidate))

    base_url = os.environ.get("STARTLUX_API") or file_values.get("STARTLUX_API", "")
    key = (os.environ.get("STARTLUX_KEY") or os.environ.get("BAMBOO_API_KEY")
           or file_values.get("STARTLUX_KEY") or file_values.get("BAMBOO_API_KEY") or "")
    model = model or os.environ.get("STARTLUX_MODEL") or file_values.get(
        "STARTLUX_MODEL") or "startlux-decision-27b-autothink"

    if not base_url:
        raise RuntimeError("缺少 STARTLUX_API（环境变量或 .env）")
    if not key:
        raise RuntimeError("缺少 STARTLUX_KEY 或 BAMBOO_API_KEY（环境变量或 .env）")
    return ApiConfig(base_url=base_url.rstrip("/"), key=key, model=model)

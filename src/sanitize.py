# -*- coding: utf-8 -*-
"""入库前脱敏：保留字段名/分隔符，遮盖常见凭据值。"""
from __future__ import annotations

import re

MASK = "[REDACTED]"
_VALUE = r"(?:'[^']*'|\"[^\"]*\"|\S{4,})"
_KEY = (r"(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|credential|authorization|auth|bearer)")

# 各正则只捕获完整前缀；JSON 值另行补引号，避免破坏结构。
_PATTERNS = (
    (re.compile(r"(\$env:[A-Za-z_][A-Za-z0-9_]*\s*=\s*)" + _VALUE), False),
    (re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-]{8,})"), False),
    (re.compile(r"(?i)(Authorization\s*:\s*)(?:Basic\s+\S+|Bearer\s+\S+|\S{4,})"), False),
    (re.compile(r'(?i)("' + _KEY + r'"\s*:\s*)("(?:\\.|[^"\\])*"|[^,}\]\s]+)'), True),
    (re.compile(r"(?i)(\b(?!authorization\b)" + _KEY + r"\b\s*[:=]\s*)" + _VALUE), False),
    (re.compile(r"(?i)(--(?:password|passwd|token|secret|api-key)\s+)(\S{4,})"), False),
)
_BARE = re.compile(
    r"\b(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,})")


def redact(text: str) -> str:
    """命中即替换成 [REDACTED]；无命中原样返回。"""
    if not text:
        return text
    out = str(text)
    for pattern, quoted in _PATTERNS:
        replacement = '"' + MASK + '"' if quoted else MASK
        out = pattern.sub(lambda match: match.group(1) + replacement, out)
    return _BARE.sub(MASK, out)

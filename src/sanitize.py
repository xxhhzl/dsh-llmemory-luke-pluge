# -*- coding: utf-8 -*-
"""入库前脱敏：凭据不进记忆库（只盖值，保留键名与句子结构）。"""
from __future__ import annotations

import re

MASK = "[REDACTED]"
_VALUE = r"('[^']*'|\"[^\"]*\"|\S{4,})"
_KEY = (r"(pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|credential|authorization|auth|bearer)")

_PATTERNS = (
    re.compile(r"(\$env:[A-Za-z_][A-Za-z0-9_]*\s*=\s*)" + _VALUE),
    re.compile(r"(?i)(Bearer\s+)([A-Za-z0-9._\-]{8,})"),
    re.compile(r"(?i)\b(" + _KEY + r")\b\s*[:=]\s*" + _VALUE),
    re.compile(r"(?i)(--(?:password|passwd|token|secret|api-key)\s+)(\S{4,})"),
)
_BARE = re.compile(
    r"\b(sk-[A-Za-z0-9]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,})")


def redact(text: str) -> str:
    """命中即替换成 [REDACTED]；无命中原样返回。"""
    if not text:
        return text
    out = str(text)
    for pattern in _PATTERNS:
        out = pattern.sub(lambda match: match.group(1) + MASK, out)
    return _BARE.sub(MASK, out)

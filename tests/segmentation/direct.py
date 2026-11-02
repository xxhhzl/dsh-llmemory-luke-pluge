# -*- coding: utf-8 -*-
"""研究脚手架用的直连传输：直接打 StartLux `/v1/decisions`。

产品侧已改为外壳代理（`src/llm.py` + `src/jev.py`），本文件只服务于
`tests/segmentation/` 的离线扫描、A/B 对比与标注脚本，**不进内核**。

接口（见 Bamboo Relay 文档）：
    POST {base}/v1/decisions
    {
      "model": "startlux-decision-27b-autothink",
      "state": <文本或 JSON>,
      "questions": {"boundary": {"type": "choice", "instructions": "...", "criteria": {...}}},
      "think_mode": "never" | "auto" | "always",
      "think_budget": 1-4096,
      "none_check": false, "allow_none": false
    }
    应答：{"answers": {"boundary": {"choice": "CONTINUE|SPLIT", "confidence": 0.98,
                                   "probabilities": {...}}}, "usage": {...}}
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from jev import CRITERIA, INSTRUCTIONS, JudgeError, JudgeResult


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    key: str
    model: str = "startlux-decision-27b-autothink"

    @property
    def masked_key(self) -> str:
        return f"{self.key[:4]}…{self.key[-2:]}(len={len(self.key)})"

    @property
    def decisions_url(self) -> str:
        '''STARTLUX_API 可以填 /v1、/v1/decisions 或 /v1/systemone，统一成决策端点。'''
        url = self.base_url.rstrip("/")
        if url.endswith(("/decisions", "/systemone")):
            return url
        if url.endswith("/v1"):
            return url + "/decisions"
        return url + "/v1/decisions"


class DecisionTransport:
    """真实 StartLux：结构化决策接口（/v1/decisions），仅研究脚本使用。"""

    def __init__(self, config: ApiConfig, timeout: float = 300.0,
                 think_budget: int = 2048):
        self.config = config
        self.timeout = timeout
        self.think_budget = max(1, min(4096, int(think_budget)))

    def _payload(self, state: dict, think: bool) -> dict:
        return {
            "model": self.config.model,
            "state": state,
            "questions": {
                "boundary": {
                    "type": "choice",
                    "instructions": INSTRUCTIONS,
                    "criteria": dict(CRITERIA),
                }
            },
            "think_mode": "always" if think else "never",
            "think_budget": self.think_budget,
            "none_check": False,
            "allow_none": False,
            "return_thought": False,
        }

    def judge(self, state: dict, *, think: bool = False) -> JudgeResult:
        started = time.monotonic()
        body = json.dumps(self._payload(state, think), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.decisions_url, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.config.key}"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            detail = detail.replace(self.config.key, "<masked>")
            raise JudgeError(f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise JudgeError(f"{type(exc).__name__}: {exc}") from exc

        latency = int((time.monotonic() - started) * 1000)
        answer = (payload.get("answers") or {}).get("boundary") or {}
        decision, p_split = parse_choice_answer(answer)

        meta = {"usage": payload.get("usage") or {}}
        if isinstance(answer.get("none_probability"), (int, float)):
            meta["none_probability"] = answer["none_probability"]

        return JudgeResult(
            decision=decision,
            p_split=p_split,
            stage="g2" if think else "g1",
            model=payload.get("model") or self.config.model,
            latency_ms=latency,
            raw=json.dumps(answer, ensure_ascii=False)[:500],
            meta=meta,
        )


def parse_choice_answer(answer: dict) -> tuple:
    """把 /decisions 的 choice 应答解析成 (decision, p_split)。"""
    if not answer:
        raise JudgeError("应答缺少 answers.boundary")

    choice = answer.get("choice")
    if choice is None:
        raise JudgeError("模型选择了 None of the above（可用选项不合适）")
    choice = str(choice).upper()
    if choice not in CRITERIA:
        raise JudgeError(f"未知选项: {choice!r}")

    probabilities = answer.get("probabilities") or {}
    p_split = probabilities.get("SPLIT")
    if p_split is None:
        confidence = float(answer.get("confidence") or 0.5)
        p_split = confidence if choice == "SPLIT" else 1.0 - confidence

    try:
        p_split = float(p_split)
    except (TypeError, ValueError):
        raise JudgeError(f"无法解析概率: {p_split!r}")
    if p_split > 1.0:
        p_split = p_split / 100.0

    return choice, round(max(0.0, min(1.0, p_split)), 4)


# 兼容旧名字
HttpTransport = DecisionTransport

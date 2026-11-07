# -*- coding: utf-8 -*-
"""把项目里所有提示词导出到 prompt.md（唯一来源是 src/，避免文档与代码漂移）。

用法：
    python scripts/export_prompts.py          # 重新生成 prompt.md
    python scripts/export_prompts.py --check  # 只校验 prompt.md 是否过期
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import base                      # noqa: E402
import config                    # noqa: E402
import consolidate               # noqa: E402
import insight                   # noqa: E402
import jev                       # noqa: E402
import summary                   # noqa: E402
import tools                     # noqa: E402

OUT = ROOT / "docs" / "prompt.md"


def _fence(text: str, lang: str = "text") -> str:
    return f"```{lang}\n{text.strip(chr(10))}\n```"


def _tool_schemas() -> list:
    q = base.message_queue(5)
    tools.get_tools_schema({"request_id": "dump", "session_id": "dump",
                            "agent_id": "dump"}, q)
    return json.loads(q.recv())["data"]


def _summary_user_template() -> str:
    meta = summary.EventMeta(
        event_id=42, session_id="session-demo", message_count=3,
        first_at="2026-11-04T08:30:00+08:00",
        last_at="2026-11-04T08:35:00+08:00", tokens=1200)
    messages = [
        {"msg_id": "m1", "role": "user", "content": "把安装步骤改成 PowerShell",
         "ts": "2026-11-04T08:30:00+08:00"},
        {"msg_id": "m2", "role": "assistant", "content": "已改写并运行构建",
         "ts": "2026-11-04T08:32:00+08:00"},
        {"msg_id": "m3", "role": "tool", "content": "exit code: 0",
         "ts": "2026-11-04T08:35:00+08:00"},
    ]
    return summary.build_messages(meta, messages,
                                  previous_summary="（示例：上一次的摘要，可省略）")[1]["content"]


def _consolidate_user_template() -> str:
    items = [{"table": "cognition", "content": "项目用 pnpm",
              "subject": "项目", "kind": "fact", "importance": 3,
              "confidence": 0.8, "cues": ["pnpm"]}]
    rows = [[type("Row", (), {"id": 7, "content": "项目用 npm",
                              "subject": "项目", "context": None})()]]
    return consolidate.build_messages(items, rows)[1]["content"]


def _insight_user_template() -> str:
    rows = [
        type("Row", (), {"id": 1, "kind": "pitfall", "scope": "global",
                         "context": "edit 报 old_string not found 时",
                         "content": "先重新 read 目标文件再改"})(),
        type("Row", (), {"id": 2, "kind": "technique", "scope": "global",
                         "context": "批量改文件前",
                         "content": "逐个 read 确认当前内容"})(),
        type("Row", (), {"id": 3, "kind": "pitfall", "scope": "project",
                         "context": "改文档表格时",
                         "content": "先用 Select-String 定位实际行"})(),
    ]
    return insight.build_messages(rows)[1]["content"]


def render() -> str:
    parts = [
        "# 长期记忆插件 · 提示词清单",
        "",
        "> 由 `python scripts/export_prompts.py` 从 `src/` 自动生成，改提示词请改源码后重跑，",
        "> 不要手改本文件（`--check` 会校验是否过期）。",
        "",
        "模型看到的提示词有五处：事件归档（调用 A）、事件边界判定、合并裁决（调用 B）、",
        "洞见归纳（归纳层）、以及四个记忆工具的定义。下面按「来源 → 提示词原文」逐条列出。",
        "",
        "## 目录",
        "",
        "1. 事件归档器（`src/summary.py`）",
        "2. 归档失败重试后缀（`src/summary.py`）",
        "3. 事件边界判定器（`src/jev.py`）",
        "4. 合并裁决器（`src/consolidate.py`）",
        "5. 洞见归纳器（`src/insight.py`）",
        "6. `memory_search` 工具（`src/tools.py`）",
        "7. `memory_get` 工具（`src/tools.py`）",
        "8. `memory_recent` 工具（`src/tools.py`）",
        "9. `memory_report` 工具（`src/tools.py`）",
        "",
        "---",
        "",
        "### 提示词来源：`src/summary.py` — 事件归档器（调用 A）",
        "",
        "触发时机：一个事件闭合（或补摘要）时，把消息序列压成标题、摘要、经验、认知。",
        '输出契约：`{"title","summary","experiences","cognitions"}`，由 `parse_result` 解析。',
        "",
        _fence(summary.SUMMARY_SYSTEM_PROMPT),
        "",
        "#### 配套的 user 消息模板（由 `build_messages` 拼装，非独立提示词）",
        "",
        _fence(_summary_user_template()),
        "",
        "### 提示词来源：`src/summary.py` — 归档失败重试后缀",
        "",
        "触发时机：上一次输出无法解析成 JSON 时，追加到 system prompt 末尾重试一次。",
        "",
        _fence(summary.RETRY_SUFFIX),
        "",
        "### 提示词来源：`src/jev.py` — 事件边界判定器（切分）",
        "",
        "触发时机：每条新消息进来时判断「是否开启新事件」，是增量切分的核心判定。",
        '输出契约：`{"decision":"CONTINUE|SPLIT","confidence":0-1}`，confidence 是**所选选项**的把握'
        "（`parse_judgement` 会换算成 p_split）。",
        "",
        _fence(jev.SYSTEM_PROMPT),
        "",
        "### 提示词来源：`src/consolidate.py` — 合并裁决器（调用 B）",
        "",
        "触发时机：新条目落库前，对候选旧条目决定 new / supersede / invalidate。",
        '输出契约：`{"decisions":[{"index","action","target_id","reason"}]}`。',
        "",
        _fence(consolidate.CONSOLIDATE_SYSTEM_PROMPT),
        "",
        "#### 配套的 user 消息模板（由 `build_messages` 拼装）",
        "",
        _fence(_consolidate_user_template(), "json"),
        "",
        "### 提示词来源：`src/insight.py` — 洞见归纳器（归纳层）",
        "",
        "触发时机：离线显式调用 `insight.induce()`，把同一线索下 ≥3 条经验提炼成通用规则；",
        "不挂在 ingest 热路径上（模型调用昂贵，且要等同类经验攒够样本）。",
        '输出契约：`{"insights":[{"content","scope","confidence","cues","source_ids"}]}`，'
        "`source_ids` 必须原样来自输入经验，且至少 2 条支撑。",
        "",
        _fence(insight.INDUCTION_SYSTEM_PROMPT),
        "",
        "#### 配套的 user 消息模板（由 `build_messages` 拼装）",
        "",
        _fence(_insight_user_template(), "json"),
        "",
    ]

    titles = {"memory_search": "检索工具：模糊找，先定位再 get",
              "memory_get": "取值工具：按 id 读整条",
              "memory_recent": "时间线工具：按区间回放",
              "memory_report": "纠错工具：把条目标为待复核"}
    for schema in _tool_schemas():
        name = schema["name"]
        parts.extend([
            f"### 提示词来源：`src/tools.py` — `{name}` 工具定义（{titles.get(name, '')}）",
            "",
            "模型看到的是 `description` + 每个参数的 `description`，它们直接决定调用姿势。",
            "",
            f"**工具描述（description）**",
            "",
            _fence(schema["description"]),
            "",
            "**参数**",
            "",
        ])
        params = schema["parameters"]
        required = set(params.get("required") or [])
        for key, spec in params["properties"].items():
            flags = ["必填" if key in required else "选填"]
            if "default" in spec:
                flags.append(f"默认 {json.dumps(spec['default'], ensure_ascii=False)}")
            parts.append(f"- `{key}`（{', '.join(flags)}）：{spec.get('description', '')}")
        parts.append("")

    parts.extend([
        "---",
        "",
        "## 维护约定",
        "",
        "- 改提示词 = 改 `src/` 里的常量/描述，然后 `python scripts/export_prompts.py`；",
        "- 输出契约（JSON 键名）与 `parse_result` / `parse_decisions` / `parse_judgement` 绑定，",
        "  改键名要同步解析函数与测试；",
        "- 工具参数描述必须与实现一致（`limit` 收敛、`scope` 语义、`from_at` 时区归一）。",
        "",
    ])
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出提示词清单")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    text = render()
    if args.check:
        if not OUT.exists():
            print("prompt.md 不存在")
            return 1
        if OUT.read_text(encoding="utf-8") != text:
            print("prompt.md 与源码不一致，请重跑 export_prompts.py")
            return 1
        print("prompt.md 与源码一致")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"已写入 {OUT.relative_to(ROOT)}（{len(text)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

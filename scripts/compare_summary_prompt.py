# -*- coding: utf-8 -*-
"""真实事件的摘要提示词 A/B：不改源码、不写记忆库，只输出对比报告。

默认对比当前提示词与「第二句保留具体产出」的候选提示词。
模型调用复用 tests/shell_sim.py 的本地 OpenAI 兼容通道；密钥不打印。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\compare_summary_prompt.py
    .\\.venv\\Scripts\\python.exe scripts\\compare_summary_prompt.py --events 6,7,19 --model glm-5.3
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from segmenter import estimate_tokens                    # noqa: E402
from shell_sim import ShellSim                           # noqa: E402
from summary import (SUMMARY_SYSTEM_PROMPT, EventMeta,    # noqa: E402
                     build_messages, parse_result)

CANDIDATE_RULE = """
5. 标题 ≤24 字符、动词开头；摘要 ≤400 字符，第一句写做了什么、结果如何。
   第二句写关键产出/结论：保留回答历史追问所必需的具体名称、数字、版本、路径、URL 与错误码；
   不确定的细节不要补，正文未出现的信息仍不写。
""".strip()
OLD_RULE = "5. 标题 ≤24 字符、动词开头；摘要 ≤400 字符，第一句写做了什么、结果如何。"
assert OLD_RULE in SUMMARY_SYSTEM_PROMPT
CANDIDATE_PROMPT = SUMMARY_SYSTEM_PROMPT.replace(OLD_RULE, CANDIDATE_RULE)
SPECIFIC = re.compile(r"https?://|\b\d+(?:\.\d+)+\b|[A-Za-z]:[\\/]|"
                      r"[\w./-]+\.(?:py|md|json|db|js|svg)\b|#[0-9]+")

def load_event(db_path: Path, event_id: int) -> dict:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        event = conn.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
        if event is None:
            raise ValueError(f"事件 #{event_id} 不存在")
        rows = conn.execute(
            "SELECT m.* FROM message m JOIN memory r "
            "ON r.session_id = m.session_id AND r.message_id = m.message_id "
            "WHERE r.event_id = ? ORDER BY m.seq", (event_id,)).fetchall()
        return {"event": dict(event), "messages": [dict(row) for row in rows]}
    finally:
        conn.close()


def run_one(sim: ShellSim, meta: EventMeta, messages: list, prompt: str) -> dict:
    payload = build_messages(meta, messages)
    raw = sim.chat([{"role": "system", "content": prompt}, payload[1]])
    result = parse_result(raw)
    return {"ok": result.ok, "title": result.title, "summary": result.summary,
            "summary_chars": len(result.summary), "truncated": result.truncated,
            "experiences": list(result.experiences),
            "cognitions": list(result.cognitions),
            "needs_review": result.needs_review, "error": result.error,
            "specific_tokens": SPECIFIC.findall(result.summary)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / ".dsh-longmemory" / "harness" / "memory.db"))
    parser.add_argument("--events", default="6,7,19")
    parser.add_argument("--model", default="glm-5.3")
    parser.add_argument("--out", default=str(ROOT / ".tmp" / "summary_detail_compare" / "report.json"))
    args = parser.parse_args(argv)
    sim = ShellSim(ROOT / ".tmp" / "summary_detail_compare", model=args.model)
    if not sim.api_key:
        print("未找到本地模型凭据（WORKBUDDY_API_KEY 或 ~/.dsh/.credentials.yaml）")
        return 2
    report = {"model": args.model, "db": args.db,
              "candidate_rule": CANDIDATE_RULE, "cases": []}
    for event_id in [int(v) for v in args.events.split(",") if v.strip()]:
        item = load_event(Path(args.db), event_id)
        event, rows = item["event"], item["messages"]
        msgs = [{"msg_id": row["message_id"], "seq": row["seq"], "role": row["role"],
                 "content": row["content"], "ts": row["occur_at"]} for row in rows]
        meta = EventMeta(event_id=event_id, session_id=event["session_id"],
                         message_count=len(msgs), first_at=msgs[0]["ts"] if msgs else "",
                         last_at=msgs[-1]["ts"] if msgs else "",
                         tokens=sum(estimate_tokens(m["content"]) for m in msgs))
        case = {"event_id": event_id, "event_title": event["title"],
                "stored_summary": event["summary"], "messages": len(msgs)}
        print(f"\n{'=' * 70}\n事件 #{event_id}：{event['title']}（{len(msgs)} 条）\n{'=' * 70}")
        for tag, prompt in (("current", SUMMARY_SYSTEM_PROMPT), ("candidate", CANDIDATE_PROMPT)):
            started = time.monotonic()
            try:
                out = run_one(sim, meta, msgs, prompt)
            except Exception as exc:
                out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            out["elapsed_s"] = round(time.monotonic() - started, 2)
            case[tag] = out
            print(f"\n[{tag}] ok={out.get('ok')} {out['elapsed_s']}s")
            print(out.get("summary") or out.get("error"))
        report["cases"].append(case)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

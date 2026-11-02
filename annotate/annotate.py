# -*- coding: utf-8 -*-
"""事件边界人工标注工具。

三个子命令：

    prepare  从 DSH 会话生成标注候选（默认离线规则，--live 用真实模型）
    label    交互式逐条标注（b=边界 / n=非边界 / u=拿不准 / s=跳过 / q=保存退出）
    stats    统计进度、边界比例、双人一致性（Cohen's kappa）

示例：
    python annotate/annotate.py prepare --session <SESSION_ID>
    python annotate/annotate.py prepare --session <SESSION_ID> --live
    python annotate/annotate.py label --file annotate/data/<SESSION_ID>_candidates.jsonl --field label_a
    python annotate/annotate.py stats --file annotate/data/<SESSION_ID>_candidates.jsonl

数据都在 annotate/data/ 下，JSONL 与 CSV 各一份；CSV 可以直接用 Excel 标。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from segmentation.dsh_sessions import discover, load_messages          # noqa: E402
from segmentation.direct import DecisionTransport                       # noqa: E402
from jev import FakeTransport, truncate                                # noqa: E402
from segmenter import SegmentConfig, Segmenter                         # noqa: E402

DATA_DIR = Path(__file__).resolve().parent / "data"
FIELDS = ("sample_id", "session_id", "msg_id", "seq", "role",
          "prev_2", "prev_1", "current", "next_1", "next_2",
          "model_decision", "p_split", "source", "event_id",
          "label_a", "label_b", "reason", "note")
REASONS = {
    "1": "换任务",
    "2": "任务收尾",
    "3": "工具循环",
    "4": "长间隔",
    "5": "显式转折",
    "6": "其他",
}
LABELS = {"b": "B", "n": "N", "u": "U"}


# ---------------- 样本准备 ----------------

def _context(messages, index, radius=2):
    """取当前消息前后各 radius 条（跳过 system），返回截断后的文本。"""
    def render(message):
        if message is None:
            return ""
        return f"{message['role']}: {truncate(message['content'], 200, 200)}"

    before = []
    step = index - 1
    while step >= 0 and len(before) < radius:
        if messages[step].get("role") != "system":
            before.append(messages[step])
        step -= 1
    before.reverse()

    after = []
    step = index + 1
    while step < len(messages) and len(after) < radius:
        if messages[step].get("role") != "system":
            after.append(messages[step])
        step += 1

    return {
        "prev_2": render(before[0] if len(before) > 1 else None),
        "prev_1": render(before[-1] if before else None),
        "current": render(messages[index]),
        "next_1": render(after[0] if after else None),
        "next_2": render(after[1] if len(after) > 1 else None),
    }


def select_candidates(messages, decisions, radius=2, extra_negatives=20,
                      max_items=300, seed=7):
    """正样本 = 被判定为 SPLIT 的消息；负样本 = 其前后邻条 + 随机 CONTINUE。"""
    kept = [m for m in messages if m.get("role") != "system"]
    by_id = {m["msg_id"]: m for m in kept}
    index_of = {m["msg_id"]: i for i, m in enumerate(kept)}

    event_of = {}
    event_no = 0
    for decision in decisions:
        if decision.decision == "SPLIT":
            event_no += 1
        event_of[decision.message_id] = max(1, event_no)

    positives = [d.message_id for d in decisions if d.decision == "SPLIT"]
    positive_set = set(positives)

    neighbors = []
    for msg_id in positives:
        index = index_of.get(msg_id)
        if index is None:
            continue
        for offset in (-1, 1):
            neighbor_index = index + offset
            if 0 <= neighbor_index < len(kept):
                candidate = kept[neighbor_index]["msg_id"]
                if candidate not in positive_set and candidate not in neighbors:
                    neighbors.append(candidate)

    pool = [d for d in decisions
            if d.decision != "SPLIT" and d.message_id not in set(neighbors)]
    rng = random.Random(seed)
    random_negatives = [d.message_id for d in rng.sample(pool, min(extra_negatives, len(pool)))]

    selected = list(dict.fromkeys(positives + neighbors + random_negatives))
    selected.sort(key=lambda mid: index_of.get(mid, 10 ** 9))
    if max_items and len(selected) > max_items:
        # 保底：正样本全部保留，负样本按比例裁剪
        keep = positives[:max_items]
        remain = max_items - len(keep)
        negatives = [mid for mid in selected if mid not in positive_set]
        selected = sorted(keep + negatives[:max(0, remain)],
                          key=lambda mid: index_of.get(mid, 10 ** 9))

    decision_by_id = {d.message_id: d for d in decisions}
    items = []
    for number, msg_id in enumerate(selected, 1):
        message = by_id[msg_id]
        decision = decision_by_id[msg_id]
        item = {
            "sample_id": f"{number:04d}",
            "session_id": message.get("session_id", ""),
            "msg_id": msg_id,
            "seq": message.get("seq"),
            "role": message.get("role"),
            **(_context(messages, index_of[msg_id], radius)),
            "model_decision": decision.decision,
            "p_split": decision.p_split,
            "source": f"{decision.source}/{decision.boundary_type or decision.stage}",
            "event_id": event_of.get(msg_id, 1),
            "label_a": "",
            "label_b": "",
            "reason": "",
            "note": "",
        }
        items.append(item)
    return items


def _write_outputs(items, out_base: Path):
    out_base.parent.mkdir(parents=True, exist_ok=True)
    jsonl = out_base.with_suffix(".jsonl")
    csv_path = out_base.with_suffix(".csv")

    with jsonl.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for item in items:
            writer.writerow({key: item.get(key, "") for key in FIELDS})
    return jsonl, csv_path


def cmd_prepare(args) -> int:
    sessions = [s for s in discover() if args.session in s.session_id]
    if not sessions:
        print(f"没有匹配会话: {args.session}")
        return 1
    session = sessions[0]
    messages = load_messages(session)
    if not messages:
        print("会话没有可解析消息")
        return 1

    if args.live:
        from segmentation.env import load_api_config
        transport = DecisionTransport(load_api_config())
        print(f"[live] 使用 StartLux: {load_api_config().model}")
    else:
        transport = FakeTransport()

    result = Segmenter(transport=transport,
                       config=SegmentConfig(use_autothink=False)).run(messages)
    items = select_candidates(messages, result.decisions,
                              radius=args.radius,
                              extra_negatives=args.extra_negatives,
                              max_items=args.max_items,
                              seed=args.seed)

    out_base = Path(args.out) if args.out else (
        DATA_DIR / f"{session.session_id.split('-')[1] if '-' in session.session_id else 'session'}_candidates")
    jsonl, csv_path = _write_outputs(items, out_base)
    positives = sum(1 for item in items if item["model_decision"] == "SPLIT")
    print(f"会话: {session.project}::{session.session_id}")
    print(f"消息 {len(messages)} 条；候选 {len(items)} 条（正样本 {positives}，负样本 {len(items) - positives}）")
    print(f"JSONL: {jsonl}")
    print(f"CSV:   {csv_path}")
    return 0


# ---------------- 交互标注 ----------------

def load_items(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_items(path: Path, items):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _format_block(tag: str, value: str, current: bool = False, width: int | None = None):
    """把一条消息渲染成带缩进/竖线/空行的块，避免长文本糊在一起。"""
    if width is None:
        try:
            width = max(40, shutil.get_terminal_size((100, 24)).columns - 14)
        except OSError:
            width = 86
    prefix = ">>" if current else "  "
    text = str(value or "（无）")
    wrapped = textwrap.wrap(text, width=width, break_long_words=True,
                            break_on_hyphens=False) or [""]

    lines = [f"{prefix} {tag:8s}│ {wrapped[0]}"]
    for line in wrapped[1:]:
        lines.append(f"   {'':8s}│ {line}")
    lines.append(f"   {'':8s}│")          # 块尾分隔线
    return lines


def _show(item, index, total, field):
    _clear()
    print(f"[{index + 1}/{total}]  {item['sample_id']}  seq={item['seq']}  role={item['role']}  "
          f"模型={item['model_decision']} p={item['p_split']} 来源={item['source']} 事件={item['event_id']}")
    print("─" * 78)
    for position, key in enumerate(("prev_2", "prev_1", "current", "next_1", "next_2")):
        if position:
            print()                        # 消息之间空一行
        block = _format_block(key, item.get(key), current=(key == "current"))
        print("\n".join(block))
    print("─" * 78)
    print(f"当前标注字段: {field}   已标: {item.get(field) or '-'}")
    print("b=边界  n=非边界  u=拿不准  s=跳过  q=保存退出  ?=帮助")


def _ask_reason() -> str:
    print("原因（可回车跳过）:", "  ".join(f"{k}={v}" for k, v in REASONS.items()))
    choice = input("> ").strip()
    return REASONS.get(choice, "")


def cmd_label(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"找不到文件: {path}")
        return 1
    items = load_items(path)
    field = args.field
    todo = [i for i, item in enumerate(items) if not item.get(field)]

    print(f"文件: {path}")
    print(f"总样本 {len(items)}，未标注 {len(todo)}，字段 {field}")
    if not todo:
        print("这个字段已经全部标完。")
        return 0

    try:
        for order, index in enumerate(todo):
            item = items[index]
            _show(item, order, len(todo), field)
            while True:
                key = input("> ").strip().lower()
                if key == "?":
                    print("b=边界  n=非边界  u=拿不准  s=跳过  q=保存退出")
                    continue
                if key == "q":
                    save_items(path, items)
                    print(f"已保存到 {path}")
                    return 0
                if key in ("s", ""):
                    break
                if key in LABELS:
                    item[field] = LABELS[key]
                    if key in ("b", "u"):
                        item["reason"] = _ask_reason()
                    break
                print("无效输入，按 ? 查看帮助")
            save_items(path, items)
    except KeyboardInterrupt:
        save_items(path, items)
        print("\n已中断并保存。")
        return 0

    save_items(path, items)
    print(f"完成，已保存到 {path}")
    return 0


# ---------------- 统计 ----------------

def cohen_kappa(a: list, b: list) -> float:
    pairs = [(x, y) for x, y in zip(a, b) if x and y]
    if not pairs:
        return 0.0
    n = len(pairs)
    agree = sum(1 for x, y in pairs if x == y) / n
    labels = sorted({x for pair in pairs for x in pair})
    expected = 0.0
    for label in labels:
        pa = sum(1 for x, _ in pairs if x == label) / n
        pb = sum(1 for _, y in pairs if y == label) / n
        expected += pa * pb
    if expected >= 1.0:
        return 1.0
    return round((agree - expected) / (1 - expected), 3)


def cmd_stats(args) -> int:
    path = Path(args.file)
    items = load_items(path)
    total = len(items)
    print(f"文件: {path}  样本 {total}")

    for field in ("label_a", "label_b"):
        done = [item for item in items if item.get(field)]
        counts = {label: sum(1 for item in done if item[field] == label) for label in ("B", "N", "U")}
        print(f"{field}: 已标 {len(done)}/{total}  分布 {counts}")

    a = [item.get("label_a") for item in items]
    b = [item.get("label_b") for item in items]
    if any(a) and any(b):
        both = sum(1 for x, y in zip(a, b) if x and y)
        print(f"双人同时标注 {both} 条，Cohen's kappa = {cohen_kappa(a, b)}")

    model_correct = 0
    model_total = 0
    for item in items:
        gold = item.get("label_a") or item.get("label_b")
        if gold in ("B", "N"):
            model_total += 1
            predicted = "B" if item["model_decision"] == "SPLIT" else "N"
            model_correct += 1 if predicted == gold else 0
    if model_total:
        print(f"模型与标注一致率: {model_correct / model_total:.1%}（{model_total} 条可比）")
    return 0


# ---------------- 入口 ----------------

def main() -> int:
    parser = argparse.ArgumentParser(description="事件边界标注工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="生成标注候选")
    p_prepare.add_argument("--session", required=True, help="会话 id 片段")
    p_prepare.add_argument("--live", action="store_true", help="使用真实 StartLux 判定")
    p_prepare.add_argument("--out", default=None, help="输出文件基名（不带扩展名）")
    p_prepare.add_argument("--radius", type=int, default=2, help="前后各取几条上下文")
    p_prepare.add_argument("--extra-negatives", type=int, default=20,
                           help="额外随机负样本数")
    p_prepare.add_argument("--max-items", type=int, default=300, help="候选上限")
    p_prepare.add_argument("--seed", type=int, default=7)
    p_prepare.set_defaults(func=cmd_prepare)

    p_label = sub.add_parser("label", help="交互式标注")
    p_label.add_argument("--file", required=True)
    p_label.add_argument("--field", choices=("label_a", "label_b"), default="label_a")
    p_label.set_defaults(func=cmd_label)

    p_stats = sub.add_parser("stats", help="统计进度与一致性")
    p_stats.add_argument("--file", required=True)
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

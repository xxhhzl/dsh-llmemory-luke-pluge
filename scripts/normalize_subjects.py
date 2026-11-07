# -*- coding: utf-8 -*-
"""存量认知主体归一：把同一事物的多种写法收敛到一个规范写法。

用法：
    python scripts/normalize_subjects.py --db <memory.db>            # 只打印计划
    python scripts/normalize_subjects.py --db <memory.db> --apply    # 执行
    python scripts/normalize_subjects.py --db <memory.db> --report  # 列出全部主体与计数
    python scripts/normalize_subjects.py --db <memory.db> \
        --alias "记忆插件=dsh-longmemory-plugin" --alias "项目 dsh-longmemory-plugin=dsh-longmemory-plugin"

归一只做两件**安全**的事：
1. 精确变体：去掉空白、忽略大小写后相同的写法（「dsh-longmemory-plugin」vs「dsh-longmemory-plugin 」）；
2. 显式别名：`--alias 旧写法=规范写法`，可多次。

不做自动语义分组：真实库里「项目」「用户」「环境」这类通用主体是子串桥，
任何包含式分组都会把不相干的主体连成一片（实测 160 种写法被并成一组）。
语义变体请先用 `--report` 看清单，再人工/模型确认后以 `--alias` 指定。

规范写法：组内出现次数最多者；并列取最长（信息更完整）。
只改 `subject` 写法，不动正文、不动 FTS（subject 不参与全文索引）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

MAX_ALIAS_DEPTH = 5


def _norm(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


def parse_aliases(pairs) -> dict:
    '''["旧=新", ...] → {旧: 新}；非法项报错（避免静默丢参数）。'''
    result = {}
    for pair in pairs or ():
        if "=" not in pair:
            raise ValueError(f"--alias 需要 旧写法=规范写法，收到 {pair!r}")
        old, new = pair.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise ValueError(f"--alias 两侧都不能为空：{pair!r}")
        result[old] = new
    return result


def _resolve(value: str, mapping: dict) -> str:
    '''跟随别名链（带环保护）。'''
    seen = set()
    current = value
    for _ in range(MAX_ALIAS_DEPTH):
        if current not in mapping or current in seen:
            break
        seen.add(current)
        current = mapping[current]
    return current


def build_groups(rows, aliases: dict | None = None) -> dict:
    '''
    生成归一计划。

    :param rows: [{"subject": str, "n": int}]（n 为该写法出现次数）
    :return: {规范写法: [被归一的写法, ...]}，只包含需要改写的组
    '''
    aliases = aliases or {}
    counts = Counter()
    spellings = defaultdict(list)       # 规范化键 → 原始写法列表
    for row in rows:
        subject = str(row["subject"])
        counts[subject] += int(row["n"])
        spellings[_norm(subject)].append(subject)

    mapping = {}
    # 1) 精确变体：规范化后相同 → 选次数最多（并列取最长）的写法
    for variants in spellings.values():
        unique = sorted(set(variants), key=lambda item: (-counts[item], -len(item), item))
        canonical = unique[0]
        for variant in unique[1:]:
            mapping[variant] = canonical

    # 2) 显式别名
    mapping.update({old: new for old, new in aliases.items() if old != new})

    plan = defaultdict(list)
    for old, new in mapping.items():
        target = _resolve(new, mapping)
        if target != old:
            plan[target].append(old)
    return {canonical: sorted(set(items)) for canonical, items in plan.items()}


def fetch_subjects(db_path: Path) -> list:
    conn = sqlite3.connect(str(db_path))
    try:
        return [{"subject": row[0], "n": row[1]} for row in conn.execute(
            "SELECT subject, COUNT(*) FROM cognition WHERE status = 'active' "
            "GROUP BY subject ORDER BY COUNT(*) DESC")]
    finally:
        conn.close()


def apply_plan(db_path: Path, plan: dict) -> int:
    '''执行归一，返回被改写的行数。'''
    conn = sqlite3.connect(str(db_path))
    changed = 0
    try:
        with conn:
            for canonical, variants in plan.items():
                for variant in variants:
                    cur = conn.execute(
                        "UPDATE cognition SET subject = ? "
                        "WHERE status = 'active' AND subject = ?",
                        (canonical, variant))
                    changed += cur.rowcount
    finally:
        conn.close()
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="存量认知主体归一")
    parser.add_argument("--db", required=True, help="memory.db 路径")
    parser.add_argument("--apply", action="store_true", help="真正执行（默认只打印计划）")
    parser.add_argument("--report", action="store_true", help="列出全部主体与计数后退出")
    parser.add_argument("--alias", action="append", default=[],
                        help="显式别名：旧写法=规范写法（可多次）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return 2
    rows = fetch_subjects(db_path)

    if args.report:
        print(f"共 {len(rows)} 种主体：")
        for row in rows:
            print(f"  [{row['n']}] {row['subject']}")
        return 0

    aliases = parse_aliases(args.alias)
    plan = build_groups(rows, aliases)

    if not plan:
        print("没有可归一的写法（未给 --alias 时只合并空白/大小写变体）。")
        return 0
    total = sum(len(items) for items in plan.values())
    print(f"待归一：{len(plan)} 组、{total} 种写法\n")
    for canonical, variants in sorted(plan.items(), key=lambda kv: -len(kv[1])):
        print(f"→ {canonical}")
        for variant in variants:
            print(f"    ← {variant}")
    if not args.apply:
        print("\n（演练模式；加 --apply 执行）")
        return 0
    changed = apply_plan(db_path, plan)
    print(f"\n已改写 {changed} 行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

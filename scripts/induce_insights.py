# -*- coding: utf-8 -*-
"""离线归纳洞见：把同一线索下 ≥N 条经验提炼成通用规则（ExpeL insights 层）。

用法：
    python scripts/induce_insights.py                      # 演练（不写库）
    python scripts/induce_insights.py --apply              # 写库
    python scripts/induce_insights.py --db <path> --model deepseek-v4.1-flash

说明：
- 默认库是插件配置里的 `.dsh-longmemory/harness/memory.db`；`--db` 覆盖。
- 归纳是显式操作，不挂在 ingest 热路径：模型调用昂贵，且要等同类经验攒够样本。
- 本次会打开目标库（触发迁移，确保 insight 表存在），演练模式不写任何条目。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import insight                                    # noqa: E402
from database import Database                     # noqa: E402
from shell_sim import ShellSim                    # noqa: E402

DEFAULT_DB = ROOT / ".dsh-longmemory" / "harness" / "memory.db"


class SimTransport:
    '''把 ShellSim.chat 适配成 SummaryTransport.complete。'''

    def __init__(self, sim):
        self._sim = sim

    def complete(self, messages, *, timeout=None):
        return self._sim.chat(messages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线归纳洞见")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--min-support", type=int, default=3)
    parser.add_argument("--max-clusters", type=int, default=5)
    parser.add_argument("--apply", action="store_true", help="真正写库（默认演练）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return 2

    db = Database(db_path)
    try:
        groups = insight.clusters(db, min_support=args.min_support, limit=20)
        print(f"库：{db_path}")
        print(f"可归纳的线索簇（≥{args.min_support} 条）：{len(groups)}")
        for cue, rows in groups[:10]:
            print(f"  - {cue}: {len(rows)} 条")
        if not groups:
            return 0

        sim = ShellSim(ROOT / ".tmp" / "induce_insights", model=args.model)
        report = insight.induce(db, SimTransport(sim),
                                min_support=args.min_support,
                                max_clusters=args.max_clusters,
                                commit=args.apply)
        for item in report:
            print("=" * 70)
            print(f"簇 {item['cluster']}｜ids={item.get('ids')}"
                  + (f"｜error={item['error']}" if item.get("error") else ""))
            for ins in item["insights"]:
                print(f"  [{ins['scope']}/{ins['confidence']}] {ins['content']}")
                print(f"      证据: {list(ins['source_ids'])}")
        if not args.apply:
            print("\n（演练模式：未写库；加 --apply 执行）")
        else:
            print(f"\n库内洞见总数：{db.count_l3('insight')}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

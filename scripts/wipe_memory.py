# -*- coding: utf-8 -*-
"""清空记忆库数据（保留表结构）。

用法（**先关闭 Harness**，否则运行中的插件会立刻回填当前会话）：
    python scripts/wipe_memory.py --yes
    python scripts/wipe_memory.py --db <memory.db> --yes

行为：
1. 备份到同目录 `backup/memory.db.bak-<时间戳>`（含 -wal/-shm）；
2. 按依赖顺序 DELETE 所有数据表；
3. FTS optimize + VACUUM + wal_checkpoint(TRUNCATE)；
4. 打印清理前后计数。表结构保留，下次启动正常迁移。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".dsh-longmemory" / "harness" / "memory.db"

TABLES = ("memory_fts", "memory", "message", "event",
          "experience", "cognition", "insight", "self_memory", "digest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清空记忆库数据（保留表结构）")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--yes", action="store_true", help="确认执行（否则只打印计划）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"库不存在：{db_path}")
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        targets = [table for table in TABLES if table in existing]
        before = {table: conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in targets}
        print(f"库：{db_path}")
        print("清理前：", before)
        if not args.yes:
            print("（演练模式；加 --yes 执行）")
            return 0

        backup_dir = db_path.parent / "backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = backup_dir / f"{db_path.name}.bak-{stamp}"
        shutil.copy2(db_path, backup)
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, backup.with_name(backup.name + suffix))
        print("备份：", backup)

        with conn:
            for table in targets:
                conn.execute(f"DELETE FROM {table}")
            if "sqlite_sequence" in existing:
                conn.execute("DELETE FROM sqlite_sequence")
        try:
            if "memory_fts" in targets:
                conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('optimize')")
                conn.commit()
        except sqlite3.Error:
            pass
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = {table: conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in targets}
        print("清理后：", after)
        print(f"库大小：{db_path.stat().st_size} 字节")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

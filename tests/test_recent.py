# -*- coding: utf-8 -*-
"""memory_recent 语义回归（批2）。

锁定的期望语义（评审定稿）：
- 事件按「发生区间与查询区间相交」返回；
- l3 按**入库时间**（created_at）落在区间内返回，且**独立配额**，不被事件挤掉；
- 时间比较跨时区正确（+08:00 与 +00:00 混存不错 8 小时）；
- 非法时间参数报参数错误，不是内部错误。
"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import retrieval
from database import Cognition, Database, Experience, Memory, Message

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_recent"
S1 = "s_recent_1"


def add_event(db, title, summary, offsets, *, session=S1):
    """建一个事件，并按给定时间戳挂消息（occur_at 原样写入，用于测时区）。"""
    global _SEQ
    event = db.create_event(session, title, summary=summary)
    for occur in offsets:
        _SEQ += 1
        msg_id = f"m_{event.id}_{_SEQ}"
        db.insert_message(Message(session_id=session, message_id=msg_id, owner="a",
                                  seq=_SEQ, role="user", content=title,
                                  occur_at=occur, created_at=occur))
        db.insert_memory(Memory(session_id=session, message_id=msg_id, owner="a",
                                seq=_SEQ, occur_at=occur, created_at=occur))
        db.bind_event(session, msg_id, event.id, strict=True)
    return event.id


_SEQ = 0


class RecentCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)


class TestRecentEvents(RecentCase):
    def test_overlapping_event_returned(self):
        add_event(self.db, "边框调整", "改成 1px",
                  ["2026-11-03T10:00:00+08:00", "2026-11-03T10:05:00+08:00"])
        hits = retrieval.recent(self.db, S1,
                                from_at="2026-11-03T09:00:00+08:00",
                                to_at="2026-11-03T11:00:00+08:00")
        self.assertEqual([hit.kind for hit in hits], ["event"])

    def test_non_overlapping_excluded(self):
        add_event(self.db, "边框调整", "改成 1px", ["2026-11-03T10:00:00+08:00"])
        hits = retrieval.recent(self.db, S1,
                                from_at="2026-11-03T20:00:00+08:00",
                                to_at="2026-11-03T21:00:00+08:00")
        self.assertEqual(hits, [])

    def test_timezone_mixed_offsets(self):
        """+08:00 的事件落在 UTC 窗口内：不能因为字典序比较被漏掉。"""
        add_event(self.db, "跨时区", "10:00+08:00 = 02:00Z",
                  ["2026-11-03T10:00:00+08:00"])
        hits = retrieval.recent(self.db, S1,
                                from_at="2026-11-03T01:00:00+00:00",
                                to_at="2026-11-03T03:00:00+00:00")
        self.assertEqual(len(hits), 1)

    def test_cross_session_replay_and_explicit_filter(self):
        first = add_event(self.db, "上午会话", "查插件",
                          ["2026-11-03T10:00:00+08:00"], session=S1)
        second = add_event(self.db, "下午会话", "审查记忆插件",
                           ["2026-11-03T10:05:00+08:00"], session="s_recent_2")
        window = {"from_at": "2026-11-03T09:00:00+08:00",
                  "to_at": "2026-11-03T11:00:00+08:00"}

        across = retrieval.recent(self.db, None, **window)
        self.assertEqual([hit.id for hit in across], [second, first])
        self.assertEqual([hit.meta["session_id"] for hit in across],
                         ["s_recent_2", S1])

        only_second = retrieval.recent(self.db, "s_recent_2", **window)
        self.assertEqual([hit.id for hit in only_second], [second])


class TestRecentL3(RecentCase):
    def test_l3_inside_window_returned(self):
        """旧 bug：先按 id 取 top-N 再按时间过滤 → 窗口内的条目取不到。"""
        self.db.insert_experience(Experience(
            content="窗口内的经验", source_event_id=1,
            created_at="2026-11-03T10:30:00+00:00", importance=1))
        # 之后灌入 40 条更「重要」的旧条目，把窗口内那条挤出 id DESC 的 top-N
        for index in range(40):
            self.db.insert_experience(Experience(
                content=f"旧经验 {index}", source_event_id=1,
                created_at="2026-10-01T09:00:00+00:00", importance=5))
        hits = retrieval.recent(self.db, S1,
                                from_at="2026-11-03T10:00:00+00:00",
                                to_at="2026-11-03T11:00:00+00:00")
        contents = [hit.text for hit in hits if hit.kind == "experience"]
        self.assertEqual(contents, ["窗口内的经验"])

    def test_l3_not_starved_by_events(self):
        """事件占满 limit 时，l3 仍要有独立配额。"""
        for index in range(5):
            add_event(self.db, f"事件{index}", "摘要",
                      [f"2026-11-03T10:0{index}:00+08:00"])
        self.db.insert_cognition(Cognition(
            subject="用户", content="偏好中文回复", source_event_id=1,
            created_at="2026-11-03T02:30:00+00:00"))
        hits = retrieval.recent(self.db, S1,
                                from_at="2026-11-03T00:00:00+00:00",
                                to_at="2026-11-03T11:00:00+00:00", limit=5)
        self.assertIn("cognition", {hit.kind for hit in hits})
        self.assertLessEqual(len(hits), 5)

    def test_l3_outside_window_excluded(self):
        self.db.insert_cognition(Cognition(
            subject="用户", content="很久以前的事", source_event_id=1,
            created_at="2026-10-01T09:00:00+00:00"))
        hits = retrieval.recent(self.db, S1,
                                from_at="2026-11-03T00:00:00+00:00",
                                to_at="2026-11-03T11:00:00+00:00")
        self.assertEqual(hits, [])


class TestRecentValidation(RecentCase):
    def test_bad_time_raises_value_error(self):
        with self.assertRaises(ValueError):
            retrieval.recent(self.db, S1, from_at="不是时间", to_at="2026-11-03T11:00:00+00:00")

    def test_from_after_to_raises(self):
        with self.assertRaises(ValueError):
            retrieval.recent(self.db, S1,
                             from_at="2026-11-03T12:00:00+00:00",
                             to_at="2026-11-03T11:00:00+00:00")


if __name__ == "__main__":
    unittest.main()

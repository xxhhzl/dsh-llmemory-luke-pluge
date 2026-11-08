# -*- coding: utf-8 -*-
"""向量索引与 embedding 代理（src/embed.py、Database.vec_*、app_setting）。

不联网：embedding 端点用假客户端代替；未安装 sqlite-vec 时跳过向量用例
（读路径仍走 FTS，属于设计内的降级）。
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import embed
import retrieval
from database import Cognition, Database, Experience

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_vec"
S1 = "s_vec"
DIM = 1024

try:
    import sqlite_vec  # noqa: F401
    HAS_VEC = True
except Exception:       # pragma: no cover - 环境相关
    HAS_VEC = False


def vec(index: int, weight: float = 1.0) -> list:
    '''单位向量：同一 index 距离 0，index 越远夹角越大。'''
    values = [0.0] * DIM
    values[index % DIM] = float(weight)
    return values


class VecCase(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def add_experience(self, content, *, context="导出前", cues=(), origin_project=None,
                       source_event_id=1):
        return self.db.insert_experience(Experience(
            context=context, content=content, kind="technique", importance=3,
            confidence=0.9, origin_project=origin_project,
            source_event_id=source_event_id, cues=tuple(cues),
            created_at="2026-11-03T10:00:00+00:00"))

    def add_cognition(self, content, *, subject="用户", origin_project=None,
                      source_event_id=1):
        return self.db.insert_cognition(Cognition(
            subject=subject, content=content, kind="preference", importance=3,
            confidence=0.9, origin_project=origin_project,
            source_event_id=source_event_id,
            created_at="2026-11-03T10:00:00+00:00"))


class TestAppSetting(VecCase):
    def test_roundtrip_and_overwrite(self):
        self.db.set_setting("model.jev", {"mode": "custom", "model": "m1"})
        self.assertEqual(self.db.get_setting("model.jev"),
                         {"mode": "custom", "model": "m1"})
        self.db.set_setting("model.jev", {"mode": "host"})
        self.assertEqual(self.db.get_setting("model.jev"), {"mode": "host"},
                         "同键覆盖写")
        self.assertEqual(self.db.list_settings()["model.jev"], {"mode": "host"})
        self.assertIsNone(self.db.get_setting("nope"))


@unittest.skipUnless(HAS_VEC, "未安装 sqlite-vec")
class TestVecTables(VecCase):
    def test_upsert_and_nearest(self):
        self.assertTrue(self.db._vec_enabled)
        near = self.add_experience("导出前先统一线宽")
        far = self.add_experience("配色只用深色模式")
        self.assertTrue(self.db.vec_upsert("experience", near, vec(0)))
        self.assertTrue(self.db.vec_upsert("experience", far, vec(500)))

        hits = self.db.vec_search("experience", vec(0), k=2)
        self.assertEqual(set(hits), {near, far})
        self.assertLess(hits[near], hits[far], "同向量的距离更小")

        status = self.db.vec_status()
        self.assertTrue(status["enabled"])
        self.assertEqual(status["dim"], DIM)
        self.assertEqual(status["counts"]["experience"], 2)

    def test_upsert_replaces_same_ref(self):
        item = self.add_experience("同一行换向量")
        self.db.vec_upsert("experience", item, vec(0))
        self.db.vec_upsert("experience", item, vec(200))
        self.assertEqual(self.db.vec_status()["counts"]["experience"], 1,
                         'ref_id 是主键，重写不能留两行')
        hits = self.db.vec_search("experience", vec(200), k=1)
        self.assertIn(item, hits)

    def test_project_scope_adds_shared(self):
        a = self.add_cognition("项目 A 用 pnpm", origin_project="proj_a")
        b = self.add_cognition("项目 B 用 npm", origin_project="proj_b")
        shared = self.add_cognition("用户偏好中文回复")
        for row_id, project in ((a, "proj_a"), (b, "proj_b"), (shared, "")):
            self.db.vec_upsert("cognition", row_id, vec(7), project)

        scoped = self.db.vec_search("cognition", vec(7), k=10,
                                    origin_project="proj_a", scope="project")
        self.assertEqual(set(scoped), {a, shared}, "本项目 + 通用，不含别的项目")
        everything = self.db.vec_search("cognition", vec(7), k=10, scope="all")
        self.assertEqual(set(everything), {a, b, shared})

    def test_delete(self):
        item = self.add_experience("待删向量")
        self.db.vec_upsert("experience", item, vec(3))
        self.assertTrue(self.db.vec_unindex("experience", item))
        self.assertEqual(self.db.vec_search("experience", vec(3), k=5), {})
        self.assertEqual(self.db.vec_status()["counts"]["experience"], 0)

    def test_invalidate_drops_vector(self):
        item = self.add_experience("被取代的做法")
        self.db.vec_upsert("experience", item, vec(4))
        self.db.invalidate_experience(item, "换了方案")
        self.assertEqual(self.db.vec_status()["counts"]["experience"], 0,
                         "失效条目不该继续占 KNN 名额")

    def test_supersede_drops_vector(self):
        old = self.add_experience("旧做法")
        self.db.vec_upsert("experience", old, vec(6))
        new = self.db.supersede_experience(old, Experience(
            context="导出前", content="新做法", kind="technique", importance=3,
            confidence=0.9, source_event_id=1,
            created_at="2026-11-03T10:00:00+00:00"))
        self.assertIsNotNone(new)
        self.assertNotIn(old, self.db.vec_search("experience", vec(6), k=5))

    def test_unknown_kind_is_noop(self):
        self.assertFalse(self.db.vec_upsert("event", 1, vec(0)))
        self.assertFalse(self.db.vec_unindex("event", 1))


class FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, texts, timeout=None):
        self.calls.append(list(texts))
        return [vec(index) for index in range(len(self.calls[-1]))]


class TestEmbedText(VecCase):
    def test_text_for_includes_all_fields(self):
        experience = Experience(context="导出前", content="统一线宽",
                                cues=("drawio",), source_event_id=1,
                                created_at="2026-11-03T10:00:00+00:00")
        text = embed.text_for("experience", experience)
        for part in ("导出前", "统一线宽", "drawio"):
            self.assertIn(part, text)

        cognition = Cognition(subject="用户", content="偏好中文回复",
                              kind="preference", cues=("偏好",))
        self.assertEqual(embed.text_for("cognition", cognition),
                         "用户 偏好中文回复 偏好")

    def test_index_rows_without_vec_is_noop(self):
        item = self.add_experience("没装扩展就不建索引")
        fake = FakeClient()
        with mock.patch.object(embed, "_client", fake):
            if self.db._vec_enabled:
                self.skipTest("本机装了 sqlite-vec，跳过降级分支")
            self.assertEqual(embed.index_rows(self.db, [("experience", item)]), 0)
        self.assertEqual(fake.calls, [], "不可用时不发 embedding 请求")


@unittest.skipUnless(HAS_VEC, "未安装 sqlite-vec")
class TestIndexRows(VecCase):
    def test_index_event_rows(self):
        experience = self.add_experience("导出前先统一线宽", cues=("drawio",))
        cognition = self.add_cognition("偏好中文回复")
        fake = FakeClient()
        with mock.patch.object(embed, "_client", fake):
            written = embed.index_event(self.db, 1)
        self.assertEqual(written, 2)
        self.assertEqual(len(fake.calls[0]), 2, "一批只发一次请求")
        self.assertTrue(any("统一线宽" in text for text in fake.calls[0]))

        hits = self.db.vec_search("experience", vec(0), k=5)
        self.assertIn(experience, hits)
        # experience 排在前，拿的是 vec(0)；cognition 拿 vec(1)
        self.assertIn(cognition, self.db.vec_search("cognition", vec(1), k=5))

    def test_inactive_rows_skipped(self):
        experience = self.add_experience("已经作废的做法")
        self.db.invalidate_experience(experience, "换了方案")
        fake = FakeClient()
        with mock.patch.object(embed, "_client", fake):
            self.assertEqual(embed.index_event(self.db, 1), 0)
        self.assertEqual(fake.calls, [])

    def test_client_error_is_reported(self):
        item = self.db.get_experience(self.add_experience("端点挂了"))
        errors = []

        class Failing:
            def request(self, texts, timeout=None):
                raise embed.EmbedError("embedding 端点 连不上（ECONNREFUSED）")

        with mock.patch.object(embed, "_client", Failing()):
            written = embed.index_rows(self.db, [("experience", item)],
                                       on_error=errors.append)
        self.assertEqual(written, 0)
        self.assertEqual(len(errors), 1, "失败要回调出去，调用方才能报错")
        self.assertIn("连不上", str(errors[0]))

    def test_count_mismatch_is_reported(self):
        item = self.db.get_experience(self.add_experience("条数不匹配"))
        errors = []

        class Short:
            def request(self, texts, timeout=None):
                return []

        with mock.patch.object(embed, "_client", Short()):
            written = embed.index_rows(self.db, [("experience", item)],
                                       on_error=errors.append)
        self.assertEqual(written, 0)
        self.assertIn("期望 1 条", str(errors[0]))


@unittest.skipUnless(HAS_VEC, "未安装 sqlite-vec")
class TestRRFFusion(VecCase):
    def test_vector_only_hit_is_recalled(self):
        # 正文与查询词零重叠：只有向量通道能捞回来
        target = self.add_cognition("导出前先统一线宽")
        self.db.vec_upsert("cognition", target, vec(5))
        hits = retrieval.search(self.db, "borderWidth", query_vector=vec(5),
                                touch=False)
        self.assertIn(target, [hit.id for hit in hits
                               if hit.kind == "cognition"],
                      "纯向量命中也要进候选")

    def test_both_channels_fused(self):
        lexical = self.add_cognition("drawio 导出要统一线宽")
        self.db.index_fts("cognition", lexical, "", "drawio 导出要统一线宽")
        semantic = self.add_cognition("边框粗细保持一致")
        self.db.vec_upsert("cognition", semantic, vec(9))

        hits = retrieval.search(self.db, "drawio", query_vector=vec(9), touch=False)
        ids = {hit.id for hit in hits if hit.kind == "cognition"}
        self.assertEqual(ids, {lexical, semantic}, "两路都能进结果")

    def test_no_vector_keeps_lexical_only(self):
        semantic = self.add_cognition("边框粗细保持一致")
        self.db.vec_upsert("cognition", semantic, vec(9))
        hits = retrieval.search(self.db, "drawio", touch=False)
        self.assertNotIn(semantic, [hit.id for hit in hits],
                         "没给 query_vector 就不该走向量通道")


class TestEmbedClient(unittest.TestCase):
    def setUp(self):
        self.q = base.message_queue()
        self.client = embed.EmbedClient(self.q, timeout=2.0)

    def _start(self, texts):
        out = {}

        def run():
            try:
                out["vectors"] = self.client.request(texts)
            except Exception as exc:      # pragma: no cover - 断言用
                out["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        frame = json.loads(self.q.recv_blocking(timeout=2.0))
        return thread, out, frame

    def test_request_frame_and_result(self):
        thread, out, frame = self._start(["甲", "乙"])
        self.assertEqual(frame["type"], "embed.request")
        payload = frame["data"]
        self.assertEqual(payload["input"], ["甲", "乙"])
        self.assertTrue(payload["request_id"])
        self.assertTrue(self.client.on_result({"data": {
            "request_id": payload["request_id"], "vectors": [[0.1], [0.2]]}}))
        thread.join(2.0)
        self.assertEqual(out["vectors"], [[0.1], [0.2]])

    def test_error_payload_raises(self):
        thread, out, frame = self._start(["甲"])
        self.client.on_result({"data": {
            "request_id": frame["data"]["request_id"],
            "error": {"message": "端点 404"}}})
        thread.join(2.0)
        self.assertIsInstance(out["error"], embed.EmbedError)
        self.assertIn("404", str(out["error"]))

    def test_missing_vectors_raises(self):
        thread, out, frame = self._start(["甲"])
        self.client.on_result({"data": {"request_id": frame["data"]["request_id"]}})
        thread.join(2.0)
        self.assertIn("vectors", str(out["error"]))

    def test_timeout(self):
        client = embed.EmbedClient(base.message_queue(), timeout=0.05)
        with self.assertRaises(embed.EmbedError):
            client.request(["甲"])

    def test_unmatched_result_ignored(self):
        self.assertFalse(self.client.on_result({"data": {"request_id": "nope"}}))


if __name__ == "__main__":
    unittest.main()

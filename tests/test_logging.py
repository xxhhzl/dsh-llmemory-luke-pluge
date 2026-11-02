# -*- coding: utf-8 -*-
"""日志封装测试：双输出、人类可读格式、级别、上下文、幂等、失败降级。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import io
import logging
import shutil
import sys
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import base
import config

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_log"
ROOT = logging.getLogger("longmemory")


class LoggingCase(unittest.TestCase):
    def setUp(self):
        BASE_TMP.mkdir(parents=True, exist_ok=True)
        self.dir = BASE_TMP / uuid.uuid4().hex
        self.dir.mkdir()
        self.log_path = self.dir / "longmemory.log"
        self._reset()
        base.clear_log_context()
        # 把 stderr 收进内存，避免测试刷屏；需要断言的用例再套一层 redirect_stderr
        self._stderr_guard = redirect_stderr(io.StringIO())
        self._stderr_guard.__enter__()

    def tearDown(self):
        self._reset()
        base.clear_log_context()
        self._stderr_guard.__exit__(None, None, None)
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def _reset():
        for handler in list(ROOT.handlers):
            ROOT.removeHandler(handler)
            handler.close()

    def read_log(self) -> str:
        for handler in ROOT.handlers:
            handler.flush()
        return self.log_path.read_text(encoding="utf-8") if self.log_path.exists() else ""


class TestSetup(LoggingCase):
    def test_writes_both_stderr_and_file(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            base.setup_logging(level="debug", path=self.log_path, force=True)
            base.log.info("ingest.accepted", new_messages=6)

        self.assertEqual(len(ROOT.handlers), 2)
        text = self.read_log()
        self.assertIn("ingest.accepted", text)
        self.assertIn("new_messages=6", text)
        self.assertIn("ingest.accepted", buf.getvalue())

    def test_human_readable_single_line_format(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        base.log.info("frame.received")
        line = self.read_log().strip().splitlines()[-1]
        # <ISO 时间> <级别> <logger> <事件>
        parts = line.split()
        self.assertRegex(parts[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$")
        self.assertEqual(parts[1], "INFO")
        self.assertEqual(parts[2], "longmemory")
        self.assertEqual(parts[3], "frame.received")

    def test_setup_is_idempotent(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        handlers = list(ROOT.handlers)
        base.setup_logging(level="debug", path=self.log_path)
        self.assertEqual(ROOT.handlers, handlers)
        self.assertEqual(ROOT.level, logging.INFO)

    def test_sub_logger_name(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        base.get_logger("db").info("db.opened")
        self.assertIn("longmemory.db db.opened", self.read_log())

    def test_log_never_touches_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(out), redirect_stderr(err):
            base.setup_logging(level="info", path=self.log_path, force=True)
            base.log.error("something.bad")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("something.bad", err.getvalue())


class TestLevel(LoggingCase):
    def test_level_filters_records(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        base.set_log_level("error")
        base.log.info("should.not.appear")
        base.log.error("should.appear")
        text = self.read_log()
        self.assertNotIn("should.not.appear", text)
        self.assertIn("should.appear", text)

    def test_config_set_log_level(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        config.patch_config({"log": {"level": "debug"}})
        self.assertEqual(ROOT.level, logging.DEBUG)
        config.patch_config({"log": {"level": "info"}})


class TestContext(LoggingCase):
    def test_context_is_attached_by_default(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        base.set_log_context(session_id="s_1", agent_id="lead#0",
                             request_id="c-1", job_id=None)
        base.log.info("frame.received", frame_type="health")
        line = self.read_log().strip().splitlines()[-1]
        self.assertIn("session_id=s_1", line)
        self.assertIn("agent_id=lead#0", line)
        self.assertIn("request_id=c-1", line)
        self.assertIn("frame_type=health", line)
        self.assertNotIn("job_id=", line)      # None 不输出

    def test_bind_adds_fields(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        base.set_log_context(session_id="s_1")
        base.log.bind(stage="ingest").info("evt")
        line = self.read_log().strip().splitlines()[-1]
        self.assertIn("session_id=s_1", line)
        self.assertIn("stage=ingest", line)

    def test_clear_context(self):
        base.setup_logging(level="info", path=self.log_path, force=True)
        base.set_log_context(session_id="s_1")
        base.clear_log_context()
        base.log.info("evt")
        self.assertNotIn("session_id=", self.read_log())


class TestDefaults(LoggingCase):
    def test_default_path_follows_db_path(self):
        saved = copy.deepcopy(config._config)
        saved_revision = config.get_revision()
        try:
            config._config["store"]["db_path"] = str(self.dir / "data" / "memory.db")
            self.assertEqual(base._default_log_path(),
                             self.dir / "data" / "longmemory.log")
        finally:
            config._config = saved
            config._revision = saved_revision

    def test_unwritable_file_degrades_to_stderr(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            base.setup_logging(level="info", path=self.dir, force=True)  # 目录当文件
        self.assertEqual(len(ROOT.handlers), 1)
        self.assertIn("log.file.unavailable", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

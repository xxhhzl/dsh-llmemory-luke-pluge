# -*- coding: utf-8 -*-
"""入库脱敏：规则本身 + 写入链路真的用了它。"""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ingest
import sanitize
from database import Database

BASE_TMP = Path(__file__).resolve().parents[1] / ".tmp" / "test_sanitize"
S1 = "s_sanitize"


class TestRedact(unittest.TestCase):
    def test_env_assignment(self):
        out = sanitize.redact("$env:ZYM_PASS='Qq.20060115'")
        self.assertNotIn("Qq.20060115", out)
        self.assertIn("ZYM_PASS", out)
        self.assertIn("[REDACTED]", out)

    def test_unquoted_env_assignment(self):
        out = sanitize.redact("$env:ZYM_PASS=Qq.20060115")
        self.assertNotIn("Qq.20060115", out)

    def test_key_value_forms(self):
        for text in ("password: hunter2xyz", "token=abcd1234efgh",
                     "API_KEY: sk-live-1234567890"):
            self.assertNotIn("hunter2xyz", sanitize.redact(text))
            self.assertIn("[REDACTED]", sanitize.redact(text))

    def test_cli_flag_and_bearer(self):
        self.assertNotIn("hunter2", sanitize.redact("mysql --password hunter2"))
        self.assertNotIn("abcdefgh123456",
                         sanitize.redact("Authorization: Bearer abcdefgh123456"))

    def test_bare_keys(self):
        for secret in ("sk-abcdef123456", "ghp_abcdefgh1234", "AKIAIOSFODNN7EXAMPLE"):
            self.assertEqual(sanitize.redact(secret), "[REDACTED]")

    def test_json_and_basic_credentials(self):
        import json
        raw = '{"api_key":"customsecret123","nested":{"password":"mysecretvalue"}}'
        masked = sanitize.redact(raw)
        self.assertEqual(json.loads(masked), {
            "api_key": "[REDACTED]", "nested": {"password": "[REDACTED]"}})
        self.assertEqual(sanitize.redact("password=topsecret123"),
                         "password=[REDACTED]")
        self.assertEqual(sanitize.redact("Authorization: Basic dXNlcjpwYXNz"),
                         "Authorization: [REDACTED]")
        self.assertEqual(sanitize.redact(masked), masked, "重复脱敏必须幂等")

    def test_plain_text_untouched(self):
        text = "把 .env 里的 ZYM_PASS 读进环境变量，再跑部署脚本"
        self.assertEqual(sanitize.redact(text), text)


class TestIngestUsesRedaction(unittest.TestCase):
    def setUp(self):
        self._dir = BASE_TMP / uuid.uuid4().hex
        self._dir.mkdir(parents=True)
        self.db = Database(self._dir / "memory.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self._dir, ignore_errors=True)

    def test_message_written_with_mask(self):
        job = {"session_id": S1, "messages": [
            {"msg_id": "m1", "seq": 0, "role": "user",
             "content": "$env:ZYM_PASS='Qq.20060115'; python deploy.py"}]}
        ingest.IngestWorker._store_messages(None, self.db, job)
        stored = self.db.get_message(S1, "m1").content
        self.assertNotIn("Qq.20060115", stored, "明文密码不该落库")
        self.assertIn("[REDACTED]", stored)
        self.assertIn("python deploy.py", stored, "其余内容要保留")


if __name__ == "__main__":
    unittest.main()

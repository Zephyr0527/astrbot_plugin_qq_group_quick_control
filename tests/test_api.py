from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault(
    "ASTRBOT_ROOT", tempfile.mkdtemp(prefix="qqgqc-api-test-root-")
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.api.web import bind_request_context  # noqa: E402
from astrbot.core.star.star_tools import StarTools  # noqa: E402
from astrbot_plugin_qq_group_quick_control.main import (  # noqa: E402
    QQGroupQuickControl,
)


class FakePlatformManager:
    def __init__(self):
        self.platform_insts = []


class FakeContext:
    def __init__(self, config=None):
        self.config = config or {}
        self.platform_manager = FakePlatformManager()
        self.web_apis = []

    def get_config(self, umo=None):
        return self.config

    def get_all_stars(self):
        return []

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis.append((route, handler, methods, desc))


class FakeRequest:
    def __init__(self, body=None, username="astrbot"):
        self.username = username
        self._body = body
        self.query = {}

    async def json(self, default=None, **kwargs):
        return self._body


class WebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.data_patch = patch.object(
            StarTools, "get_data_dir", return_value=Path(self._temp.name)
        )
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

        self.context = FakeContext(
            {
                "provider_ltm_settings": {
                    "active_reply": {
                        "enable": True,
                        "method": "possibility_reply",
                        "possibility_reply": 0.3,
                        "whitelist": [],
                    }
                },
                "platform_settings": {"enable_id_white_list": False},
            }
        )
        self.plugin = QQGroupQuickControl(self.context)
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def call(self, handler, body=None, username="astrbot"):
        with bind_request_context(FakeRequest(body, username=username)):
            response = await handler()
        return response.status_code, json.loads(response.body)

    async def test_read_requires_login(self):
        status, payload = await self.call(self.plugin._api_groups, username="")
        self.assertEqual(status, 401)
        self.assertEqual(payload["status"], "error")

    async def test_write_rejects_api_key_identity(self):
        status, payload = await self.call(
            self.plugin._api_save,
            {
                "group_id": "123456789",
                "auto_reply_enabled": False,
            },
            username="api_key:abc",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["status"], "error")

    async def test_groups_empty_state_and_warnings(self):
        status, payload = await self.call(self.plugin._api_groups)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["groups"], [])
        self.assertTrue(payload["data"]["warnings"])
        self.assertEqual(payload["data"]["global"]["possibility_reply"], 0.3)

    async def test_add_save_reset_flow(self):
        status, payload = await self.call(
            self.plugin._api_add,
            {"group_id": "123456789", "remark": "测试群"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        group = payload["data"]["group"]
        self.assertTrue(group["configured"])
        self.assertTrue(group["auto_reply_enabled"])
        self.assertFalse(group["meme_enabled"])
        self.assertTrue(group["respond_enabled"])
        self.assertEqual(group["probability_percent"], 30.0)

        status, payload = await self.call(
            self.plugin._api_save,
            {
                "group_id": "123456789",
                "remark": "主测试群",
                "auto_reply_enabled": True,
                "meme_enabled": True,
                "respond_enabled": False,
                "reply_probability": 0.55,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        group = payload["data"]["group"]
        self.assertEqual(group["remark"], "主测试群")
        self.assertEqual(group["probability_percent"], 55.0)
        self.assertTrue(group["meme_enabled"])
        self.assertFalse(group["respond_enabled"])

        status, payload = await self.call(
            self.plugin._api_reset, {"group_id": "123456789"}
        )
        self.assertEqual(status, 200)
        group = payload["data"]["group"]
        self.assertEqual(group["remark"], "主测试群")
        self.assertFalse(group["auto_reply_enabled"])
        self.assertFalse(group["meme_enabled"])
        self.assertTrue(group["respond_enabled"])
        self.assertEqual(group["probability_percent"], 20.0)

    async def test_invalid_inputs_are_rejected(self):
        cases = [
            {"group_id": "abc"},
            {"group_id": "123456789", "reply_probability": 1.5},
            {"group_id": "123456789", "auto_reply_enabled": "yes"},
            {"group_id": "123456789", "meme_enabled": 1},
            {"group_id": "123456789", "respond_enabled": "true"},
            {"group_id": "123456789", "remark": "a" * 65},
            {"group_id": "123456789", "remark": "line\nbreak"},
        ]
        for body in cases:
            with self.subTest(body=body):
                status, payload = await self.call(self.plugin._api_save, body)
                self.assertEqual(status, 200)
                self.assertEqual(payload["status"], "error")
                self.assertTrue(payload["message"])

    async def test_save_creates_config_without_add(self):
        status, payload = await self.call(
            self.plugin._api_save,
            {
                "group_id": "123456",
                "remark": "直接保存",
                "auto_reply_enabled": False,
                "meme_enabled": False,
                "respond_enabled": True,
                "reply_probability": 0.12,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["group"]["probability_percent"], 12.0)
        saved = self.plugin.store.get("123456")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.reply_probability, 0.12)


if __name__ == "__main__":
    unittest.main()


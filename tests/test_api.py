from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault(
    "ASTRBOT_ROOT", tempfile.mkdtemp(prefix="qqgqc-api-test-root-")
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.api.web import bind_request_context  # noqa: E402
from astrbot.core.star.star_tools import StarTools  # noqa: E402
from astrbot_plugin_qq_group_quick_control.core import default_group_config  # noqa: E402
from astrbot_plugin_qq_group_quick_control.main import (  # noqa: E402
    QQGroupQuickControl,
)


class FakeOneBotClient:
    def __init__(
        self,
        *,
        qq="3566720028",
        nickname="Deepseek",
        groups=None,
        fail_login=False,
        fail_groups=False,
    ):
        self.qq = str(qq)
        self.nickname = nickname
        self.groups = (
            groups
            if groups is not None
            else [
                {"group_id": 123456789, "group_name": "测试群 A", "member_count": 42},
                {"group_id": 784368663, "group_name": "测试群 B", "member_count": 10},
                {"group_id": 123456, "group_name": "测试群 C", "member_count": 3},
            ]
        )
        self.fail_login = fail_login
        self.fail_groups = fail_groups

    async def call_action(self, action):
        if action == "get_login_info":
            if self.fail_login:
                raise RuntimeError("login failed")
            return {
                "status": "ok",
                "retcode": 0,
                "data": {"user_id": int(self.qq), "nickname": self.nickname},
            }
        if action == "get_group_list":
            if self.fail_groups:
                raise RuntimeError("group list failed")
            return {"status": "ok", "retcode": 0, "data": self.groups}
        raise RuntimeError(f"unsupported action {action}")


class FakePlatform:
    def __init__(self, client=None, *, platform_id="qq-snowluma", name="aiocqhttp"):
        self.client = client or FakeOneBotClient()
        self._meta = SimpleNamespace(id=platform_id, name=name)

    def meta(self):
        return self._meta

    def get_client(self):
        return self.client


class FakePlatformManager:
    def __init__(self, platforms=None):
        self.platform_insts = list(platforms or [FakePlatform()])


class FakeContext:
    def __init__(self, config=None, platforms=None):
        self.config = config or {}
        self.platform_manager = FakePlatformManager(platforms)
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

    async def test_groups_only_include_verified_bot_account(self):
        status, payload = await self.call(self.plugin._api_groups)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        data = payload["data"]
        self.assertEqual(data["list_status"], "ok")
        self.assertTrue(data["bot_account"]["verified"])
        self.assertEqual(data["bot_account"]["qq"], "3566720028")
        self.assertEqual(data["bot_account"]["nickname"], "Deepseek")
        self.assertEqual(
            {item["group_id"] for item in data["groups"]},
            {"123456789", "784368663", "123456"},
        )
        self.assertEqual(data["counts"]["total"], 3)
        self.assertEqual(data["global"]["possibility_reply"], 0.3)

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

    async def test_account_mismatch_blocks_list_and_writes(self):
        self.context.platform_manager.platform_insts = [
            FakePlatform(FakeOneBotClient(qq="3360457297", nickname="Zephyr"))
        ]
        status, payload = await self.call(self.plugin._api_groups)
        self.assertEqual(status, 200)
        data = payload["data"]
        self.assertEqual(data["list_status"], "error")
        self.assertEqual(data["groups"], [])
        self.assertFalse(data["bot_account"]["verified"])
        self.assertEqual(data["bot_account"]["qq"], "3360457297")
        self.assertIn("不匹配", data["list_error"])

        status, payload = await self.call(
            self.plugin._api_add, {"group_id": "123456789"}
        )
        self.assertEqual(payload["status"], "error")
        self.assertIsNone(self.plugin.store.get("123456789"))

    async def test_missing_bound_platform_blocks_list_and_writes(self):
        self.context.platform_manager.platform_insts = [
            FakePlatform(FakeOneBotClient(), platform_id="other-bot")
        ]
        status, payload = await self.call(self.plugin._api_groups)
        data = payload["data"]
        self.assertEqual(data["list_status"], "error")
        self.assertEqual(data["groups"], [])
        self.assertIn("qq-snowluma", data["list_error"])
        self.assertIsNone(self.plugin.store.get("123456789"))

    async def test_group_list_failure_is_explicit(self):
        self.context.platform_manager.platform_insts = [
            FakePlatform(FakeOneBotClient(fail_groups=True))
        ]
        status, payload = await self.call(self.plugin._api_groups)
        data = payload["data"]
        self.assertEqual(data["list_status"], "error")
        self.assertEqual(data["groups"], [])
        self.assertTrue(data["bot_account"]["verified"])
        self.assertIn("读取群列表失败", data["list_error"])

    async def test_malformed_group_list_is_explicit(self):
        self.context.platform_manager.platform_insts = [
            FakePlatform(FakeOneBotClient(groups={"bad": "shape"}))
        ]
        status, payload = await self.call(self.plugin._api_groups)
        data = payload["data"]
        self.assertEqual(data["list_status"], "error")
        self.assertEqual(data["groups"], [])
        self.assertIn("格式异常", data["list_error"])

    async def test_failed_refresh_does_not_reuse_old_cache(self):
        first = await self.plugin._get_group_list(force=True)
        self.assertTrue(first.ok)
        self.context.platform_manager.platform_insts = [
            FakePlatform(FakeOneBotClient(qq="3360457297", nickname="Zephyr"))
        ]
        failed = await self.plugin._get_group_list(force=True)
        self.assertFalse(failed.ok)
        again = await self.plugin._get_group_list(force=False)
        self.assertFalse(again.ok)
        self.assertEqual(again.groups, [])

    async def test_unverified_cache_does_not_add_groups(self):
        await self.plugin.store.upsert(default_group_config("999999"))
        self.plugin.store.note_observed("888888", "未验证群")
        await self.plugin.store.flush_observed()
        self.context.config["provider_ltm_settings"]["active_reply"]["whitelist"] = [
            "777777"
        ]
        status, payload = await self.call(self.plugin._api_groups)
        data = payload["data"]
        ids = {item["group_id"] for item in data["groups"]}
        self.assertNotIn("999999", ids)
        self.assertNotIn("888888", ids)
        self.assertNotIn("777777", ids)
        self.assertEqual(data["preserved"]["configured_not_in_bot"], 1)
        self.assertEqual(data["preserved"]["observed_not_in_bot"], 1)
        self.assertEqual(data["preserved"]["whitelist_not_in_bot"], 1)

    async def test_empty_group_list_is_valid_but_blocks_add(self):
        self.context.platform_manager.platform_insts = [
            FakePlatform(FakeOneBotClient(groups=[]))
        ]
        status, payload = await self.call(self.plugin._api_groups)
        data = payload["data"]
        self.assertEqual(data["list_status"], "ok")
        self.assertEqual(data["groups"], [])
        self.assertTrue(data["bot_account"]["verified"])
        status, payload = await self.call(
            self.plugin._api_add, {"group_id": "123456789"}
        )
        self.assertEqual(payload["status"], "error")

    async def test_save_rejects_group_not_joined_by_bot(self):
        status, payload = await self.call(
            self.plugin._api_save,
            {
                "group_id": "999999",
                "auto_reply_enabled": False,
            },
        )
        self.assertEqual(payload["status"], "error")
        self.assertIsNone(self.plugin.store.get("999999"))


if __name__ == "__main__":
    unittest.main()


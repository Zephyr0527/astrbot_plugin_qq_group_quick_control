from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault(
    "ASTRBOT_ROOT", tempfile.mkdtemp(prefix="qqgqc-runtime-test-root-")
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.star.star_tools import StarTools  # noqa: E402
from astrbot_plugin_qq_group_quick_control.core import (  # noqa: E402
    default_group_config,
)
from astrbot_plugin_qq_group_quick_control.main import (  # noqa: E402
    MEME_DISABLED_ORIGIN,
    MEME_MANAGER_ORIGIN_KEY,
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
        self.stars = []

    def get_config(self, umo=None):
        return self.config

    def get_all_stars(self):
        return self.stars

    def register_web_api(self, route, handler, methods, desc):
        self.web_apis.append((route, handler, methods, desc))


class FakeEvent:
    def __init__(self, group_id="123456789", group_name="真实群名"):
        self._group_id = group_id
        self.group_name = group_name
        self.unified_msg_origin = f"qq:GroupMessage:{group_id}"
        self.is_at_or_wake_command = False
        self.call_llm = False
        self._extras = {}
        self._stopped = False
        self._has_send_oper = False
        self.message_obj = SimpleNamespace(
            group=SimpleNamespace(group_id=group_id, group_name=group_name)
        )

    def get_group_id(self):
        return self._group_id

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def should_call_llm(self, value):
        self.call_llm = value

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped


class FakeGroupChatContextOriginal:
    def __init__(self):
        self.calls = 0
        self.cfg_result = {
            "enable_active_reply": True,
            "ar_method": "possibility_reply",
            "ar_whitelist": [],
            "ar_possibility": 0.5,
        }

    def cfg(self, event):
        return self.cfg_result

    async def need_active_reply(self, event):
        self.calls += 1
        return True


class FakeStealerPlugin:
    def __init__(self):
        self.calls = 0

    def is_send_enabled_for_event(self, event):
        self.calls += 1
        return True


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.temp_path = Path(self._temp.name)
        self.data_patch = patch.object(
            StarTools, "get_data_dir", return_value=self.temp_path
        )
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)

        from astrbot.builtin_stars.astrbot.group_chat_context import (
            GroupChatContext,
        )

        self.GroupChatContext = GroupChatContext
        self.real_need_active_reply = GroupChatContext.need_active_reply
        self.original = FakeGroupChatContextOriginal()

        async def fake_need_active_reply(context_obj, event):
            self.original.calls += 1
            return True

        self.fake_need_active_reply = fake_need_active_reply
        GroupChatContext.need_active_reply = fake_need_active_reply

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
        self.stealer = FakeStealerPlugin()
        self.context.stars.append(
            SimpleNamespace(
                name="astrbot_plugin_stealer",
                version="3.1.6",
                star_cls=self.stealer,
            )
        )

        self.plugin = QQGroupQuickControl(self.context)
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.GroupChatContext.need_active_reply = self.real_need_active_reply

    async def test_web_routes_registered(self):
        routes = {route for route, _, _, _ in self.context.web_apis}
        self.assertIn("/astrbot_plugin_qq_group_quick_control/groups", routes)
        self.assertIn("/astrbot_plugin_qq_group_quick_control/groups/save", routes)
        self.assertIn("/astrbot_plugin_qq_group_quick_control/groups/add", routes)
        self.assertIn("/astrbot_plugin_qq_group_quick_control/groups/reset", routes)

    async def test_unconfigured_group_uses_original_active_reply(self):
        event = FakeEvent("123456789")
        result = await self.GroupChatContext.need_active_reply(
            SimpleNamespace(), event
        )
        self.assertTrue(result)
        self.assertEqual(self.original.calls, 1)

    async def test_configured_group_disables_active_reply(self):
        config = default_group_config(
            "123456789", auto_reply_enabled=False, reply_probability=1.0
        )
        await self.plugin.store.upsert(config)
        event = FakeEvent("123456789")
        ctx = SimpleNamespace(cfg=lambda evt: self.original.cfg_result)
        result = await self.GroupChatContext.need_active_reply(ctx, event)
        self.assertFalse(result)
        self.assertEqual(self.original.calls, 0)

    async def test_configured_group_uses_independent_probability(self):
        await self.plugin.store.upsert(
            default_group_config(
                "111111", auto_reply_enabled=True, reply_probability=0.0
            )
        )
        await self.plugin.store.upsert(
            default_group_config(
                "222222", auto_reply_enabled=True, reply_probability=1.0
            )
        )
        ctx = SimpleNamespace(cfg=lambda evt: self.original.cfg_result)
        first = FakeEvent("111111")
        second = FakeEvent("222222")
        self.assertFalse(
            await self.GroupChatContext.need_active_reply(ctx, first)
        )
        self.assertTrue(
            await self.GroupChatContext.need_active_reply(ctx, second)
        )
        self.assertTrue(second.get_extra("qqgqc_active_reply"))
        self.assertEqual(self.original.calls, 0)

    async def test_configured_group_respects_global_whitelist(self):
        await self.plugin.store.upsert(
            default_group_config(
                "123456789", auto_reply_enabled=True, reply_probability=1.0
            )
        )
        ctx = SimpleNamespace(
            cfg=lambda evt: {
                "enable_active_reply": True,
                "ar_method": "possibility_reply",
                "ar_whitelist": ["999999"],
            }
        )
        self.assertFalse(
            await self.GroupChatContext.need_active_reply(ctx, FakeEvent("123456789"))
        )

    async def test_respond_gate_sets_call_llm_without_stopping(self):
        await self.plugin.store.upsert(
            default_group_config("123456789", respond_enabled=False)
        )
        event = FakeEvent("123456789")
        await self.plugin.suppress_default_chat(event)
        self.assertTrue(event.call_llm)
        self.assertFalse(event.is_stopped())

    async def test_respond_gate_stops_default_llm(self):
        await self.plugin.store.upsert(
            default_group_config("123456789", respond_enabled=False)
        )
        event = FakeEvent("123456789")
        await self.plugin.gate_waiting_llm(event)
        self.assertTrue(event.is_stopped())

    async def test_respond_gate_allows_plugin_llm(self):
        await self.plugin.store.upsert(
            default_group_config("123456789", respond_enabled=False)
        )
        event = FakeEvent("123456789")
        event.set_extra("provider_request", object())
        await self.plugin.gate_waiting_llm(event)
        self.assertFalse(event.is_stopped())

    async def test_respond_gate_blocks_marked_active_reply(self):
        await self.plugin.store.upsert(
            default_group_config("123456789", respond_enabled=False)
        )
        event = FakeEvent("123456789")
        event.set_extra("provider_request", object())
        event.set_extra("qqgqc_active_reply", True)
        await self.plugin.gate_waiting_llm(event)
        self.assertTrue(event.is_stopped())

    async def test_meme_origin_gate_for_meme_disabled_group(self):
        await self.plugin.store.upsert(
            default_group_config("123456789", meme_enabled=False)
        )
        event = FakeEvent("123456789")
        event.set_extra("provider_request", object())
        await self.plugin.gate_waiting_llm(event)
        self.assertEqual(
            event.get_extra(MEME_MANAGER_ORIGIN_KEY), MEME_DISABLED_ORIGIN
        )

        second = FakeEvent("123456789")
        await self.plugin.mark_meme_disabled_before_manager(second, object())
        self.assertEqual(
            second.get_extra(MEME_MANAGER_ORIGIN_KEY), MEME_DISABLED_ORIGIN
        )

    async def test_meme_origin_gate_does_not_touch_unconfigured_group(self):
        event = FakeEvent("123456789")
        event.set_extra("provider_request", object())
        await self.plugin.gate_waiting_llm(event)
        self.assertIsNone(event.get_extra(MEME_MANAGER_ORIGIN_KEY))

    async def test_stealer_send_gate(self):
        await self.plugin.store.upsert(
            default_group_config("123456789", meme_enabled=False)
        )
        event = FakeEvent("123456789")
        self.assertFalse(self.stealer.is_send_enabled_for_event(event))

        allowed = FakeEvent("999999")
        self.assertTrue(self.stealer.is_send_enabled_for_event(allowed))

    async def test_view_exposes_effective_flags(self):
        await self.plugin.store.upsert(
            default_group_config(
                "123456789",
                auto_reply_enabled=True,
                meme_enabled=False,
                respond_enabled=False,
                reply_probability=0.25,
            )
        )
        view = self.plugin._build_group_view(
            "123456789",
            self.plugin.store.get("123456789"),
            source="config",
            group_name="真实群名",
        )
        self.assertTrue(view["configured"])
        self.assertEqual(view["probability_percent"], 25.0)
        self.assertTrue(view["effective"]["auto_reply"])
        self.assertTrue(view["effective"]["meme_suppressed"])
        self.assertTrue(view["effective"]["respond_suppressed"])

    async def test_unconfigured_view_is_inherited(self):
        view = self.plugin._build_group_view(
            "123456789", None, source="observed", group_name="真实群名"
        )
        self.assertFalse(view["configured"])
        self.assertTrue(view["auto_reply_enabled"])
        self.assertTrue(view["meme_enabled"])
        self.assertTrue(view["respond_enabled"])
        self.assertEqual(view["probability_percent"], 30.0)


if __name__ == "__main__":
    unittest.main()


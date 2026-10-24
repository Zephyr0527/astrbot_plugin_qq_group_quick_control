from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault(
    "ASTRBOT_ROOT", tempfile.mkdtemp(prefix="qqgqc-page-test-root-")
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.core.star.star import StarMetadata  # noqa: E402
from astrbot.dashboard.services.plugin_page_service import (  # noqa: E402
    PluginPageService,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_qq_group_quick_control"


class FakeContext:
    def __init__(self, metadata):
        self._metadata = metadata

    def get_all_stars(self):
        return [self._metadata]


class FakePluginManager:
    def __init__(self, metadata):
        self.plugin_store_path = str(PLUGIN_ROOT.parent)
        self.reserved_plugin_path = str(PLUGIN_ROOT.parent)
        self.context = FakeContext(metadata)


class FakeConfig:
    def __init__(self, payload):
        self._payload = payload

    def get(self, key, default=None):
        return self._payload.get(key, default)


class PageServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.metadata = StarMetadata(
            name=PLUGIN_NAME,
            display_name="QQ群聊一键操作",
            root_dir_name=PLUGIN_NAME,
            activated=True,
        )
        self.manager = FakePluginManager(self.metadata)
        self.service = PluginPageService(
            self.manager,
            config=FakeConfig(
                {
                    "dashboard": {
                        "jwt_secret": "test-secret-for-page-token-0123456789abcdef"
                    }
                }
            ),
        )

    async def test_page_discovery_and_assets(self):
        pages = await self.service.discover_plugin_pages(self.metadata)
        self.assertEqual([page.name for page in pages], ["qq_group_quick_control"])

        html = await self.service.serve_page_content(
            plugin_name=PLUGIN_NAME,
            view_name="qq_group_quick_control",
            asset_path="",
            asset_token="",
            username="astrbot",
            locale="zh-CN",
            theme="dark",
        )
        self.assertEqual(html.content_type, "text/html; charset=utf-8")
        self.assertIn("QQ群聊一键操作", html.content)
        self.assertIn("/api/plugin/page/bridge-sdk.js", html.content)
        self.assertIn('data-theme="dark"', html.content)
        self.assertIn("asset_token=", html.content)

        js = await self.service.serve_page_content(
            plugin_name=PLUGIN_NAME,
            view_name="qq_group_quick_control",
            asset_path="app.js",
            asset_token="",
            username="astrbot",
            locale="zh-CN",
            theme=None,
        )
        self.assertEqual(js.content_type, "text/javascript")
        self.assertIn("AstrBotPluginPage", js.content)

        css = await self.service.serve_page_content(
            plugin_name=PLUGIN_NAME,
            view_name="qq_group_quick_control",
            asset_path="style.css",
            asset_token="",
            username="astrbot",
            locale="zh-CN",
            theme=None,
        )
        self.assertEqual(css.content_type, "text/css")
        self.assertIn("--primary", css.content)

    async def test_entry_config_contains_content_path(self):
        config = await self.service.get_plugin_page_entry_config(
            plugin_name=PLUGIN_NAME,
            view_name="qq_group_quick_control",
            username="astrbot",
            locale="zh-CN",
        )
        self.assertEqual(config["name"], "qq_group_quick_control")
        self.assertEqual(
            config["title"], "qq_group_quick_control"
        )
        self.assertIn("/api/v1/plugins/", config["content_path"])
        self.assertIn("_t/", config["content_path"])

    async def test_path_traversal_is_rejected(self):
        with self.assertRaises(Exception):
            await self.service.serve_page_content(
                plugin_name=PLUGIN_NAME,
                view_name="qq_group_quick_control",
                asset_path="../metadata.yaml",
                asset_token="",
                username="astrbot",
                locale="zh-CN",
                theme=None,
            )


if __name__ == "__main__":
    unittest.main()


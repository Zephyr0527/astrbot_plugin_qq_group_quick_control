from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_qq_group_quick_control.core import (  # noqa: E402
    GroupConfig,
    default_group_config,
)
from astrbot_plugin_qq_group_quick_control.store import GroupConfigStore  # noqa: E402


class StoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "groups.json"

    async def make_store(self):
        store = GroupConfigStore(self.path)
        await store.load()
        return store

    async def test_empty_start_and_round_trip(self):
        store = await self.make_store()
        self.assertIsNone(store.get("123456789"))
        self.assertEqual(store.snapshot(), {})

        config = default_group_config(
            "123456789",
            remark="测试群",
            auto_reply_enabled=True,
            meme_enabled=False,
            respond_enabled=True,
            reply_probability=0.35,
        )
        saved = await store.upsert(config)
        self.assertTrue(saved.created_at)
        self.assertTrue(saved.updated_at)
        self.assertTrue(self.path.is_file())

        reloaded = await self.make_store()
        again = reloaded.get("123456789")
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(again.remark, "测试群")
        self.assertTrue(again.auto_reply_enabled)
        self.assertFalse(again.meme_enabled)
        self.assertEqual(again.reply_probability, 0.35)

    async def test_updates_are_independent_and_persisted(self):
        store = await self.make_store()
        await store.upsert(default_group_config("111111", reply_probability=0.2))
        await store.upsert(default_group_config("222222", reply_probability=0.5))
        await store.upsert(default_group_config("111111", reply_probability=0.7))

        reloaded = await self.make_store()
        first = reloaded.get("111111")
        second = reloaded.get("222222")
        assert first is not None and second is not None
        self.assertEqual(first.reply_probability, 0.7)
        self.assertEqual(second.reply_probability, 0.5)

    async def test_backup_and_corrupt_recovery(self):
        store = await self.make_store()
        await store.upsert(default_group_config("111111", remark="第一版"))
        await store.upsert(default_group_config("222222", remark="第二版"))
        self.assertTrue(store.backup_path.is_file())

        # Damage the primary file, keeping the previous version in .bak.
        self.path.write_text("{not json", encoding="utf-8")
        recovered = await self.make_store()
        self.assertIn("读取", recovered.load_warning)
        self.assertIsNotNone(recovered.get("111111"))
        self.assertIsNone(recovered.get("222222"))
        self.assertTrue(list(self.path.parent.glob("groups.json.corrupt-*")))

    async def test_invalid_group_entries_are_skipped(self):
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "groups": {
                        "123456789": {
                            "group_id": "123456789",
                            "remark": "ok",
                            "auto_reply_enabled": True,
                            "meme_enabled": False,
                            "respond_enabled": True,
                            "reply_probability": 0.2,
                        },
                        "bad": {
                            "group_id": "bad",
                            "reply_probability": 2,
                        },
                    },
                    "observed": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        store = await self.make_store()
        self.assertIsNotNone(store.get("123456789"))
        self.assertIsNone(store.get("bad"))
        self.assertIn("跳过无效群配置", store.load_warning)

    async def test_observed_names_flush_and_reload(self):
        store = await self.make_store()
        store.note_observed("123456789", "真实群名")
        self.assertFalse(self.path.exists())
        await store.flush_observed()
        self.assertTrue(self.path.exists())

        reloaded = await self.make_store()
        observed = reloaded.observed_snapshot()
        self.assertEqual(observed["123456789"]["group_name"], "真实群名")

    async def test_concurrent_upserts_do_not_corrupt_file(self):
        store = await self.make_store()
        configs = [
            default_group_config(str(100000 + index), reply_probability=0.1)
            for index in range(8)
        ]
        await asyncio.gather(*(store.upsert(config) for config in configs))
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["groups"]), 8)
        reloaded = await self.make_store()
        self.assertEqual(len(reloaded.snapshot()), 8)

    async def test_remove(self):
        store = await self.make_store()
        await store.upsert(default_group_config("111111"))
        self.assertTrue(await store.remove("111111"))
        self.assertFalse(await store.remove("111111"))
        reloaded = await self.make_store()
        self.assertIsNone(reloaded.get("111111"))

    async def test_created_at_is_preserved(self):
        store = await self.make_store()
        first = await store.upsert(default_group_config("111111"))
        second = await store.upsert(
            GroupConfig(
                group_id="111111",
                reply_probability=0.9,
                created_at="should-not-win",
                updated_at="",
            )
        )
        self.assertEqual(second.created_at, first.created_at)
        self.assertNotEqual(second.updated_at, "")


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_qq_group_quick_control.core import (  # noqa: E402
    GroupConfig,
    ValidationError,
    decide_active_reply,
    default_group_config,
    extract_group_id_from_whitelist_entry,
    infer_global_group_scope,
    parse_bool,
    parse_group_id,
    parse_probability,
    parse_remark,
    whitelist_allows,
)


class GroupIdTests(unittest.TestCase):
    def test_valid_numeric_ids(self):
        self.assertEqual(parse_group_id("123456789"), "123456789")
        self.assertEqual(parse_group_id(123456789), "123456789")
        self.assertEqual(parse_group_id(" 12345 "), "12345")

    def test_invalid_ids(self):
        for value in [
            "",
            None,
            True,
            "1234",
            "123456789012345678901",
            "12345a",
            "123 456",
            -12345,
        ]:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_group_id(value)


class RemarkTests(unittest.TestCase):
    def test_trim_and_allow_unicode(self):
        self.assertEqual(parse_remark("  主测试群 🙂  "), "主测试群 🙂")
        self.assertEqual(parse_remark(""), "")
        self.assertEqual(parse_remark(None), "")

    def test_length_and_control_chars(self):
        self.assertEqual(len(parse_remark("a" * 64)), 64)
        with self.assertRaises(ValidationError):
            parse_remark("a" * 65)
        for value in ["a\nb", "a\rb", "a\x00b", "a\tb", 123]:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_remark(value)


class ProbabilityTests(unittest.TestCase):
    def test_valid_probabilities(self):
        self.assertEqual(parse_probability(0), 0.0)
        self.assertEqual(parse_probability(1), 1.0)
        self.assertEqual(parse_probability("0.205"), 0.205)
        self.assertEqual(parse_probability(0.123456), 0.1235)

    def test_invalid_probabilities(self):
        for value in [-0.001, 1.001, True, None, "abc", math.nan, math.inf, -math.inf]:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_probability(value)


class BoolTests(unittest.TestCase):
    def test_strict_bool(self):
        self.assertIs(parse_bool(True, "x"), True)
        self.assertIs(parse_bool(False, "x"), False)
        for value in [1, 0, "true", "false", None]:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_bool(value, "x")


class GroupConfigTests(unittest.TestCase):
    def test_default_is_safe(self):
        config = default_group_config("123456")
        self.assertFalse(config.auto_reply_enabled)
        self.assertFalse(config.meme_enabled)
        self.assertTrue(config.respond_enabled)
        self.assertEqual(config.reply_probability, 0.2)

    def test_round_trip_and_validation(self):
        config = GroupConfig.from_dict(
            {
                "group_id": "123456789",
                "remark": "测试群",
                "auto_reply_enabled": True,
                "meme_enabled": False,
                "respond_enabled": True,
                "reply_probability": 0.35,
            }
        )
        payload = config.to_dict()
        again = GroupConfig.from_dict(payload)
        self.assertEqual(again, config)
        self.assertEqual(again.reply_probability, 0.35)

    def test_from_dict_rejects_wrong_types(self):
        with self.assertRaises(ValidationError):
            GroupConfig.from_dict(
                {
                    "group_id": "123456789",
                    "auto_reply_enabled": "yes",
                }
            )


class WhitelistTests(unittest.TestCase):
    def test_empty_whitelist_allows(self):
        self.assertTrue(
            whitelist_allows((), group_id="1", unified_msg_origin="p:GroupMessage:1")
        )

    def test_bare_id_and_umo(self):
        self.assertTrue(
            whitelist_allows(
                ("123456789",),
                group_id="123456789",
                unified_msg_origin="qq:GroupMessage:123456789",
            )
        )
        self.assertTrue(
            whitelist_allows(
                ("qq:GroupMessage:123456789",),
                group_id="123456789",
                unified_msg_origin="qq:GroupMessage:123456789",
            )
        )

    def test_unrelated_entry_blocks(self):
        self.assertFalse(
            whitelist_allows(
                ("999999",),
                group_id="123456789",
                unified_msg_origin="qq:GroupMessage:123456789",
            )
        )

    def test_extract_group_id(self):
        self.assertEqual(extract_group_id_from_whitelist_entry("123456789"), "123456789")
        self.assertEqual(
            extract_group_id_from_whitelist_entry("qq:GroupMessage:123456789"),
            "123456789",
        )
        self.assertEqual(
            extract_group_id_from_whitelist_entry("qq:FriendMessage:999999999"), ""
        )


class ActiveReplyDecisionTests(unittest.TestCase):
    def make_config(self, **overrides):
        base = {
            "group_id": "123456789",
            "auto_reply_enabled": True,
            "meme_enabled": True,
            "respond_enabled": True,
            "reply_probability": 0.5,
        }
        base.update(overrides)
        return GroupConfig(**base)

    def decide(self, config, **overrides):
        args = {
            "is_group_message": True,
            "is_at_or_wake_command": False,
            "group_id": config.group_id,
            "unified_msg_origin": f"qq:GroupMessage:{config.group_id}",
            "global_enabled": True,
            "global_method": "possibility_reply",
            "whitelist": (),
            "rng": lambda: 0.4,
        }
        args.update(overrides)
        return decide_active_reply(config, **args)

    def test_disabled_switches_short_circuit_without_draw(self):
        calls = []
        rng = lambda: calls.append(1) or 0.0  # noqa: E731
        self.assertFalse(
            self.decide(self.make_config(auto_reply_enabled=False), rng=rng)
        )
        self.assertFalse(
            self.decide(self.make_config(respond_enabled=False), rng=rng)
        )
        self.assertEqual(calls, [])

    def test_group_and_wake_gates(self):
        config = self.make_config()
        self.assertFalse(self.decide(config, is_group_message=False))
        self.assertFalse(self.decide(config, is_at_or_wake_command=True))

    def test_global_gates(self):
        config = self.make_config()
        self.assertFalse(self.decide(config, global_enabled=False))
        self.assertFalse(self.decide(config, global_method="keyword"))

    def test_whitelist_is_not_expanded(self):
        config = self.make_config()
        self.assertFalse(self.decide(config, whitelist=("999999",)))
        self.assertTrue(self.decide(config, whitelist=("123456789",)))

    def test_probability_boundaries_and_single_draw(self):
        config_zero = self.make_config(reply_probability=0)
        self.assertFalse(self.decide(config_zero))
        config_one = self.make_config(reply_probability=1)
        self.assertTrue(self.decide(config_one))

        calls = []

        def rng():
            calls.append(1)
            return 0.49

        config_half = self.make_config(reply_probability=0.5)
        self.assertTrue(self.decide(config_half, rng=rng))
        self.assertEqual(len(calls), 1)

        equal_calls = []

        def rng_equal():
            equal_calls.append(1)
            return 0.5

        self.assertFalse(self.decide(config_half, rng=rng_equal))
        self.assertEqual(len(equal_calls), 1)

    def test_different_groups_are_independent(self):
        a = self.make_config(group_id="111111", reply_probability=0.2)
        b = self.make_config(group_id="222222", reply_probability=0.5)
        c = self.make_config(group_id="333333", auto_reply_enabled=False)
        rng = lambda: 0.3  # noqa: E731
        self.assertFalse(self.decide(a, rng=rng))
        self.assertTrue(self.decide(b, rng=rng))
        self.assertFalse(self.decide(c, rng=rng))

    def test_inherited_scope_uses_global_probability(self):
        covered, probability = infer_global_group_scope(
            group_id="123456789",
            global_enabled=True,
            global_method="possibility_reply",
            whitelist=(),
            global_probability=0.37,
        )
        self.assertTrue(covered)
        self.assertEqual(probability, 0.37)

        covered, probability = infer_global_group_scope(
            group_id="123456789",
            global_enabled=False,
            global_method="possibility_reply",
            whitelist=(),
            global_probability=0.37,
        )
        self.assertFalse(covered)
        self.assertEqual(probability, 0.37)


if __name__ == "__main__":
    unittest.main()


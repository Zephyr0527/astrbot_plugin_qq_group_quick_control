"""Pure data models and decision helpers for QQ群聊一键操作.

This module deliberately has no AstrBot imports so that the validation and
decision rules can be unit-tested without a running AstrBot instance.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

PLUGIN_NAME = "astrbot_plugin_qq_group_quick_control"
DISPLAY_NAME = "QQ群聊一键操作"

GROUP_ID_MIN_LENGTH = 5
GROUP_ID_MAX_LENGTH = 20
REMARK_MAX_LENGTH = 64
DEFAULT_REPLY_PROBABILITY = 0.2
DEFAULT_AUTO_REPLY_ENABLED = False
DEFAULT_MEME_ENABLED = False
DEFAULT_RESPOND_ENABLED = True

_GROUP_ID_RE = re.compile(r"^\d{5,20}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class ValidationError(ValueError):
    """Raised when a value from the WebUI or data file is invalid."""


def utc_now_iso() -> str:
    """Return a stable UTC timestamp for persistence."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_group_id(value: Any) -> str:
    """Validate and normalize a QQ group id.

    Args:
        value: Raw value from JSON. Numeric ids are accepted for convenience.

    Returns:
        A string containing only digits.

    Raises:
        ValidationError: If the value is not a plausible QQ group id.
    """
    if isinstance(value, bool) or value is None:
        raise ValidationError("群号必须是 5-20 位数字")
    text = str(value).strip()
    if not text:
        raise ValidationError("群号不能为空")
    if not _GROUP_ID_RE.fullmatch(text):
        raise ValidationError("群号必须是 5-20 位数字")
    return text


def parse_remark(value: Any) -> str:
    """Validate and normalize a user-defined group remark."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError("群备注必须是文本")
    text = value.strip()
    if len(text) > REMARK_MAX_LENGTH:
        raise ValidationError(f"群备注不能超过 {REMARK_MAX_LENGTH} 个字符")
    if _CONTROL_CHARS_RE.search(text):
        raise ValidationError("群备注不能包含换行或控制字符")
    return text


def parse_probability(value: Any) -> float:
    """Validate a probability in the inclusive range 0..1."""
    if isinstance(value, bool) or value is None:
        raise ValidationError("主动回复概率必须在 0 到 1 之间")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("主动回复概率必须是数字") from exc
    if not math.isfinite(number):
        raise ValidationError("主动回复概率必须是有限数字")
    if number < 0 or number > 1:
        raise ValidationError("主动回复概率必须在 0 到 1 之间（0%～100%）")
    return round(number, 4)


def parse_bool(value: Any, field_name: str) -> bool:
    """Validate a JSON boolean without accepting truthy strings/numbers."""
    if type(value) is not bool:
        raise ValidationError(f"{field_name} 必须是布尔值 true 或 false")
    return value


@dataclass(frozen=True, slots=True)
class GroupConfig:
    """One group's independent configuration."""

    group_id: str
    remark: str = ""
    auto_reply_enabled: bool = DEFAULT_AUTO_REPLY_ENABLED
    meme_enabled: bool = DEFAULT_MEME_ENABLED
    respond_enabled: bool = DEFAULT_RESPOND_ENABLED
    reply_probability: float = DEFAULT_REPLY_PROBABILITY
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(
        cls,
        data: Any,
        *,
        fallback_group_id: str | None = None,
    ) -> "GroupConfig":
        """Build a config from persisted JSON with full validation."""
        if not isinstance(data, dict):
            raise ValidationError("群配置必须是对象")
        group_id = parse_group_id(data.get("group_id", fallback_group_id))
        return cls(
            group_id=group_id,
            remark=parse_remark(data.get("remark", "")),
            auto_reply_enabled=parse_bool(
                data.get("auto_reply_enabled", DEFAULT_AUTO_REPLY_ENABLED),
                "自动回复开关",
            ),
            meme_enabled=parse_bool(
                data.get("meme_enabled", DEFAULT_MEME_ENABLED),
                "表情包开关",
            ),
            respond_enabled=parse_bool(
                data.get("respond_enabled", DEFAULT_RESPOND_ENABLED),
                "回应开关",
            ),
            reply_probability=parse_probability(
                data.get("reply_probability", DEFAULT_REPLY_PROBABILITY)
            ),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the on-disk JSON shape."""
        return {
            "group_id": self.group_id,
            "remark": self.remark,
            "auto_reply_enabled": self.auto_reply_enabled,
            "meme_enabled": self.meme_enabled,
            "respond_enabled": self.respond_enabled,
            "reply_probability": self.reply_probability,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def with_timestamps(self, *, created_at: str, updated_at: str) -> "GroupConfig":
        """Return a copy with persistence timestamps set."""
        return replace(self, created_at=created_at, updated_at=updated_at)


def default_group_config(
    group_id: str,
    *,
    remark: str = "",
    auto_reply_enabled: bool = DEFAULT_AUTO_REPLY_ENABLED,
    meme_enabled: bool = DEFAULT_MEME_ENABLED,
    respond_enabled: bool = DEFAULT_RESPOND_ENABLED,
    reply_probability: float = DEFAULT_REPLY_PROBABILITY,
) -> GroupConfig:
    """Create a validated default config for a new group."""
    now = utc_now_iso()
    return GroupConfig(
        group_id=parse_group_id(group_id),
        remark=parse_remark(remark),
        auto_reply_enabled=parse_bool(auto_reply_enabled, "自动回复开关"),
        meme_enabled=parse_bool(meme_enabled, "表情包开关"),
        respond_enabled=parse_bool(respond_enabled, "回应开关"),
        reply_probability=parse_probability(reply_probability),
        created_at=now,
        updated_at=now,
    )


def normalize_global_whitelist(value: Any) -> tuple[str, ...]:
    """Normalize the AstrBot active-reply whitelist for read-only checks."""
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def whitelist_allows(
    whitelist: tuple[str, ...] | list[str],
    *,
    group_id: str,
    unified_msg_origin: str,
) -> bool:
    """Mirror AstrBot's active-reply whitelist check exactly.

    AstrBot accepts either the complete UMO or the bare group id. This helper
    intentionally keeps the same semantics so that enabling this plugin never
    widens the existing whitelist.
    """
    if not whitelist:
        return True
    return unified_msg_origin in whitelist or (
        bool(group_id) and group_id in whitelist
    )


def decide_active_reply(
    config: GroupConfig,
    *,
    is_group_message: bool,
    is_at_or_wake_command: bool,
    group_id: str,
    unified_msg_origin: str,
    global_enabled: bool,
    global_method: str,
    whitelist: tuple[str, ...] | list[str],
    rng: Callable[[], float] = random.random,
) -> bool:
    """Decide whether a configured group gets one active-reply attempt.

    The probability is evaluated at most once, and only after every cheaper
    gate has passed. Unconfigured groups never reach this helper and keep the
    original AstrBot behavior.
    """
    if not config.respond_enabled or not config.auto_reply_enabled:
        return False
    if not is_group_message or is_at_or_wake_command:
        return False
    if not global_enabled or global_method != "possibility_reply":
        return False
    if not whitelist_allows(
        whitelist,
        group_id=group_id,
        unified_msg_origin=unified_msg_origin,
    ):
        return False

    probability = config.reply_probability
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    return rng() < probability


def infer_global_group_scope(
    *,
    group_id: str,
    global_enabled: bool,
    global_method: str,
    whitelist: tuple[str, ...] | list[str],
    global_probability: float = DEFAULT_REPLY_PROBABILITY,
) -> tuple[bool, float]:
    """Describe the inherited active-reply state for page display.

    Returns:
        ``(covered, probability)`` where ``covered`` says this group would be
        eligible under the current global configuration.
    """
    probability = parse_probability(global_probability)
    if not global_enabled or global_method != "possibility_reply":
        return False, probability
    if whitelist and group_id not in whitelist:
        # A UMO entry can still cover this group. For display purposes, an
        # entry ending in ``:GroupMessage:<gid>`` is accepted; runtime checks
        # keep AstrBot's exact semantics.
        covered = any(
            str(entry).endswith(f":GroupMessage:{group_id}") for entry in whitelist
        )
    else:
        covered = True
    return covered, probability


def extract_group_id_from_whitelist_entry(entry: Any) -> str:
    """Extract a group id from a bare id or an AstrBot UMO, if possible."""
    text = str(entry or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    parts = text.split(":")
    if len(parts) >= 3 and parts[-2] == "GroupMessage" and parts[-1].isdigit():
        return parts[-1]
    return ""

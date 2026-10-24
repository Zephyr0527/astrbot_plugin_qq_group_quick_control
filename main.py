"""QQ群聊一键操作 - AstrBot group-level quick controls.

The plugin provides:
* a standalone WebUI page under ``pages/qq_group_quick_control/``;
* durable per-group settings in ``data/plugin_data/<plugin>/groups.json``;
* a narrow runtime bridge that lets configured groups override AstrBot's
  active-reply probability without changing unconfigured groups;
* a default-chat gate for the "respond" switch;
* compatibility gates for meme_manager and astrbot_plugin_stealer's automatic
  meme sending.

Only AstrBot's public plugin APIs are used for registration and Web exposure.
The active-reply bridge is a guarded runtime wrapper around the built-in
``GroupChatContext.need_active_reply`` because AstrBot v4.29.1 exposes no
plugin-level hook for the per-group active-reply probability.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from sys import maxsize
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request

from .core import (
    DEFAULT_AUTO_REPLY_ENABLED,
    DEFAULT_MEME_ENABLED,
    DEFAULT_REPLY_PROBABILITY,
    DEFAULT_RESPOND_ENABLED,
    DISPLAY_NAME,
    PLUGIN_NAME,
    REMARK_MAX_LENGTH,
    GroupConfig,
    ValidationError,
    decide_active_reply,
    default_group_config,
    extract_group_id_from_whitelist_entry,
    infer_global_group_scope,
    normalize_global_whitelist,
    parse_bool,
    parse_group_id,
    parse_probability,
    parse_remark,
    utc_now_iso,
)
from .store import GroupConfigStore

MEME_MANAGER_ORIGIN_KEY = "meme_manager_llm_request_origin"
MEME_DISABLED_ORIGIN = "qqgqc_disabled"
STEALER_PLUGIN_NAME = "astrbot_plugin_stealer"
MEME_MANAGER_PLUGIN_NAME = "meme_manager"
TESTED_MEME_MANAGER_VERSION = "5.2.5"
GROUP_LIST_CACHE_SECONDS = 20
OBSERVED_FLUSH_INTERVAL_SECONDS = 300


class QQGroupQuickControl(Star):
    """Group management page and runtime gates for onebot groups."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = GroupConfigStore(self.data_dir / "groups.json")
        self._group_cache: tuple[float, list[dict[str, Any]], list[str]] | None = None
        self._last_group_success: list[dict[str, Any]] = []
        self._group_lock = asyncio.Lock()
        self._last_observed_flush = 0.0
        self._active_reply_class: type | None = None
        self._active_reply_wrapper: Any = None
        self._active_reply_original: Any = None
        self._active_reply_available = False
        self._active_reply_error = ""
        self._stealer_plugin: Any = None
        self._stealer_gate: Any = None
        self._stealer_original: Any = None
        self._active = True
        self._initialized = False

    async def initialize(self) -> None:
        """Load persisted state and install guarded runtime bridges."""
        await self.store.load()
        if self.store.load_warning:
            logger.warning("群配置加载提示：%s", self.store.load_warning)
        self._register_web_apis()
        self._install_active_reply_hook()
        self._apply_stealer_gate()
        self._initialized = True
        logger.info(
            "QQ群聊一键操作已加载：%d 个群有自定义配置，数据目录 %s",
            len(self.store.snapshot()),
            self.data_dir,
        )

    async def terminate(self) -> None:
        """Persist the observed cache and remove only our own runtime wrappers."""
        self._active = False
        try:
            await self.store.flush_observed()
        except Exception:
            logger.exception("保存群名称缓存失败")
        self._restore_active_reply_hook()
        self._restore_stealer_gate()
        logger.info("QQ群聊一键操作已卸载")

    # ------------------------------------------------------------------
    # Web API
    # ------------------------------------------------------------------

    def _register_web_apis(self) -> None:
        routes = (
            ("groups", self._api_groups, ["GET"], "读取群列表与群级配置"),
            ("groups/save", self._api_save, ["POST"], "保存单个群的配置"),
            ("groups/add", self._api_add, ["POST"], "添加一个群到管理列表"),
            ("groups/reset", self._api_reset, ["POST"], "恢复单个群的默认配置"),
        )
        for route, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}",
                handler,
                methods,
                desc,
            )

    @staticmethod
    def _ok(data: Any) -> Any:
        return json_response({"status": "ok", "data": data})

    @staticmethod
    def _bad(message: str) -> Any:
        # Return HTTP 200 with a status envelope so the plugin page can show
        # the exact validation message instead of a generic axios 400 error.
        return json_response({"status": "error", "message": message})

    def _request_user(self) -> str:
        try:
            return str(getattr(request, "username", "") or "")
        except Exception:
            return ""

    def _require_read(self) -> Any | None:
        if not self._request_user():
            return error_response("需要先登录 AstrBot WebUI。", status_code=401)
        return None

    def _require_write(self) -> Any | None:
        user = self._request_user()
        if not user:
            return error_response("需要先登录 AstrBot WebUI。", status_code=401)
        if user.startswith("api_key:"):
            return error_response(
                "该操作仅允许 WebUI 登录管理员，API Key 无权修改群配置。",
                status_code=403,
            )
        return None

    async def _api_groups(self) -> Any:
        auth_error = self._require_read()
        if auth_error:
            return auth_error
        force = str(request.query.get("refresh", "")).lower() in {
            "1",
            "true",
            "yes",
        }
        onebot_groups, warnings = await self._get_group_list(force=force)
        try:
            await self.store.flush_observed()
        except Exception:
            logger.exception("保存群名称缓存失败")
            warnings.append("群名称缓存写入失败，但不影响现有群配置。")

        groups = self._build_group_views(onebot_groups)
        global_state = self._global_state()
        warnings.extend(self._runtime_warnings(global_state))
        return self._ok(
            {
                "groups": groups,
                "counts": {
                    "total": len(groups),
                    "configured": sum(1 for item in groups if item["configured"]),
                    "onebot": sum(
                        1 for item in groups if item["source"] == "onebot"
                    ),
                },
                "global": global_state,
                "adapters": self._adapter_status(),
                "defaults": {
                    "auto_reply_enabled": DEFAULT_AUTO_REPLY_ENABLED,
                    "meme_enabled": DEFAULT_MEME_ENABLED,
                    "respond_enabled": DEFAULT_RESPOND_ENABLED,
                    "reply_probability": DEFAULT_REPLY_PROBABILITY,
                    "remark_max_length": REMARK_MAX_LENGTH,
                },
                "can_write": not self._request_user().startswith("api_key:"),
                "warnings": warnings,
                "refreshed_at": utc_now_iso(),
            }
        )

    async def _api_save(self) -> Any:
        auth_error = self._require_write()
        if auth_error:
            return auth_error
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._bad("请求体必须是 JSON 对象。")
        try:
            group_id = parse_group_id(payload.get("group_id"))
            existing = self.store.get(group_id)
            base = existing or self._page_default_config(group_id)
            remark = (
                parse_remark(payload.get("remark"))
                if "remark" in payload
                else base.remark
            )
            auto_reply_enabled = (
                parse_bool(payload.get("auto_reply_enabled"), "自动回复开关")
                if "auto_reply_enabled" in payload
                else base.auto_reply_enabled
            )
            meme_enabled = (
                parse_bool(payload.get("meme_enabled"), "表情包开关")
                if "meme_enabled" in payload
                else base.meme_enabled
            )
            respond_enabled = (
                parse_bool(payload.get("respond_enabled"), "回应开关")
                if "respond_enabled" in payload
                else base.respond_enabled
            )
            reply_probability = (
                parse_probability(payload.get("reply_probability"))
                if "reply_probability" in payload
                else base.reply_probability
            )
            config = GroupConfig(
                group_id=group_id,
                remark=remark,
                auto_reply_enabled=auto_reply_enabled,
                meme_enabled=meme_enabled,
                respond_enabled=respond_enabled,
                reply_probability=reply_probability,
                created_at=base.created_at,
                updated_at=base.updated_at,
            )
            saved = await self.store.upsert(config)
        except ValidationError as exc:
            return self._bad(str(exc))
        except Exception:
            logger.exception("保存群配置失败")
            return self._bad("保存失败，请查看 AstrBot 日志。")

        view = self._build_group_view(
            saved.group_id,
            saved,
            source="config",
            group_name=self._cached_group_name(saved.group_id),
        )
        logger.info(
            "已保存群 %s 的配置：自动回复=%s 表情包=%s 回应=%s 概率=%.2f%%",
            saved.group_id,
            saved.auto_reply_enabled,
            saved.meme_enabled,
            saved.respond_enabled,
            saved.reply_probability * 100,
        )
        return self._ok(
            {
                "group": view,
                "message": "已保存，配置立即生效。",
            }
        )

    async def _api_add(self) -> Any:
        auth_error = self._require_write()
        if auth_error:
            return auth_error
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._bad("请求体必须是 JSON 对象。")
        try:
            group_id = parse_group_id(payload.get("group_id"))
            remark = parse_remark(payload.get("remark", ""))
        except ValidationError as exc:
            return self._bad(str(exc))

        existing = self.store.get(group_id)
        if existing:
            return self._ok(
                {
                    "group": self._build_group_view(
                        group_id,
                        existing,
                        source="config",
                        group_name=self._cached_group_name(group_id),
                    ),
                    "message": "该群已有自定义配置，未做修改。",
                }
            )

        onebot_groups, warnings = await self._get_group_list(force=True)
        known_ids = {item["group_id"] for item in onebot_groups}
        if known_ids and group_id not in known_ids:
            return self._bad(
                f"群 {group_id} 不在当前 OneBot 群列表中。"
                "请确认群号；如果机器人确实不在该群，无法添加。"
            )
        if not known_ids:
            warnings.append(
                "未能从 OneBot 确认群号，已按手动添加处理，请自行核对。"
            )

        inherited = self._inherited_state(group_id)
        try:
            config = default_group_config(
                group_id,
                remark=remark,
                auto_reply_enabled=inherited["auto_reply_enabled"],
                meme_enabled=False,
                respond_enabled=True,
                reply_probability=inherited["reply_probability"],
            )
            saved = await self.store.upsert(config)
        except ValidationError as exc:
            return self._bad(str(exc))
        except Exception:
            logger.exception("添加群配置失败")
            return self._bad("添加失败，请查看 AstrBot 日志。")

        view = self._build_group_view(
            group_id,
            saved,
            source="config",
            group_name=self._cached_group_name(group_id),
        )
        return self._ok(
            {
                "group": view,
                "warnings": warnings,
                "message": "已添加。新群默认关闭表情包，回应保持开启。",
            }
        )

    async def _api_reset(self) -> Any:
        auth_error = self._require_write()
        if auth_error:
            return auth_error
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return self._bad("请求体必须是 JSON 对象。")
        try:
            group_id = parse_group_id(payload.get("group_id"))
        except ValidationError as exc:
            return self._bad(str(exc))

        existing = self.store.get(group_id)
        if existing is None:
            return self._bad("该群还没有自定义配置，无需恢复。")
        try:
            config = default_group_config(
                group_id,
                remark=existing.remark,
                auto_reply_enabled=DEFAULT_AUTO_REPLY_ENABLED,
                meme_enabled=DEFAULT_MEME_ENABLED,
                respond_enabled=DEFAULT_RESPOND_ENABLED,
                reply_probability=DEFAULT_REPLY_PROBABILITY,
            )
            saved = await self.store.upsert(config)
        except Exception:
            logger.exception("恢复群默认配置失败")
            return self._bad("恢复默认配置失败，请查看 AstrBot 日志。")

        view = self._build_group_view(
            group_id,
            saved,
            source="config",
            group_name=self._cached_group_name(group_id),
        )
        return self._ok(
            {
                "group": view,
                "message": (
                    "已恢复默认：关闭主动回复与表情包，保留回应，"
                    "主动回复概率 20%。群备注已保留。"
                ),
            }
        )

    # ------------------------------------------------------------------
    # Group list and page view
    # ------------------------------------------------------------------

    async def _get_group_list(
        self, *, force: bool = False
    ) -> tuple[list[dict[str, Any]], list[str]]:
        now = time.monotonic()
        if (
            not force
            and self._group_cache is not None
            and now - self._group_cache[0] < GROUP_LIST_CACHE_SECONDS
        ):
            return self._group_cache[1], list(self._group_cache[2])

        async with self._group_lock:
            now = time.monotonic()
            if (
                not force
                and self._group_cache is not None
                and now - self._group_cache[0] < GROUP_LIST_CACHE_SECONDS
            ):
                return self._group_cache[1], list(self._group_cache[2])

            groups: list[dict[str, Any]] = []
            warnings: list[str] = []
            seen: dict[str, dict[str, Any]] = {}
            platform_count = 0
            try:
                platforms = list(self.context.platform_manager.platform_insts)
            except Exception:
                platforms = []
            for platform in platforms:
                try:
                    meta = platform.meta()
                except Exception:
                    continue
                if getattr(meta, "name", "") != "aiocqhttp":
                    continue
                platform_count += 1
                platform_id = getattr(meta, "id", "") or "aiocqhttp"
                get_client = getattr(platform, "get_client", None)
                if not callable(get_client):
                    warnings.append(f"平台 {platform_id} 不支持获取群列表。")
                    continue
                try:
                    client = get_client()
                    action = getattr(client, "call_action", None)
                    if callable(action):
                        result = await asyncio.wait_for(
                            action("get_group_list"), timeout=8
                        )
                    else:
                        getter = getattr(client, "get_group_list", None)
                        if not callable(getter):
                            warnings.append(
                                f"平台 {platform_id} 的 OneBot 客户端不支持 get_group_list。"
                            )
                            continue
                        result = await asyncio.wait_for(getter(), timeout=8)
                except Exception as exc:
                    warnings.append(
                        f"从平台 {platform_id} 获取群列表失败："
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue

                items = result.get("data") if isinstance(result, dict) else result
                if not isinstance(items, list):
                    warnings.append(
                        f"平台 {platform_id} 返回了无法识别的群列表格式。"
                    )
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        group_id = parse_group_id(item.get("group_id"))
                    except ValidationError:
                        continue
                    name = str(item.get("group_name") or "").strip()
                    try:
                        member_count = int(item.get("member_count") or 0)
                    except (TypeError, ValueError):
                        member_count = 0
                    previous = seen.get(group_id)
                    if previous is not None:
                        if not previous.get("group_name") and name:
                            previous["group_name"] = name
                        if member_count > int(previous.get("member_count") or 0):
                            previous["member_count"] = member_count
                        continue
                    record = {
                        "group_id": group_id,
                        "group_name": name,
                        "member_count": member_count,
                        "platform_id": platform_id,
                    }
                    seen[group_id] = record
                    groups.append(record)

            if platform_count == 0:
                warnings.append(
                    "未检测到 aiocqhttp 平台，无法自动获取群列表；"
                    "可在页面中手动输入群号添加管理对象。"
                )
            if not groups and warnings and self._last_group_success:
                groups = list(self._last_group_success)
                warnings.append("本次未获取到群列表，已使用上次成功获取的缓存。")
            if groups:
                self._last_group_success = list(groups)

            groups.sort(
                key=lambda item: (
                    0,
                    int(item["group_id"]),
                )
                if str(item["group_id"]).isdigit()
                else (1, item["group_id"])
            )
            self._group_cache = (time.monotonic(), list(groups), list(warnings))
            return groups, warnings

    def _cached_group_name(self, group_id: str) -> str:
        observed = self.store.observed_snapshot().get(group_id, {})
        name = str(observed.get("group_name") or "").strip()
        if name:
            return name
        if self._group_cache:
            for item in self._group_cache[1]:
                if item["group_id"] == group_id:
                    return str(item.get("group_name") or "")
        for item in self._last_group_success:
            if item["group_id"] == group_id:
                return str(item.get("group_name") or "")
        return ""

    def _build_group_views(
        self, onebot_groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        configs = self.store.snapshot()
        observed = self.store.observed_snapshot()
        onebot_map = {item["group_id"]: item for item in onebot_groups}
        global_state = self._global_state()

        ids: set[str] = set(configs)
        ids.update(onebot_map)
        ids.update(observed)
        for entry in global_state["whitelist"]:
            group_id = extract_group_id_from_whitelist_entry(entry)
            if group_id:
                ids.add(group_id)

        views: list[dict[str, Any]] = []
        for group_id in ids:
            if group_id in onebot_map:
                source = "onebot"
            elif group_id in configs:
                source = "config"
            elif group_id in observed:
                source = "observed"
            else:
                source = "active_reply_whitelist"
            onebot = onebot_map.get(group_id, {})
            group_name = str(
                onebot.get("group_name")
                or observed.get(group_id, {}).get("group_name")
                or ""
            ).strip()
            member_count = int(onebot.get("member_count") or 0)
            views.append(
                self._build_group_view(
                    group_id,
                    configs.get(group_id),
                    source=source,
                    group_name=group_name,
                    member_count=member_count,
                )
            )

        views.sort(
            key=lambda item: (
                0,
                int(item["group_id"]),
            )
            if str(item["group_id"]).isdigit()
            else (1, item["group_id"])
        )
        return views

    def _build_group_view(
        self,
        group_id: str,
        config: GroupConfig | None,
        *,
        source: str,
        group_name: str = "",
        member_count: int = 0,
    ) -> dict[str, Any]:
        global_state = self._global_state()
        inherited = self._inherited_state(group_id)
        if config is None:
            auto_reply_enabled = bool(inherited["auto_reply_enabled"])
            meme_enabled = True
            respond_enabled = True
            reply_probability = float(inherited["reply_probability"])
            remark = ""
        else:
            auto_reply_enabled = config.auto_reply_enabled
            meme_enabled = config.meme_enabled
            respond_enabled = config.respond_enabled
            reply_probability = config.reply_probability
            remark = config.remark

        effective_auto = bool(auto_reply_enabled)
        if not global_state["active_reply_enabled"] or (
            global_state["method"] != "possibility_reply"
        ):
            effective_auto = False
        elif global_state["whitelist"]:
            whitelist = [str(item) for item in global_state["whitelist"]]
            effective_auto = bool(
                group_id in whitelist
                or any(
                    entry.endswith(f":GroupMessage:{group_id}")
                    for entry in whitelist
                )
            )

        return {
            "group_id": group_id,
            "group_name": group_name,
            "member_count": member_count,
            "remark": remark,
            "source": source,
            "configured": config is not None,
            "auto_reply_enabled": auto_reply_enabled,
            "meme_enabled": meme_enabled,
            "respond_enabled": respond_enabled,
            "reply_probability": reply_probability,
            "probability_percent": round(reply_probability * 100, 1),
            "effective": {
                "auto_reply": effective_auto,
                "meme_suppressed": bool(config is not None and not meme_enabled),
                "respond_suppressed": bool(
                    config is not None and not respond_enabled
                ),
            },
            "inherited": {
                "auto_reply_enabled": bool(inherited["auto_reply_enabled"]),
                "reply_probability": float(inherited["reply_probability"]),
            },
            "created_at": config.created_at if config else "",
            "updated_at": config.updated_at if config else "",
        }

    def _page_default_config(self, group_id: str) -> GroupConfig:
        """Values shown before a group has ever been saved."""
        inherited = self._inherited_state(group_id)
        return GroupConfig(
            group_id=group_id,
            remark="",
            auto_reply_enabled=bool(inherited["auto_reply_enabled"]),
            # ``True`` means "do not restrict the existing meme plugins".
            meme_enabled=True,
            respond_enabled=True,
            reply_probability=float(inherited["reply_probability"]),
        )

    # ------------------------------------------------------------------
    # Global AstrBot config helpers
    # ------------------------------------------------------------------

    def _global_state(self) -> dict[str, Any]:
        try:
            cfg = self.context.get_config()
        except Exception:
            cfg = {}
        provider_ltm = cfg.get("provider_ltm_settings", {}) if hasattr(cfg, "get") else {}
        active_reply = (
            provider_ltm.get("active_reply", {})
            if isinstance(provider_ltm, dict)
            else {}
        )
        platform_settings = cfg.get("platform_settings", {}) if hasattr(cfg, "get") else {}
        whitelist = list(normalize_global_whitelist(active_reply.get("whitelist", [])))
        try:
            probability = parse_probability(
                active_reply.get("possibility_reply", DEFAULT_REPLY_PROBABILITY)
            )
        except ValidationError:
            probability = DEFAULT_REPLY_PROBABILITY
        return {
            "active_reply_enabled": bool(active_reply.get("enable", False)),
            "method": str(active_reply.get("method") or "possibility_reply"),
            "possibility_reply": probability,
            "whitelist": whitelist,
            "whitelist_count": len(whitelist),
            "id_whitelist_enabled": bool(
                platform_settings.get("enable_id_white_list", False)
            )
            if isinstance(platform_settings, dict)
            else False,
        }

    def _inherited_state(self, group_id: str) -> dict[str, Any]:
        state = self._global_state()
        covered, probability = infer_global_group_scope(
            group_id=group_id,
            global_enabled=state["active_reply_enabled"],
            global_method=state["method"],
            whitelist=state["whitelist"],
            global_probability=state["possibility_reply"],
        )
        return {
            "auto_reply_enabled": bool(covered),
            "reply_probability": float(probability),
        }

    def _runtime_warnings(self, global_state: dict[str, Any]) -> list[str]:
        warnings: list[str] = []
        if not global_state["active_reply_enabled"]:
            warnings.append(
                "AstrBot 全局「主动回复」总开关当前关闭；各群即使开启自动回复，"
                "也不会触发主动回复。"
            )
        if global_state["method"] != "possibility_reply":
            warnings.append(
                "AstrBot 全局主动回复方式不是「按概率回复」；"
                "本插件的群级概率不会生效。"
            )
        if not self._active_reply_available:
            warnings.append(
                "当前 AstrBot 版本未找到主动回复接管点，群级自动回复/概率不会生效。"
            )
        adapters = self._adapter_status()
        if not adapters["meme_manager"]["detected"] and not adapters["stealer"][
            "detected"
        ]:
            warnings.append(
                "未检测到 meme_manager 或 stealer；表情包开关不会影响其他图片功能。"
            )
        if adapters["meme_manager"]["detected"] and adapters["meme_manager"][
            "version"
        ] not in {"", TESTED_MEME_MANAGER_VERSION}:
            warnings.append(
                "meme_manager 版本为 "
                f"{adapters['meme_manager']['version']}，本插件按 "
                f"{TESTED_MEME_MANAGER_VERSION} 的事件字段适配；如表情包开关不生效，"
                "请查看已知限制。"
            )
        return warnings

    def _adapter_status(self) -> dict[str, Any]:
        meme = self._find_plugin(MEME_MANAGER_PLUGIN_NAME)
        stealer = self._find_plugin(STEALER_PLUGIN_NAME)
        gate_active = bool(
            self._stealer_gate is not None
            and self._stealer_plugin is not None
            and getattr(
                self._stealer_plugin, "is_send_enabled_for_event", None
            )
            is self._stealer_gate
        )
        return {
            "meme_manager": {
                "detected": meme is not None,
                "version": str(getattr(meme, "version", "") or ""),
                "mode": "llm_request_origin",
                "tested_version": TESTED_MEME_MANAGER_VERSION,
            },
            "stealer": {
                "detected": stealer is not None,
                "version": str(getattr(stealer, "version", "") or ""),
                "mode": "runtime_send_gate",
                "gate_active": gate_active,
            },
        }

    def _find_plugin(self, name: str) -> Any | None:
        try:
            for star in self.context.get_all_stars():
                if getattr(star, "name", None) == name:
                    return star
        except Exception:
            return None
        return None

    # ------------------------------------------------------------------
    # Active reply runtime bridge
    # ------------------------------------------------------------------

    def _install_active_reply_hook(self) -> None:
        try:
            from astrbot.builtin_stars.astrbot.group_chat_context import (
                GroupChatContext,
            )
        except Exception as exc:
            self._active_reply_available = False
            self._active_reply_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "未找到 AstrBot 主动回复上下文类，群级概率不可用：%s",
                self._active_reply_error,
            )
            return

        current = GroupChatContext.need_active_reply
        original = current
        if getattr(current, "_qqgqc_wrapper", False):
            original = getattr(current, "_qqgqc_original", current)

        async def wrapper(context_obj: Any, event: AstrMessageEvent) -> bool:
            if not self._active:
                return await original(context_obj, event)
            group_id = str(event.get_group_id() or "").strip()
            config = self.store.get(group_id) if group_id else None
            if config is None:
                return await original(context_obj, event)

            # Cheap gates first: no model call, no random draw.
            if not config.respond_enabled or not config.auto_reply_enabled:
                return False
            try:
                settings = context_obj.cfg(event)
            except Exception:
                logger.exception("读取全局主动回复配置失败，回退到 AstrBot 原逻辑")
                return await original(context_obj, event)

            decided = decide_active_reply(
                config,
                is_group_message=(
                    event.get_message_type() == MessageType.GROUP_MESSAGE
                ),
                is_at_or_wake_command=bool(
                    getattr(event, "is_at_or_wake_command", False)
                ),
                group_id=group_id,
                unified_msg_origin=event.unified_msg_origin,
                global_enabled=bool(settings.get("enable_active_reply", False)),
                global_method=str(settings.get("ar_method") or ""),
                whitelist=normalize_global_whitelist(
                    settings.get("ar_whitelist", [])
                ),
            )
            if decided:
                event.set_extra("qqgqc_active_reply", True)
            return decided

        wrapper._qqgqc_wrapper = True
        wrapper._qqgqc_original = original
        wrapper._qqgqc_owner = self
        GroupChatContext.need_active_reply = wrapper
        self._active_reply_class = GroupChatContext
        self._active_reply_wrapper = wrapper
        self._active_reply_original = original
        self._active_reply_available = True
        self._active_reply_error = ""
        logger.info(
            "已安装群级主动回复接管（AstrBot GroupChatContext.need_active_reply）"
        )

    def _restore_active_reply_hook(self) -> None:
        cls = self._active_reply_class
        wrapper = self._active_reply_wrapper
        if cls is None or wrapper is None:
            return
        try:
            if getattr(cls, "need_active_reply", None) is wrapper:
                cls.need_active_reply = self._active_reply_original
                logger.info("已恢复 AstrBot 原有主动回复逻辑")
        except Exception:
            logger.exception("恢复主动回复逻辑失败")
        finally:
            self._active_reply_class = None
            self._active_reply_wrapper = None
            self._active_reply_original = None

    # ------------------------------------------------------------------
    # Meme sending gates
    # ------------------------------------------------------------------

    def _event_group_config(self, event: AstrMessageEvent) -> GroupConfig | None:
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return None
        return self.store.get(group_id)

    @filter.on_waiting_llm_request(priority=-100_000)
    async def gate_waiting_llm(self, event: AstrMessageEvent) -> None:
        """Stop default chat for disabled groups and mark meme_manager origin."""
        config = self._event_group_config(event)
        if config is None:
            return
        if not config.respond_enabled and (
            event.get_extra("qqgqc_active_reply")
            or event.get_extra("provider_request") is None
        ):
            event.set_extra("qqgqc_respond_blocked", True)
            event.stop_event()
            logger.debug("群 %s 已关闭回应，拦截默认 LLM 请求", config.group_id)
            return
        if not config.meme_enabled:
            event.set_extra(MEME_MANAGER_ORIGIN_KEY, MEME_DISABLED_ORIGIN)

    @filter.on_llm_request(priority=100_000)
    async def mark_meme_disabled_before_manager(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Run before meme_manager's own on_llm_request hook."""
        config = self._event_group_config(event)
        if config is not None and not config.meme_enabled:
            event.set_extra(MEME_MANAGER_ORIGIN_KEY, MEME_DISABLED_ORIGIN)

    @filter.on_llm_request(priority=-100_000)
    async def block_default_llm(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """Backstop for default-chat requests that bypassed on_waiting."""
        config = self._event_group_config(event)
        if (
            config is not None
            and not config.respond_enabled
            and (
                event.get_extra("qqgqc_active_reply")
                or event.get_extra("provider_request") is None
            )
        ):
            event.set_extra("qqgqc_respond_blocked", True)
            event.stop_event()
            logger.debug("群 %s 已关闭回应，在 LLM 请求阶段再次拦截", config.group_id)

    def _apply_stealer_gate(self) -> None:
        """Gate stealer's automatic meme sending per group at runtime.

        stealer v3.1.6 exposes ``is_send_enabled_for_event`` and uses it before
        scheming an automatic meme. We wrap that single predicate instead of
        editing stealer's config, disabling its plugin, or touching its data.
        """
        star = self._find_plugin(STEALER_PLUGIN_NAME)
        if star is None or getattr(star, "star_cls", None) is None:
            return
        plugin = star.star_cls
        if getattr(plugin, "_qqgqc_send_gate_owner", None) is self:
            return
        current = getattr(plugin, "is_send_enabled_for_event", None)
        if not callable(current):
            return
        if getattr(current, "_qqgqc_gate", False):
            current = getattr(current, "_qqgqc_original", current)

        def gated(event: AstrMessageEvent) -> bool:
            try:
                config = self._event_group_config(event)
                if config is not None and not config.meme_enabled:
                    return False
            except Exception:
                logger.exception("检查表情包群级开关失败，沿用 stealer 原逻辑")
            try:
                result = current(event)
                if inspect.isawaitable(result):
                    logger.warning(
                        "stealer 的发送开关返回了异步结果，无法在同步调用中等待；"
                        "本轮按允许处理。"
                    )
                    return True
                return bool(result)
            except Exception:
                logger.exception("调用 stealer 发送开关失败，本轮按允许处理")
                return True

        gated._qqgqc_gate = True
        gated._qqgqc_original = current
        plugin.is_send_enabled_for_event = gated
        plugin._qqgqc_send_gate_owner = self
        self._stealer_plugin = plugin
        self._stealer_gate = gated
        self._stealer_original = current
        logger.info("已安装 stealer 群级表情包发送开关")

    def _restore_stealer_gate(self) -> None:
        plugin = self._stealer_plugin
        gate = self._stealer_gate
        if plugin is None or gate is None:
            return
        try:
            if getattr(plugin, "is_send_enabled_for_event", None) is gate:
                plugin.is_send_enabled_for_event = self._stealer_original
                if getattr(plugin, "_qqgqc_send_gate_owner", None) is self:
                    try:
                        delattr(plugin, "_qqgqc_send_gate_owner")
                    except AttributeError:
                        pass
                logger.info("已恢复 stealer 原有发送开关")
        except Exception:
            logger.exception("恢复 stealer 发送开关失败")
        finally:
            self._stealer_plugin = None
            self._stealer_gate = None
            self._stealer_original = None

    # ------------------------------------------------------------------
    # Message pipeline hooks
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
    async def suppress_default_chat(self, event: AstrMessageEvent) -> None:
        """Disable AstrBot's default LLM chat for groups with respond off.

        This does not stop the event, so commands, other plugins' fixed
        responses, and admin operations continue to work. It only forbids the
        implicit default chat request.
        """
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            config = self._event_group_config(event)
            if config is None or config.respond_enabled:
                return
            event.should_call_llm(True)
            event.set_extra("qqgqc_respond_blocked", True)
            logger.debug(
                "群 %s 已关闭回应，禁用默认 LLM 聊天入口", config.group_id
            )
        except Exception:
            logger.exception("执行群级回应开关失败")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-maxsize)
    async def remember_group_name(self, event: AstrMessageEvent) -> None:
        """Cache the real group name for display, never as the user remark."""
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            group_id = str(event.get_group_id() or "").strip()
            if not group_id:
                return
            group = getattr(event.message_obj, "group", None)
            name = str(getattr(group, "group_name", "") or "").strip()
            self.store.note_observed(group_id, name)
            if (
                time.monotonic() - self._last_observed_flush
                > OBSERVED_FLUSH_INTERVAL_SECONDS
            ):
                self._last_observed_flush = time.monotonic()
                task = asyncio.create_task(self.store.flush_observed())
                task.add_done_callback(self._log_flush_error)
        except Exception:
            logger.debug("缓存群名称失败", exc_info=True)

    @staticmethod
    def _log_flush_error(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception:
            logger.exception("保存群名称缓存的后台任务失败")

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata: Any) -> None:
        """Install the stealer gate if stealer is loaded after this plugin."""
        if getattr(metadata, "name", None) == STEALER_PLUGIN_NAME:
            self._apply_stealer_gate()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """Final fallback for plugins loaded before this plugin."""
        self._apply_stealer_gate()

"""Durable, atomic storage for per-group settings.

The store keeps a validated in-memory snapshot for the hot message path and
persists changes through a temporary file + ``os.replace`` to avoid partially
written JSON files.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .core import (
    GroupConfig,
    ValidationError,
    default_group_config,
    parse_group_id,
    utc_now_iso,
)

SCHEMA_VERSION = 1


class GroupConfigStore:
    """Thread-safe (asyncio) store for group configs and observed group names."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self._groups: dict[str, GroupConfig] = {}
        self._observed: dict[str, dict[str, Any]] = {}
        self._observed_dirty = False
        self._lock = asyncio.Lock()
        self._load_warning = ""
        self._loaded = False

    @property
    def load_warning(self) -> str:
        """Return a human-readable warning if loading recovered from damage."""
        return self._load_warning

    async def load(self) -> None:
        """Load settings from disk.

        A corrupt primary file falls back to ``groups.json.bak``. If both are
        unreadable, the plugin starts with an empty configuration, which means
        every group keeps AstrBot's original behavior rather than expanding it.
        """
        async with self._lock:
            self._load_sync()
            self._loaded = True

    def _load_sync(self) -> None:
        self._groups.clear()
        self._observed.clear()
        self._observed_dirty = False
        self._load_warning = ""

        if not self.path.exists():
            return

        payload, warning = self._read_payload(self.path)
        if payload is None:
            # Preserve the damaged file for manual recovery, then try backup.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
            try:
                os.replace(self.path, corrupt)
                warning += f" 原文件已保留为 {corrupt.name}。"
            except OSError:
                pass
            if self.backup_path.exists():
                backup_payload, backup_warning = self._read_payload(self.backup_path)
                if backup_payload is not None:
                    payload = backup_payload
                    warning += " 已从备份恢复。"
                else:
                    warning += f" 备份也不可读：{backup_warning}"
            self._load_warning = warning.strip()

        if payload is None:
            return

        raw_groups = payload.get("groups", {})
        if isinstance(raw_groups, list):
            # Tolerate an early hand-written list format.
            iterable = []
            for item in raw_groups:
                if isinstance(item, dict) and item.get("group_id") is not None:
                    iterable.append((str(item["group_id"]), item))
        elif isinstance(raw_groups, dict):
            iterable = list(raw_groups.items())
        else:
            iterable = []

        for key, item in iterable:
            try:
                config = GroupConfig.from_dict(item, fallback_group_id=str(key))
                self._groups[config.group_id] = config
            except (ValidationError, TypeError, ValueError) as exc:
                self._load_warning = (
                    f"{self._load_warning} 已跳过无效群配置 {key}: {exc}".strip()
                )

        raw_observed = payload.get("observed", {})
        if isinstance(raw_observed, dict):
            for key, item in raw_observed.items():
                try:
                    group_id = parse_group_id(key)
                except ValidationError:
                    continue
                if not isinstance(item, dict):
                    continue
                name = str(item.get("group_name") or "").strip()
                last_seen = str(item.get("last_seen") or "")
                if name or last_seen:
                    self._observed[group_id] = {
                        "group_name": name,
                        "last_seen": last_seen,
                    }

    @staticmethod
    def _read_payload(path: Path) -> tuple[dict[str, Any] | None, str]:
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except Exception as exc:  # noqa: BLE001 - report any damage to the user
            return None, f"读取 {path.name} 失败：{type(exc).__name__}: {exc}"
        if not isinstance(payload, dict):
            return None, f"{path.name} 的顶层必须是 JSON 对象。"
        return payload, ""

    def get(self, group_id: str) -> GroupConfig | None:
        """Return a config without touching the disk (hot path)."""
        return self._groups.get(str(group_id).strip())

    def snapshot(self) -> dict[str, GroupConfig]:
        """Return a shallow copy of all configs."""
        return dict(self._groups)

    def observed_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a copy of cached observed group names."""
        return {key: dict(value) for key, value in self._observed.items()}

    def note_observed(self, group_id: str, group_name: str = "") -> None:
        """Remember a group seen in the message stream.

        This is deliberately cheap: it only updates memory and marks the file
        dirty. The observed cache is flushed when the management page is
        opened, on config writes, and when the plugin is unloaded.
        """
        try:
            normalized = parse_group_id(group_id)
        except ValidationError:
            return
        name = str(group_name or "").strip()
        current = self._observed.get(normalized)
        if current and current.get("group_name") == name:
            return
        self._observed[normalized] = {
            "group_name": name,
            "last_seen": utc_now_iso(),
        }
        self._observed_dirty = True

    async def flush_observed(self) -> None:
        """Persist the observed-name cache if it changed."""
        async with self._lock:
            if not self._observed_dirty:
                return
            await self._persist_locked()
            self._observed_dirty = False

    async def upsert(self, config: GroupConfig) -> GroupConfig:
        """Insert or replace one group config and persist atomically."""
        async with self._lock:
            now = utc_now_iso()
            old = self._groups.get(config.group_id)
            created_at = old.created_at if old and old.created_at else now
            stored = config.with_timestamps(created_at=created_at, updated_at=now)
            self._groups[config.group_id] = stored
            try:
                await self._persist_locked()
            except Exception:
                if old is None:
                    self._groups.pop(config.group_id, None)
                else:
                    self._groups[config.group_id] = old
                raise
            self._observed_dirty = False
            return stored

    async def remove(self, group_id: str) -> bool:
        """Remove a custom config (used only by tests/maintenance)."""
        async with self._lock:
            old = self._groups.pop(str(group_id).strip(), None)
            if old is None:
                return False
            try:
                await self._persist_locked()
            except Exception:
                self._groups[old.group_id] = old
                raise
            return True

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": utc_now_iso(),
            "groups": {
                group_id: config.to_dict()
                for group_id, config in sorted(self._groups.items())
            },
            "observed": {
                group_id: dict(value)
                for group_id, value in sorted(self._observed.items())
            },
        }

    async def _persist_locked(self) -> None:
        payload = self._payload()
        await asyncio.to_thread(self._write_atomic, payload)

    def _write_atomic(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists() and self.path.stat().st_size > 0:
                try:
                    shutil.copy2(self.path, self.backup_path)
                except OSError:
                    # A failed backup must not block the new valid config.
                    pass
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                try:
                    os.remove(temp_name)
                except OSError:
                    pass


def make_default_config_for_new_group(
    group_id: str,
    *,
    auto_reply_enabled: bool = False,
    reply_probability: float = 0.2,
    remark: str = "",
) -> GroupConfig:
    """Convenience wrapper used by the Web API and tests."""
    return default_group_config(
        group_id,
        remark=remark,
        auto_reply_enabled=auto_reply_enabled,
        meme_enabled=False,
        respond_enabled=True,
        reply_probability=reply_probability,
    )

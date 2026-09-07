# -*- coding: utf-8 -*-
"""Process-local polling for shared Tool Guard configuration."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ...config.config import ToolGuardConfig
from ...config.utils import get_config_path

POLL_INTERVAL_SECONDS = 30.0


def _file_fingerprint(path: Path) -> tuple[int, int, int] | None:
    """Return a robust stat fingerprint, or ``None`` when absent."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _load_tool_guard_config(path: Path) -> ToolGuardConfig | None:
    """Load and validate the Tool Guard section from a config file."""
    try:
        with path.open(encoding="utf-8") as file:
            data: Any = json.load(file)
        if not isinstance(data, dict):
            return None
        security = data.get("security", {})
        if not isinstance(security, dict):
            return None
        tool_guard = security.get("tool_guard", {})
        if not isinstance(tool_guard, dict):
            return None
        return ToolGuardConfig.model_validate(tool_guard)
    except (OSError, ValueError, TypeError):
        return None


class ToolGuardConfigWatcher:
    """Reload the process-local Tool Guard engine from a shared file."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._config_path = Path(config_path or get_config_path())
        self._poll_interval = poll_interval
        self._last_fingerprint: tuple[int, int, int] | None = None
        self._task: asyncio.Task[None] | None = None
        self._engine: Any = None

    @property
    def last_fingerprint(self) -> tuple[int, int, int] | None:
        """Return the last successfully observed configuration fingerprint."""
        return self._last_fingerprint

    async def start(self, engine: Any = None) -> None:
        """Take an initial snapshot and start the 30-second poll loop."""
        if self._task is not None:
            return
        if engine is None:
            from .engine import get_guard_engine

            engine = get_guard_engine()
        self._engine = engine
        self._last_fingerprint = _file_fingerprint(self._config_path)
        self._task = asyncio.create_task(
            self._poll_loop(),
            name="tool_guard_config_watcher",
        )

    async def stop(self) -> None:
        """Stop the polling task."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._check_once()
            except Exception:
                # A single failed poll must not terminate future retries.
                continue

    async def _check_once(self) -> bool:
        fingerprint = _file_fingerprint(self._config_path)
        if fingerprint is None or fingerprint == self._last_fingerprint:
            return False
        tool_guard_config = _load_tool_guard_config(self._config_path)
        if tool_guard_config is None:
            return False
        try:
            self._engine.reload_rules()
        except Exception:
            return False
        # Keep the engine's documented precedence: an explicit environment
        # override wins over the file value on every reload.
        from .engine import _guard_enabled

        self._engine.enabled = _guard_enabled()
        self._last_fingerprint = fingerprint
        return True

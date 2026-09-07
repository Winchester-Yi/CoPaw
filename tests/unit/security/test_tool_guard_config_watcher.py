# -*- coding: utf-8 -*-
"""Tests for shared Tool Guard configuration propagation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from swe.config.config import Config, SecurityConfig, ToolGuardConfig
from swe.config.utils import save_config
from swe.security.tool_guard.watcher import ToolGuardConfigWatcher


class _Engine:
    def __init__(self, *, fail: bool = False) -> None:
        self.reload_count = 0
        self.fail = fail
        self.enabled = True

    def reload_rules(self) -> None:
        self.reload_count += 1
        if self.fail:
            raise RuntimeError("reload failed")


def _config(
    enabled: bool,
    disabled_rules: list[str] | None = None,
) -> Config:
    return Config(
        security=SecurityConfig(
            tool_guard=ToolGuardConfig(
                enabled=enabled,
                disabled_rules=disabled_rules or [],
            ),
        ),
    )


def test_save_config_replaces_file_atomically(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    save_config(_config(True), config_path)
    first_inode = config_path.stat().st_ino

    save_config(_config(False), config_path)

    assert (
        json.loads(config_path.read_text(encoding="utf-8"))["security"][
            "tool_guard"
        ]["enabled"]
        is False
    )
    assert config_path.stat().st_ino != first_inode
    assert list(tmp_path.glob(".config.json.*.tmp")) == []


@pytest.mark.asyncio
async def test_watcher_reloads_changed_valid_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    save_config(_config(True), config_path)
    engine = _Engine()
    watcher = ToolGuardConfigWatcher(config_path=config_path)
    await watcher.start(engine)

    await asyncio.sleep(0)
    save_config(_config(True, ["RULE_A"]), config_path)

    assert await watcher._check_once() is True
    assert engine.reload_count == 1
    assert engine.enabled is True

    save_config(_config(True, ["RULE_B"]), config_path)
    assert await watcher._check_once() is True
    assert engine.reload_count == 2
    assert engine.enabled is True
    await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_respects_enabled_environment_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_config(True), config_path)
    engine = _Engine()
    watcher = ToolGuardConfigWatcher(config_path=config_path)
    await watcher.start(engine)

    monkeypatch.setenv("SWE_TOOL_GUARD_ENABLED", "true")
    save_config(_config(False), config_path)
    assert await watcher._check_once() is True
    assert engine.enabled is True

    monkeypatch.setenv("SWE_TOOL_GUARD_ENABLED", "false")
    save_config(_config(True), config_path)
    assert await watcher._check_once() is True
    assert engine.enabled is False
    await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_keeps_fingerprint_for_invalid_file(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_config(True), config_path)
    engine = _Engine()
    watcher = ToolGuardConfigWatcher(config_path=config_path)
    await watcher.start(engine)
    previous = watcher.last_fingerprint

    config_path.write_text("{", encoding="utf-8")

    assert await watcher._check_once() is False
    assert engine.reload_count == 0
    assert watcher.last_fingerprint == previous
    await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_keeps_old_state_when_reload_fails(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    save_config(_config(True), config_path)
    engine = _Engine(fail=True)
    watcher = ToolGuardConfigWatcher(config_path=config_path)
    await watcher.start(engine)
    previous = watcher.last_fingerprint

    save_config(_config(False), config_path)

    assert await watcher._check_once() is False
    assert engine.reload_count == 1
    assert watcher.last_fingerprint == previous
    await watcher.stop()

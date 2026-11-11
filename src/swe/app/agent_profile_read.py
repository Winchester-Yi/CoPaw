# -*- coding: utf-8 -*-
"""只读解析既有Agent配置引用，不修复配置、迁移目录或初始化工作区。"""

import json
from pathlib import Path

from fastapi import HTTPException

from ..config import utils as config_utils
from ..config.config import AgentProfileConfig, AgentsConfig


def checked_path(path, root):
    """配置引用和符号链接不能越出当前租户及工作区边界。"""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise HTTPException(403, "Agent workspace is unavailable") from error
    return resolved


def _scope_data(tenant_dir):
    path = checked_path(tenant_dir / "config.json", tenant_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = config_utils._normalize_working_dir_bound_paths(json.loads(raw))
    if not isinstance(data, dict):
        raise ValueError("Tenant config must be an object")
    return data


def resolve_agent_workspace(
    storage_id,
    agent_id=None,
    *,
    allow_disabled=False,
):
    """返回既有工作区、选定Agent、展示时区和启用状态，不读取任务数据。"""
    base = Path(config_utils.WORKING_DIR).expanduser().resolve()
    tenant_dir = base / storage_id
    checked_path(tenant_dir, tenant_dir)
    data = _scope_data(tenant_dir)
    if data is None:
        if agent_id not in (None, "default"):
            raise HTTPException(404, f"Agent '{agent_id}' not found")
        selected, zone, enabled = "default", "UTC", True
        workspace = tenant_dir / "workspaces/default"
    else:
        agents = AgentsConfig.model_validate(data.get("agents", {}))
        selected = agent_id or agents.active_agent or "default"
        profile = agents.profiles.get(selected)
        if profile is None:
            raise HTTPException(404, f"Agent '{selected}' not found")
        enabled = profile.enabled
        if not enabled and not allow_disabled:
            raise HTTPException(403, f"Agent '{selected}' is disabled")
        workspace = Path(profile.workspace_dir)
        zone = data.get("user_timezone") or "UTC"
        if not isinstance(zone, str):
            raise ValueError("User timezone must be a string")
    workspace = checked_path(workspace, tenant_dir / "workspaces")
    return workspace, selected, zone, enabled


def read_agent_profile(storage_id, agent_id):
    """系统控制面读取当前Agent正文，脏文件只报错，不备份或改写。"""
    workspace, selected, zone, enabled = resolve_agent_workspace(
        storage_id,
        agent_id,
        allow_disabled=True,
    )
    path = checked_path(workspace / "agent.json", workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Agent config must be an object")
    data.setdefault("id", selected)
    data.setdefault("name", selected.title())
    profile = AgentProfileConfig.model_validate(data)
    if profile.id != selected:
        raise ValueError("agent_profile_identity_conflict")
    return profile, zone, enabled

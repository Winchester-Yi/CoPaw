# -*- coding: utf-8 -*-
"""配置保存后只提交新版刷新意图，不操作平台、旧绑定或任务文件。"""

from dataclasses import dataclass
import logging
from uuid import uuid4

import httpx

from ...config.context import encode_scope_id, is_valid_identity_value
from .monitor_sync_client import get_scheduler_api_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshScope:
    """路由信息来自已校验的工作区或配置请求，角色只能按真实请求转发。"""

    tenant_id: str
    source_id: str
    agent_id: str
    actor_id: str | None = None
    role: str | None = None

    def headers(self):
        actor = self.actor_id or self.tenant_id
        if any(
            not isinstance(value, str)
            or len(value) > 128
            or value != value.strip()
            or not is_valid_identity_value(value)
            for value in (actor, self.tenant_id, self.source_id, self.agent_id)
        ):
            raise ValueError("invalid_system_refresh_identity")
        headers = {
            "X-User-Id": actor,
            "X-Tenant-Id": self.tenant_id,
            "X-Source-Id": self.source_id,
            "X-Agent-Id": self.agent_id,
            "X-Scope-Id": encode_scope_id(self.tenant_id, self.source_id),
        }
        if self.role is not None:
            headers["X-User-Role"] = self.role
        return headers


class SystemRefreshClient:
    """保存成功不因同步受理失败而假装回滚；失败必须明确返回设置页。"""

    def __init__(self, base_url=None, *, http=None):
        self.base_url = (base_url or get_scheduler_api_url()).rstrip("/")
        self.http = http

    async def _post(self, scope, path, body):
        try:
            headers = scope.headers()
            if self.http is not None:
                response = await self.http.post(
                    self.base_url + path,
                    headers=headers,
                    json=body,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=3,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(
                        self.base_url + path,
                        headers=headers,
                        json=body,
                    )
            response.raise_for_status()
            result = response.json()
            if response.status_code != 202 or not isinstance(result, dict):
                raise ValueError("invalid_system_refresh_receipt")
            return {
                "submission_status": "accepted",
                "receipt": result,
                "request_id": body["request_id"],
            }
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
            logger.warning(
                "配置已保存但系统同步未受理 error_type=%s",
                type(error).__name__,
            )
            return {
                "submission_status": "failed",
                "error_code": "system_sync_not_submitted",
                "request_id": body["request_id"],
            }

    async def refresh(self, scope, task_type, *, request_id=None):
        """仅提交当前任务类型和请求号，不传过时配置全文。"""
        return await self._post(
            scope,
            "/scheduler/cron/system/jobs/refresh",
            {"task_type": task_type, "request_id": request_id or str(uuid4())},
        )

    async def workspace(self, scope, *, request_id=None):
        """工作区启动使用内网固定授权入口，不构造管理员Header。"""
        return await self._post(
            scope,
            "/scheduler/internal/cron/system/workspaces/refresh",
            {"request_id": request_id or str(uuid4())},
        )


async def submit_workspace_refresh(workspace, task_type):
    """配置保存直接提交刷新，不依赖旧任务管理器的存在。"""
    from ...config.context import resolve_runtime_identity

    tenant, source, _ = resolve_runtime_identity(workspace.tenant_id)
    return await submit_agent_refresh(
        tenant or workspace.tenant_id or "",
        source or "",
        workspace.agent_id or "default",
        task_type,
    )


async def submit_agent_refresh(tenant_id, source_id, agent_id, task_type=None):
    """原初始化/保存挂点只转交刷新意图，不能恢复旧自动注册。"""
    client = SystemRefreshClient()
    scope = RefreshScope(tenant_id, source_id, agent_id or "default")
    if task_type is None:
        return await client.workspace(scope)
    return await client.refresh(scope, task_type)


async def submit_source_refresh(source_id, identity, task_type):
    """来源保存转发真实管理员身份；普通工作区启动不经过此管理入口。"""
    scope = RefreshScope(
        identity.tenant_id,
        source_id,
        "default",
        identity.updated_by or identity.tenant_id,
        getattr(identity, "role", None),
    )
    return await SystemRefreshClient().refresh(scope, task_type)

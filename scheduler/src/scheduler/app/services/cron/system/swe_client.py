# -*- coding: utf-8 -*-
"""系统控制面只读SWE配置，并按共库执行ID派发，不回传最终结果。"""

import httpx

from ..managed.identity import encode_scope
from ..managed.swe_client import checked_base


class SystemSweClient:
    """沿用可信内网与身份Header，不引入必配内部Token。"""

    def __init__(self, base_url, *, http=None):
        self.base_url = checked_base(base_url).removesuffix("/api")
        self.http = http

    def _headers(self, row):
        tenant, source = row["route_tenant_id"], row["source_id"]
        return {
            "X-User-Id": tenant,
            "X-Tenant-Id": tenant,
            "X-Source-Id": source,
            "X-Agent-Id": row["route_agent_id"],
            "X-Scope-Id": encode_scope(tenant, source),
        }

    async def _request(self, method, action, row, **kwargs):
        url = f"{self.base_url}/api/internal/cron/system/{action}"
        headers = self._headers(row)
        if self.http is not None:
            response = await self.http.request(
                method,
                url,
                headers=headers,
                **kwargs,
            )
        else:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers=headers,
                    **kwargs,
                )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("invalid_swe_system_response")
        return result

    async def get_config(self, row):
        """只传共库操作引用，不提交调用方缓存的旧配置。"""
        return await self._request(
            "GET",
            "config",
            row,
            params={
                "job_id": row["job_id"],
                "operation_id": row["operation_id"],
            },
        )

    async def execute(self, job, run):
        """仅传稳定执行ID和当前派发围栏，202不是最终执行结果。"""
        return await self._request(
            "POST",
            "execute",
            job,
            json={
                "execution_id": run["execution_id"],
                "job_id": job["job_id"],
                "dispatch_token": run["dispatch_owner"],
            },
        )

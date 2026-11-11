# -*- coding: utf-8 -*-
"""SWE 业务入口只读 Scheduler；不读取旧文件任务或管理器。"""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from ...config.context import is_valid_identity_value
from .monitor_sync_client import get_scheduler_api_url


class ManagedCronQueryClient:
    def __init__(self, base_url=None, *, http=None, headers=None):
        self.base_url = (base_url or get_scheduler_api_url()).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("cron_scheduler_url_invalid")
        self.http = http
        self.headers = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower()
            in {
                "authorization",
                "x-header-cookie",
                "b3",
                "x-b3-traceid",
                "x-b3-spanid",
            }
        }

    @asynccontextmanager
    async def _client(self):
        if self.http is not None:
            yield self.http
        else:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
            ) as client:
                yield client

    async def list_jobs(self, tenant, source, agent="default"):
        return await self._pages(tenant, source, agent, "jobs")

    async def query_progress(self, user_id, source_id, agent_id, date):
        from datetime import timezone
        from zoneinfo import ZoneInfo

        instant = (
            date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
        )
        day = instant.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        items = await self._pages(
            user_id,
            source_id,
            agent_id,
            "progress",
            day,
        )
        return sorted(
            items,
            key=lambda item: (item["scheduled_time"], item["job_id"]),
        )

    def _identity_headers(self, tenant, source, agent):
        identity = (tenant, source, agent)
        if any(
            not isinstance(value, str)
            or value != value.strip()
            or len(value) > 128
            or not is_valid_identity_value(value)
            for value in identity
        ):
            raise ValueError("cron_query_identity_invalid")
        return {
            **self.headers,
            "X-User-Id": tenant,
            "X-Tenant-Id": tenant,
            "X-Source-Id": source,
            "X-Agent-Id": agent,
        }

    @staticmethod
    def _validate_page(payload, day):
        if (
            not isinstance(payload, dict)
            or "next_cursor" not in payload
            or not isinstance(payload.get("items"), list)
        ):
            raise ValueError("cron_query_response_invalid")
        if day is not None and (
            payload.get("date") != day
            or payload.get("timezone") != "Asia/Shanghai"
        ):
            raise ValueError("cron_query_date_conflict")

    async def _pages(self, tenant, source, agent, endpoint, day=None):
        identity = (tenant, source, agent)
        headers = self._identity_headers(*identity)
        result, cursors, identifiers = [], set(), set()
        cursor = None
        async with self._client() as client:
            while True:
                params = (
                    {"limit": 200, "include_deleted": "false"}
                    if day is None
                    else {"limit": 200, "date": day}
                )
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(
                    self.base_url + "/scheduler/cron/" + endpoint,
                    headers=headers,
                    params=params,
                )
                response.raise_for_status()
                payload = response.json()
                self._validate_page(payload, day)
                for job in payload["items"]:
                    self._validate(
                        job,
                        identity,
                        identifiers,
                        "job_id" if day else "id",
                    )
                    if day is not None:
                        self._validate_progress(job)
                    result.append(job)
                cursor = payload.get("next_cursor")
                if cursor is None:
                    return result
                if (
                    not isinstance(cursor, str)
                    or not cursor
                    or cursor in cursors
                ):
                    raise ValueError("cron_query_cursor_invalid")
                cursors.add(cursor)

    @staticmethod
    def _validate(job, identity, identifiers, id_field="id"):
        if (
            not isinstance(job, dict)
            or job.get("management_owner") != "scheduler"
            or tuple(
                job.get(key) for key in ("tenant_id", "source_id", "agent_id")
            )
            != identity
        ):
            raise ValueError("cron_query_identity_conflict")
        identifier = job.get(id_field)
        if (
            not isinstance(identifier, str)
            or not identifier
            or not is_valid_identity_value(identifier)
            or identifier in identifiers
        ):
            raise ValueError("cron_query_identity_conflict")
        identifiers.add(identifier)

    @staticmethod
    def _validate_progress(job):
        for field in (
            "task_name",
            "status",
            "status_text",
            "time_info",
            "scheduled_time",
        ):
            if not isinstance(job.get(field), str):
                raise ValueError("cron_query_response_invalid")
        if not isinstance(job.get("meta"), dict):
            raise ValueError("cron_query_response_invalid")

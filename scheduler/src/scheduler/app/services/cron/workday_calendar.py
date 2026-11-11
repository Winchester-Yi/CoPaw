# -*- coding: utf-8 -*-
"""Scheduler 管理按北京时间业务日期共享的工作日日历缓存。"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from scheduler.app.database import get_db_connection
from .workday_api_auth import WorkdayAuthError, build_workday_headers

logger = logging.getLogger(__name__)
API_CACHE_SECONDS = 86400
API_TIMEOUT_SECONDS = 2.0
FALLBACK_CACHE_SECONDS = 300
FETCH_LEASE_SECONDS = 10


class CalendarStore:
    """通过共库条件更新和过期租约合并多实例对同一日期的查询。"""

    def __init__(self, db):
        self.db = db

    async def get(self, day, now):
        """读取尚未过期的完整日期结果，不返回仅有刷新租约的空行。"""
        row = await self.db.fetch_one(
            "SELECT is_workday, result_source, expires_at FROM swe_workday_calendar "
            "WHERE calendar_date=%s AND expires_at>%s AND is_workday IS NOT NULL",
            (day, now.replace(tzinfo=None)),
        )
        if not row:
            return None
        expiry = row["expires_at"]
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        return {
            "date": day.isoformat(),
            "is_workday": bool(row["is_workday"]),
            "source": row["result_source"],
            "expires_at": expiry.replace(tzinfo=timezone.utc),
        }

    async def claim(self, day, token, now):
        """原子领取过期日期的刷新租约，未领取者不得访问上游。"""
        utc_now = now.replace(tzinfo=None)
        await self.db.execute(
            "INSERT IGNORE INTO swe_workday_calendar (calendar_date) VALUES (%s)",
            (day,),
        )
        changed = await self.db.execute(
            "UPDATE swe_workday_calendar SET refresh_token=%s, refresh_until=%s "
            "WHERE calendar_date=%s AND (expires_at IS NULL OR expires_at<=%s) "
            "AND (refresh_until IS NULL OR refresh_until<=%s)",
            (
                token,
                utc_now + timedelta(seconds=FETCH_LEASE_SECONDS),
                day,
                utc_now,
                utc_now,
            ),
        )
        return changed == 1

    async def save(self, day, result, token, now):
        """只允许仍持有当前令牌的刷新者回写，防止迟到结果覆盖。"""
        await self.db.execute(
            "UPDATE swe_workday_calendar SET is_workday=%s, result_source=%s, "
            "expires_at=%s, fetched_at=%s, refresh_token=NULL, refresh_until=NULL "
            "WHERE calendar_date=%s AND refresh_token=%s",
            (
                int(result["is_workday"]),
                result["source"],
                result["expires_at"].replace(tzinfo=None),
                now.replace(tzinfo=None),
                day,
                token,
            ),
        )


def get_workday_api_url() -> str:
    """只有缓存需要刷新时才验证地址，环境配置不能覆盖有效日期缓存。"""
    template = os.environ.get("SCHEDULER_WORKDAY_API_URL", "")
    if "{date}" not in template:
        raise ValueError("SCHEDULER_WORKDAY_API_URL must contain {date}")
    template = template.strip()
    try:
        address = urlsplit(template.format(date="2000-01-01"))
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("Invalid SCHEDULER_WORKDAY_API_URL template") from exc
    if address.scheme not in {"http", "https"} or not address.netloc:
        raise ValueError("SCHEDULER_WORKDAY_API_URL must be an HTTP(S) URL")
    return template


async def _fetch_workday(day):
    template = get_workday_api_url()
    try:
        headers = await asyncio.to_thread(build_workday_headers)
    except WorkdayAuthError as error:
        logger.warning(
            "Workday API authentication unavailable: code=%s",
            error.code,
        )
        raise
    async with httpx.AsyncClient(
        timeout=API_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as client:
        response = await client.get(
            template.format(date=day.isoformat()),
            headers=headers,
        )
        response.raise_for_status()
        value = json.loads(response.text)
    # 布尔值或任意对象不属于日历协议约定的整数0/1。
    if isinstance(value, str) and value in {"0", "1", "-1"}:
        value = int(value)
    return value if type(value) is int and value in {0, 1} else -1


class CalendarService:
    """在共享日期缓存之上提供有超时和周末降级的日历判定。"""

    def __init__(self, *, store=None, fetcher=None, clock=None):
        self.store = store
        self.fetcher = fetcher or _fetch_workday
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fallback(self, day, now):
        return {
            "date": day.isoformat(),
            "is_workday": day.weekday() < 5,
            "source": "fallback",
            "expires_at": now + timedelta(seconds=FALLBACK_CACHE_SECONDS),
        }

    async def get_day(self, day: date):
        """返回日期及判定来源，存储不可用时不以猜测冒充接口结果。"""
        now = self.clock().astimezone(timezone.utc)
        try:
            store = self.store or CalendarStore(get_db_connection())
            cached = await store.get(day, now)
            if cached is not None:
                return cached
            token = uuid4().hex
            if await store.claim(day, token, now):
                return await self._refresh(store, day, token)
            # 等待已持租约实例，不另行发起同日期的上游查询。
            for delay in (0.05, 0.1, 0.2, 0.4, 0.8, 0.8):
                await asyncio.sleep(delay)
                cached = await store.get(
                    day,
                    self.clock().astimezone(timezone.utc),
                )
                if cached is not None:
                    return cached
        except Exception as exc:
            # 缺表或断库也沿用明确的周末降级，不偷偷创建数据表。
            logger.warning(
                "Workday cache unavailable: date=%s error=%s",
                day,
                type(exc).__name__,
            )
        return self._fallback(day, now)

    async def _refresh(self, store, day, token):
        try:
            value = await asyncio.wait_for(
                self.fetcher(day),
                timeout=API_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "Workday API unavailable: date=%s error=%s",
                day,
                type(exc).__name__,
            )
            value = -1
        now = self.clock().astimezone(timezone.utc)
        result = self._fallback(day, now)
        if type(value) is int and value in {0, 1}:
            result.update(
                is_workday=bool(value),
                source="api",
                expires_at=now + timedelta(seconds=API_CACHE_SECONDS),
            )
        try:
            await store.save(day, result, token, now)
        except Exception as exc:
            logger.warning(
                "Workday cache write unavailable: date=%s error=%s",
                day,
                type(exc).__name__,
            )
        return result


_calendar_service = CalendarService()


def get_calendar_service():
    """返回当前进程的无用户身份日历服务。"""
    return _calendar_service

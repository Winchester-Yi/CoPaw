# -*- coding: utf-8 -*-
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock
import asyncio

import pytest

from scheduler.app.services.cron.workday_calendar import CalendarService
from scheduler.app.services.cron.workday_calendar import CalendarStore
from scheduler.app.services.cron.calendar_schedule import (
    allows,
    next_run_times,
)


class MemoryStore:
    def __init__(self):
        self.rows = {}
        self.leases = {}

    async def get(self, day, now):
        row = self.rows.get(day)
        return row if row and row["expires_at"] > now else None

    async def claim(self, day, token, now):
        if day in self.leases:
            return False
        self.leases[day] = token
        return True

    async def save(self, day, result, token, now):
        if self.leases.get(day) == token:
            self.rows[day] = result
            del self.leases[day]


@pytest.mark.asyncio
async def test_shared_calendar_cache_coalesces_concurrent_date_requests():
    store = MemoryStore()
    fetched = []

    async def fetch(day):
        fetched.append(day)
        await asyncio.sleep(0.01)
        return 1

    first = CalendarService(store=store, fetcher=fetch)
    second = CalendarService(store=store, fetcher=fetch)
    results = await asyncio.gather(
        *[
            service.get_day(date(2026, 10, 10))
            for service in [first, second, first, second]
        ],
    )
    assert all(result["is_workday"] for result in results)
    assert fetched == [date(2026, 10, 10)]
    assert all(result["source"] == "api" for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "day,expected",
    [("2026-10-09", True), ("2026-10-10", False)],
)
async def test_unknown_falls_back_to_weekday_with_short_cache(day, expected):
    store = MemoryStore()
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    fetch = AsyncMock(return_value=-1)
    service = CalendarService(store=store, fetcher=fetch, clock=lambda: now)
    result = await service.get_day(date.fromisoformat(day))
    assert result["is_workday"] is expected
    assert result["source"] == "fallback"
    assert result["expires_at"] == now + timedelta(minutes=5)
    await service.get_day(date.fromisoformat(day))
    assert fetch.await_count == 1
    now += timedelta(minutes=6)
    await service.get_day(date.fromisoformat(day))
    assert fetch.await_count == 2


@pytest.mark.asyncio
async def test_api_holiday_and_makeup_weekend_override_weekday_rule():
    fetch = AsyncMock(side_effect=[0, 1])
    service = CalendarService(store=MemoryStore(), fetcher=fetch)
    assert not (await service.get_day(date(2026, 10, 1)))["is_workday"]
    assert (await service.get_day(date(2026, 10, 10)))["is_workday"]


@pytest.mark.asyncio
async def test_api_exception_is_cached_as_fallback():
    service = CalendarService(
        store=MemoryStore(),
        fetcher=AsyncMock(side_effect=TimeoutError()),
    )
    assert (await service.get_day(date(2026, 10, 9)))["source"] == "fallback"


@pytest.mark.asyncio
async def test_known_api_holiday_survives_cache_write_failure():
    store = MemoryStore()
    store.save = AsyncMock(side_effect=RuntimeError("database write failed"))
    service = CalendarService(store=store, fetcher=AsyncMock(return_value=0))
    result = await service.get_day(date(2026, 10, 1))
    assert result["source"] == "api"
    assert result["is_workday"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression,fire_at,fallback_day,expected_allowed",
    [
        ("0 9 LW * *", "2026-05-29T09:00:00+08:00", date(2026, 5, 31), True),
        ("0 9 15W * *", "2026-09-14T09:00:00+08:00", date(2026, 9, 15), False),
    ],
)
async def test_special_rule_reports_fallback_used_to_select_target(
    expression,
    fire_at,
    fallback_day,
    expected_allowed,
):
    class MixedCalendar:
        async def get_day(self, day):
            return {
                "is_workday": day.weekday() < 5,
                "source": "fallback" if day == fallback_day else "api",
            }

    result = await allows(
        expression,
        datetime.fromisoformat(fire_at),
        MixedCalendar(),
    )
    assert result["allowed"] is expected_allowed
    assert result["source"] == "fallback"


@pytest.mark.asyncio
async def test_one_admission_reuses_the_same_date_observation():
    seen = []

    class ChangingCalendar:
        async def get_day(self, day):
            seen.append(day)
            working = day != date(2026, 9, 30) or seen.count(day) > 1
            return {"is_workday": working, "source": "api"}

    result = await allows(
        "0 9 LW * *",
        datetime.fromisoformat("2026-09-30T09:00:00+08:00"),
        ChangingCalendar(),
    )
    assert not result["allowed"]
    assert seen.count(date(2026, 9, 30)) == 1


@pytest.mark.asyncio
async def test_slow_api_has_a_total_deadline(monkeypatch):
    from scheduler.app.services.cron import workday_calendar as module

    monkeypatch.setattr(module, "API_TIMEOUT_SECONDS", 0.01, raising=False)

    async def slow(day):
        await asyncio.sleep(0.05)
        return 1

    service = CalendarService(store=MemoryStore(), fetcher=slow)
    assert (await service.get_day(date(2026, 10, 10)))["source"] == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,source,working",
    [
        ("1", "api", True),
        ('"1"', "api", True),
        ("0", "api", False),
        ("-1", "fallback", False),
        ("<html>error</html>", "fallback", False),
    ],
)
async def test_http_calendar_response_and_date_parameter(
    monkeypatch,
    body,
    source,
    working,
):
    import httpx
    from scheduler.app.services.cron import workday_calendar as module

    monkeypatch.setattr(
        module,
        "build_workday_headers",
        lambda: {"apisign": "test-only"},
    )

    original = httpx.AsyncClient
    requested = []

    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=body)

    monkeypatch.setenv(
        "SCHEDULER_WORKDAY_API_URL",
        "http://calendar.test/{date}",
    )
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    result = await CalendarService(store=MemoryStore()).get_day(
        date(2026, 10, 10),
    )
    assert result["source"] == source
    assert result["is_workday"] is working
    assert requested == ["http://calendar.test/2026-10-10"]


class HolidayCalendar:
    async def get_day(self, day):
        workday = day.weekday() < 5
        if day == date(2026, 9, 30):
            workday = False
        if day == date(2026, 10, 10):
            workday = True
        return {
            "is_workday": workday,
            "source": "api",
            "date": day.isoformat(),
        }


@pytest.mark.asyncio
async def test_calendar_rules_preserve_minute_steps():
    runs = await next_run_times(
        "*/30 9 LW * *",
        "Asia/Shanghai",
        HolidayCalendar(),
        count=3,
        now=datetime.fromisoformat("2026-09-01T08:00:00+08:00"),
    )
    assert [value.isoformat() for value in runs] == [
        "2026-09-29T09:00:00+08:00",
        "2026-09-29T09:30:00+08:00",
        "2026-10-30T09:00:00+08:00",
    ]


@pytest.mark.asyncio
async def test_calendar_preview_uses_beijing_date_and_existing_clock_timezone():
    runs = await next_run_times(
        "0 16 LW * *",
        "UTC",
        HolidayCalendar(),
        count=1,
        now=datetime.fromisoformat("2026-09-01T00:00:00+00:00"),
    )
    assert runs[0].isoformat() == "2026-09-28T16:00:00+00:00"


@pytest.mark.asyncio
async def test_previous_workday_keeps_month_boundary_without_forward_shift():
    class Closed:
        async def get_day(self, day):
            return {
                "is_workday": day.weekday() < 5 and day.day not in {1, 15},
                "source": "api",
            }

    for cron, expected in [
        ("0 9 1W 9 * 2026", []),
        ("0 9 15W 9 * 2026", ["2026-09-14T09:00:00+08:00"]),
    ]:
        runs = await next_run_times(
            cron,
            "Asia/Shanghai",
            Closed(),
            count=1,
            now=datetime.fromisoformat("2026-09-01T00:00:00+08:00"),
        )
        assert [run.isoformat() for run in runs] == expected


@pytest.mark.asyncio
async def test_last_workday_and_previous_workday_include_holidays():
    now = datetime.fromisoformat("2026-09-01T08:00:00+08:00")
    calendar = HolidayCalendar()
    for cron in ["0 9 LW * *", "0 9 30W * *"]:
        runs = await next_run_times(
            cron,
            "Asia/Shanghai",
            calendar,
            count=1,
            now=now,
        )
        assert runs[0].isoformat() == "2026-09-29T09:00:00+08:00"


@pytest.mark.asyncio
async def test_makeup_weekend_can_be_the_anchor_workday():
    runs = await next_run_times(
        "0 9 10W * *",
        "Asia/Shanghai",
        HolidayCalendar(),
        count=1,
        now=datetime.fromisoformat("2026-10-01T00:00:00+08:00"),
    )
    assert runs[0].day == 10


@pytest.mark.asyncio
async def test_beijing_date_is_used_even_for_utc_schedule():
    fire = datetime.fromisoformat("2026-10-09T16:30:00+00:00")

    class Weekdays:
        async def get_day(self, day):
            return {
                "is_workday": day.weekday() < 5,
                "source": "fallback",
                "date": day.isoformat(),
            }

    result = await allows("30 16 * * *", fire, Weekdays())
    assert result["business_date"] == "2026-10-10"
    assert not result["allowed"]
    assert (
        await allows("30 16 * * *", fire, Weekdays(), run_on_non_workday=True)
    )["allowed"]


@pytest.mark.asyncio
async def test_calendar_month_filter_skips_without_fetching_day():
    calendar = type(
        "Calendar",
        (),
        {
            "get_day": AsyncMock(
                return_value={"is_workday": True, "source": "api"},
            ),
        },
    )()
    result = await allows(
        "0 1 1W 6 *",
        datetime.fromisoformat("2026-07-01T01:00:00+08:00"),
        calendar,
    )
    assert not result["allowed"]
    assert result["skipped"] == "calendar_rule"
    calendar.get_day.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_filters_holiday_but_retains_original_cron_candidates():
    runs = await next_run_times(
        "0 9 * * mon-fri",
        "Asia/Shanghai",
        HolidayCalendar(),
        count=2,
        now=datetime.fromisoformat("2026-09-29T10:00:00+08:00"),
    )
    assert [run.date().isoformat() for run in runs] == [
        "2026-10-01",
        "2026-10-02",
    ]


@pytest.mark.asyncio
async def test_database_lease_owner_and_expiry_control_cache_writes():
    import sqlite3

    class Database:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row
            self.conn.execute(
                "CREATE TABLE swe_workday_calendar (calendar_date DATE PRIMARY KEY, is_workday INTEGER, result_source TEXT, expires_at TEXT, fetched_at TEXT, refresh_token TEXT, refresh_until TEXT)",
            )

        async def execute(self, sql, params):
            sql = sql.replace("INSERT IGNORE", "INSERT OR IGNORE").replace(
                "%s",
                "?",
            )
            return self.conn.execute(sql, params).rowcount

        async def fetch_one(self, sql, params):
            row = self.conn.execute(sql.replace("%s", "?"), params).fetchone()
            return dict(row) if row else None

    store = CalendarStore(Database())
    day = date(2026, 10, 10)
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    assert await store.claim(day, "first", now)
    assert not await store.claim(day, "second", now)
    later = now + timedelta(seconds=11)
    assert await store.claim(day, "second", later)
    result = {
        "is_workday": True,
        "source": "api",
        "expires_at": later + timedelta(days=1),
    }
    await store.save(day, result, "first", later)
    assert await store.get(day, later) is None
    await store.save(day, result, "second", later)
    assert (await store.get(day, later))["is_workday"] is True
    assert await store.get(day, later + timedelta(days=2)) is None


@pytest.mark.asyncio
async def test_calendar_api_previews_holiday_adjusted_dates(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from scheduler.app.routers import calendar as calendar_router

    monkeypatch.setattr(
        calendar_router,
        "get_calendar_service",
        HolidayCalendar,
    )
    app = FastAPI()
    app.include_router(calendar_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/scheduler/cron/calendar/resolve",
            json={
                "cron": "0 9 LW * *",
                "timezone": "Asia/Shanghai",
                "at": "2026-09-01T00:00:00+08:00",
                "count": 1,
            },
        )
    assert response.status_code == 200
    assert response.json()["next_run_times"] == ["2026-09-29T09:00:00+08:00"]


@pytest.mark.asyncio
async def test_calendar_api_requires_timezone_for_explicit_timestamp():
    import httpx
    from fastapi import FastAPI
    from scheduler.app.routers import calendar as calendar_router

    app = FastAPI()
    app.include_router(calendar_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/scheduler/cron/calendar/resolve",
            json={"at": "2026-10-09T16:00:00"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_calendar_preview_reports_future_fallback_not_only_today(
    monkeypatch,
):
    """当前日期命中 API 不代表未来预览也全部采用权威日历。"""
    import httpx
    from fastapi import FastAPI
    from scheduler.app.routers import calendar as calendar_router

    class MixedCalendar:
        async def get_day(self, day):
            return {
                "is_workday": True,
                "source": (
                    "api" if day.isoformat() == "2026-09-01" else "fallback"
                ),
            }

    monkeypatch.setattr(calendar_router, "get_calendar_service", MixedCalendar)
    app = FastAPI()
    app.include_router(calendar_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/scheduler/cron/calendar/resolve",
            json={
                "cron": "0 9 * * *",
                "timezone": "Asia/Shanghai",
                "at": "2026-09-01T12:00:00+08:00",
                "count": 1,
            },
        )
    assert response.status_code == 200
    assert response.json()["source"] == "api"
    assert response.json()["preview_source"] == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"cron": "not a cron"},
        {"cron": "0 9 LW 99 *"},
        {"cron": "0 9 LW * mon"},
        {"cron": "0 0 9 * * * 2027"},
        {"timezone": "Missing/Timezone"},
    ],
)
async def test_calendar_api_rejects_invalid_rule_even_without_preview(payload):
    """仅查询日期时也不能把非法规则静默当成普通工作日判定。"""
    import httpx
    from fastapi import FastAPI
    from scheduler.app.routers import calendar as calendar_router

    app = FastAPI()
    app.include_router(calendar_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/scheduler/cron/calendar/resolve",
            json=payload,
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_previous_workday_31_uses_short_month_end():
    """短月的31W以月末为锚点，不跨到下个月。"""
    runs = await next_run_times(
        "0 9 31W * *",
        "Asia/Shanghai",
        HolidayCalendar(),
        count=1,
        now=datetime.fromisoformat("2026-02-01T00:00:00+08:00"),
    )
    assert runs[0].isoformat() == "2026-02-27T09:00:00+08:00"

# -*- coding: utf-8 -*-
"""按提供的行内 SDK 协议验证请求，不使用真实密钥或连接外部网关。"""

import asyncio
import sys
from datetime import date
from types import ModuleType, SimpleNamespace

import httpx
import pytest

PREFIX = "SCHEDULER_WORKDAY_API_"
KEY = "a" * 64


@pytest.fixture
def auth_env(monkeypatch):
    for name, value in {
        "APP_ID": "test-app",
        "APP_SECRET": "test-secret",
        "PRIVATE_KEY": KEY,
        "SIGN": "123",
        "URL": "http://calendar.test/{date}",
    }.items():
        monkeypatch.setenv(PREFIX + name, value)


@pytest.fixture
def sdk(monkeypatch):
    calls = []
    package = ModuleType("CMBSM")
    function = ModuleType("CMBSM.CMBSMFunction")
    utils = ModuleType("CMBSM.utils")
    com = ModuleType("CMBSM.utils.com")

    def convert(key):
        calls.append(("key", key))
        return bytes.fromhex(key)

    def sign(key, message):
        calls.append(("sign", key, message))
        return b"\x0a\xff"

    com.aschex_to_bcdhex = convert
    utils.com = com
    function.CMBSM2SignWithSM3 = sign
    for name, module in {
        "CMBSM": package,
        "CMBSM.CMBSMFunction": function,
        "CMBSM.utils": utils,
        "CMBSM.utils.com": com,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls, function


def test_exact_demo_signature_and_headers(auth_env, sdk, monkeypatch):
    from scheduler.app.services.cron import workday_api_auth as auth

    monkeypatch.setattr(
        auth,
        "time",
        SimpleNamespace(time=lambda: 1793779200.99),
    )
    headers = auth.build_workday_headers()
    assert sdk[0] == [
        ("key", KEY),
        (
            "sign",
            bytes.fromhex(KEY),
            b"appid=test-app&secret=test-secret&sign=123&timestamp=1793779200",
        ),
    ]
    assert headers == {
        "Content-Type": "application/json;charset=UTF-8",
        "Connection": "close",
        "appid": "test-app",
        "apisign": "0aff",
        "verify": "SM3withSM2",
        "timestamp": "1793779200",
        "sign": "123",
    }
    assert "test-secret" not in str(headers) and KEY not in str(headers)


def test_each_request_uses_fresh_timestamp_and_configured_sign(
    auth_env,
    sdk,
    monkeypatch,
):
    from scheduler.app.services.cron import workday_api_auth as auth

    times = iter([1000.1, 1001.9])
    monkeypatch.setattr(
        auth,
        "time",
        SimpleNamespace(time=lambda: next(times)),
    )
    monkeypatch.setenv(PREFIX + "SIGN", "custom-sign")
    assert auth.build_workday_headers()["timestamp"] == "1000"
    assert auth.build_workday_headers()["timestamp"] == "1001"
    assert sdk[0][-1][2].endswith(b"sign=custom-sign&timestamp=1001")


def test_message_is_utf8_and_not_url_encoded(auth_env, sdk, monkeypatch):
    from scheduler.app.services.cron import workday_api_auth as auth

    monkeypatch.setenv(PREFIX + "APP_SECRET", "测试&x=1")
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: 1000))
    auth.build_workday_headers()
    assert sdk[0][-1][
        2
    ] == "appid=test-app&secret=测试&x=1&sign=123&timestamp=1000".encode("utf-8")


@pytest.mark.parametrize("result", [None, b"", "not-signature-bytes"])
def test_invalid_sdk_signature_is_rejected(auth_env, sdk, result):
    from scheduler.app.services.cron.workday_api_auth import (
        WorkdayAuthError,
        build_workday_headers,
    )

    sdk[1].CMBSM2SignWithSM3 = lambda *args: result
    with pytest.raises(WorkdayAuthError, match="workday_auth_signing_failed"):
        build_workday_headers()


@pytest.mark.parametrize(
    "field,value",
    [
        ("APP_ID", ""),
        ("APP_SECRET", ""),
        ("PRIVATE_KEY", ""),
        ("SIGN", ""),
        ("PRIVATE_KEY", "not-a-hex-key"),
        ("APP_ID", "bad\r\nheader"),
        ("SIGN", "bad\nheader"),
    ],
)
def test_bad_configuration_stops_before_signing(
    auth_env,
    sdk,
    monkeypatch,
    field,
    value,
):
    from scheduler.app.services.cron.workday_api_auth import (
        WorkdayAuthError,
        build_workday_headers,
    )

    monkeypatch.setenv(PREFIX + field, value)
    with pytest.raises(WorkdayAuthError):
        build_workday_headers()
    assert sdk[0] == []


def test_missing_sdk_is_a_safe_error(auth_env, monkeypatch):
    from scheduler.app.services.cron.workday_api_auth import (
        WorkdayAuthError,
        build_workday_headers,
    )

    monkeypatch.setitem(sys.modules, "CMBSM.CMBSMFunction", None)
    with pytest.raises(WorkdayAuthError, match="workday_auth_sdk_unavailable"):
        build_workday_headers()


@pytest.mark.asyncio
async def test_sdk_failure_never_sends_unsigned_request_or_logs_secret(
    auth_env,
    sdk,
    monkeypatch,
    caplog,
):
    from scheduler.app.services.cron import workday_calendar as calendar
    from tests.unit.scheduler.test_workday_calendar import MemoryStore

    def fail(*args):
        raise RuntimeError(f"test-secret {KEY}")

    sdk[1].CMBSM2SignWithSM3 = fail
    opened = []

    def no_http(**kwargs):
        opened.append(True)
        raise AssertionError("must not send without signature")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", no_http)
    result = await calendar.CalendarService(store=MemoryStore()).get_day(
        date(2026, 10, 10),
    )
    assert result["source"] == "fallback" and not opened
    assert "test-secret" not in caplog.text and KEY not in caplog.text
    assert "workday_auth_signing_failed" in caplog.text


@pytest.mark.asyncio
async def test_http_get_carries_auth_and_does_not_follow_redirect(
    auth_env,
    sdk,
    monkeypatch,
):
    from scheduler.app.services.cron import workday_calendar as calendar

    calls = []
    original = httpx.AsyncClient

    def response(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "http://other.test"})

    monkeypatch.setattr(
        calendar.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(response),
            **kwargs,
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        await calendar._fetch_workday(date(2026, 10, 10))
    assert len(calls) == 1
    assert str(calls[0].url) == "http://calendar.test/2026-10-10"
    assert calls[0].headers["apisign"] == "0aff"
    assert "test-secret" not in str(calls[0].headers)


@pytest.mark.asyncio
async def test_valid_cache_does_not_require_credentials_or_sdk(monkeypatch):
    from scheduler.app.services.cron import workday_calendar as calendar
    from tests.unit.scheduler.test_workday_calendar import MemoryStore

    store = MemoryStore()
    service = calendar.CalendarService(
        store=store,
        fetcher=lambda day: asyncio.sleep(0, result=1),
    )
    day = date(2026, 10, 10)
    await service.get_day(day)
    for key in ("APP_ID", "APP_SECRET", "PRIVATE_KEY", "SIGN"):
        monkeypatch.delenv(PREFIX + key, raising=False)
    assert (await calendar.CalendarService(store=store).get_day(day))[
        "source"
    ] == "api"


@pytest.mark.asyncio
async def test_authenticated_success_is_cached(auth_env, sdk, monkeypatch):
    from scheduler.app.services.cron import workday_calendar as calendar
    from tests.unit.scheduler.test_workday_calendar import MemoryStore

    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        assert request.headers["verify"] == "SM3withSM2"
        assert request.headers["apisign"] == "0aff"
        return httpx.Response(200, text="1")

    monkeypatch.setattr(
        calendar.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    service = calendar.CalendarService(store=MemoryStore())
    for _ in range(2):
        result = await service.get_day(date(2026, 10, 10))
        assert result["source"] == "api" and result["is_workday"] is True
    assert len(requests) == 1 and len(sdk[0]) == 2


@pytest.mark.parametrize("env", ["dev", "prd"])
def test_auth_environment_overrides_file_defaults(auth_env, env):
    from scheduler.env_defaults import load_env_defaults
    from scheduler.app.services.cron.workday_api_auth import WorkdayAuthConfig

    load_env_defaults(env)
    config = WorkdayAuthConfig.load()
    assert config.app_id == "test-app" and config.app_secret == "test-secret"
    assert config.private_key == KEY
    assert KEY not in repr(config) and "test-secret" not in repr(config)


@pytest.mark.asyncio
async def test_missing_config_does_not_open_http(monkeypatch, caplog):
    from scheduler.app.services.cron import workday_calendar as calendar
    from tests.unit.scheduler.test_workday_calendar import MemoryStore

    monkeypatch.setenv(PREFIX + "URL", "http://calendar.test/{date}")
    monkeypatch.delenv(PREFIX + "APP_ID", raising=False)

    def unexpected(**kwargs):
        pytest.fail("must not open HTTP without authentication")

    monkeypatch.setattr(calendar.httpx, "AsyncClient", unexpected)
    assert (
        await calendar.CalendarService(store=MemoryStore()).get_day(
            date(2026, 10, 10),
        )
    )["source"] == "fallback"
    assert "workday_auth_config_missing" in caplog.text


@pytest.mark.asyncio
async def test_signing_timeout_keeps_event_loop_responsive_and_never_sends_late_http(
    auth_env,
    monkeypatch,
):
    import threading
    from scheduler.app.services.cron import workday_calendar as calendar

    entered, release = threading.Event(), threading.Event()

    def slow_signer():
        entered.set()
        release.wait(2)
        return {"apisign": "test-only"}

    requests = []
    original = httpx.AsyncClient
    monkeypatch.setattr(calendar, "build_workday_headers", slow_signer)
    monkeypatch.setattr(
        calendar.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda request: requests.append(request)
                or httpx.Response(200, text="1"),
            ),
            **kwargs,
        ),
    )
    task = asyncio.create_task(calendar._fetch_workday(date(2026, 10, 10)))
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait), 1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(task, 0.01)
    finally:
        release.set()
    await asyncio.sleep(0.02)
    assert requests == []

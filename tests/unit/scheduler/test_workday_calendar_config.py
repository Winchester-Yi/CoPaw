# -*- coding: utf-8 -*-
from datetime import date
import os

import httpx
import pytest

from scheduler.app.services.cron.workday_calendar import get_workday_api_url
from scheduler.env_defaults import load_env_defaults
from scheduler.app.services.cron import workday_calendar


@pytest.mark.parametrize("env", ["dev", "prd"])
def test_environment_json_supplies_calendar_url(monkeypatch, tmp_path, env):
    monkeypatch.delenv("SCHEDULER_WORKDAY_API_URL", raising=False)
    for name in ("APP_ID", "APP_SECRET", "PRIVATE_KEY", "SIGN"):
        monkeypatch.delenv("SCHEDULER_WORKDAY_API_" + name, raising=False)
    monkeypatch.chdir(tmp_path)
    load_env_defaults(env)
    assert (
        get_workday_api_url()
        == "http://tss-normal-api.paasst.cmbchina.cn/calendar/{date}"
    )
    assert "SCHEDULER_WORKDAY_API_URL" in os.environ
    assert os.environ["SCHEDULER_WORKDAY_API_SIGN"] == "123"
    for name in ("APP_ID", "APP_SECRET", "PRIVATE_KEY"):
        assert os.environ["SCHEDULER_WORKDAY_API_" + name] == ""


def test_environment_override_keeps_existing_precedence(monkeypatch):
    monkeypatch.setenv(
        "SCHEDULER_WORKDAY_API_URL",
        "http://override.test/{date}",
    )
    load_env_defaults("prd")
    assert get_workday_api_url() == "http://override.test/{date}"


@pytest.mark.asyncio
async def test_fetch_uses_environment_config(monkeypatch):
    monkeypatch.setattr(
        workday_calendar,
        "build_workday_headers",
        lambda: {"apisign": "test-only"},
    )
    monkeypatch.setenv(
        "SCHEDULER_WORKDAY_API_URL",
        "http://calendar.test/{date}",
    )
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, text="1")

    monkeypatch.setattr(
        workday_calendar.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    assert await workday_calendar._fetch_workday(date(2026, 10, 12)) == 1
    assert requests == ["http://calendar.test/2026-10-12"]


@pytest.mark.parametrize(
    "template",
    [
        "",
        "http://calendar.test/static",
        "http://calendar.test/{other}",
        "http://calendar.test/{date}/{other}",
        "file:///{date}",
    ],
)
def test_invalid_calendar_url_is_rejected(monkeypatch, template):
    monkeypatch.setenv("SCHEDULER_WORKDAY_API_URL", template)
    with pytest.raises(ValueError):
        get_workday_api_url()

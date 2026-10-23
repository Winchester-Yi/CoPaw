# -*- coding: utf-8 -*-
"""A stalled HTTP writer must release its replay subscription."""

import asyncio

import pytest

from swe.app.runner.sse_response import ReplayStreamingResponse


@pytest.mark.asyncio
@pytest.mark.parametrize("keepalive", [False, True])
async def test_send_timeout_closes_source_without_cancelling_business(
    keepalive,
):
    closed = asyncio.Event()

    async def source():
        try:
            yield "data: first\n\n"
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def send(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    generator = source()
    if keepalive:
        from swe.app.routers.console import _stream_with_keepalive

        body = _stream_with_keepalive(generator, interval=0.1)
    else:
        body = generator
    response = ReplayStreamingResponse(body, send_timeout=0.01)
    await asyncio.wait_for(response.stream_response(send), 1)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_close_callback_error_still_closes_source():
    closed = asyncio.Event()

    async def source():
        try:
            yield "data: first\n\n"
        finally:
            await asyncio.sleep(0)
            closed.set()

    async def send(message):
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()

    async def on_close():
        raise RuntimeError("close callback failed")

    generator = source()
    response = ReplayStreamingResponse(
        generator,
        send_timeout=0.01,
        on_close=on_close,
    )
    try:
        with pytest.raises(RuntimeError, match="close callback failed"):
            await response.stream_response(send)
        assert closed.is_set()
    finally:
        await generator.aclose()

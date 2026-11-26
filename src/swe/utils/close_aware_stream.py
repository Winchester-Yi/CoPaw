"""Async generator wrapper with cleanup before the first iteration."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any


class CloseAwareStream(AsyncGenerator):
    def __init__(
        self,
        stream: AsyncGenerator,
        close_unstarted: Callable[[], Awaitable[None]],
    ) -> None:
        self._stream = stream
        self._close_unstarted = close_unstarted
        self._started = False
        self._closed = False

    async def asend(self, value: Any) -> Any:
        if value is None:
            self._started = True
        return await self._stream.asend(value)

    async def athrow(self, *args: Any) -> Any:
        if not self._started:
            await self.aclose()
        return await self._stream.athrow(*args)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._stream.aclose()
        finally:
            if not self._started:
                await self._close_unstarted()

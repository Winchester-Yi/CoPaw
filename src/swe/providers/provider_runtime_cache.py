# -*- coding: utf-8 -*-
"""Scope-keyed runtime cache primitives for provider state."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..performance_metrics import timed_work

T = TypeVar("T")


class ProviderRuntimeCache:
    """Cache scope state while coalescing concurrent initialization/refresh."""

    def __init__(self, freshness_ttl_seconds: float = 300.0) -> None:
        self.freshness_ttl_seconds = freshness_ttl_seconds
        self._values: dict[str, Any] = {}
        self._builds: dict[str, asyncio.Future[Any]] = {}
        self._refreshes: dict[str, asyncio.Future[None]] = {}
        self._refresh_generations: dict[str, int] = {}
        self._freshness_due: set[str] = set()
        self._next_freshness_check_at: dict[str, float] = {}
        self._freshness_generation: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._instances: dict[str, Any] = {}
        self._instance_inflight: dict[str, concurrent.futures.Future[Any]] = {}
        self._instance_reset_epoch = 0
        self._instances_lock = threading.Lock()
        self._model_cache_reset: Callable[[str], None] | None = None
        self._init_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=8,
        )

    @property
    def instances(self) -> dict[str, Any]:
        """The shared, scope-keyed synchronous runtime instances."""
        return self._instances

    @property
    def instance_inflight(self) -> dict[str, concurrent.futures.Future[Any]]:
        """The shared startup futures, retained for legacy observers."""
        return self._instance_inflight

    @property
    def instances_lock(self) -> threading.Lock:
        """Lock protecting the shared instance registry."""
        return self._instances_lock

    @property
    def init_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Executor used for synchronous provider runtime startup and refresh."""
        return self._init_executor

    def submit(
        self,
        callback: Callable[..., T],
        *args: Any,
    ) -> concurrent.futures.Future[T]:
        """Run synchronous provider runtime work on the shared executor."""
        return self._init_executor.submit(
            timed_work,
            "provider",
            time.monotonic(),
            callback,
            *args,
        )

    def get_or_start_instance(
        self,
        scope: str,
        build: Callable[[str], T],
    ) -> concurrent.futures.Future[T]:
        """Return one shared startup future for a scope.

        The cache owns both the registry and the executor so callers only need
        to decide whether to wait synchronously or asynchronously.
        """
        with self._instances_lock:
            existing = self._instances.get(scope)
            if existing is not None:
                completed: concurrent.futures.Future[T] = (
                    concurrent.futures.Future()
                )
                completed.set_result(existing)
                return completed
            future = self._instance_inflight.get(scope)
            if future is not None and not future.done():
                return future
            self._instance_inflight.pop(scope, None)
            reset_epoch = self._instance_reset_epoch

            def build_and_cache() -> T:
                created = build(scope)
                with self._instances_lock:
                    if self._instance_reset_epoch != reset_epoch:
                        return created
                    return self._instances.setdefault(scope, created)

            future = self.submit(build_and_cache)
            self._instance_inflight[scope] = future
            return future

    def discard_completed_instance_startup(
        self,
        scope: str,
        future: concurrent.futures.Future[Any],
    ) -> None:
        """Remove one completed startup future without disturbing a retry."""
        if not future.done():
            return
        with self._instances_lock:
            if self._instance_inflight.get(scope) is future:
                self._instance_inflight.pop(scope, None)

    def reset_instances(self) -> None:
        """Clear shared instances and cancel pending startup work."""
        with self._instances_lock:
            self._instance_reset_epoch += 1
            self._instances.clear()
            inflight = list(self._instance_inflight.values())
            self._instance_inflight.clear()
        for future in inflight:
            if not future.done():
                future.cancel()

    async def get_or_create(
        self,
        scope: str,
        build: Callable[[], Awaitable[T]],
    ) -> T:
        async with self._lock:
            if scope in self._values:
                return self._values[scope]
            future = self._builds.get(scope)
            if future is None:
                future = asyncio.ensure_future(build())
                self._builds[scope] = future
        try:
            value = await asyncio.shield(future)
        except BaseException:
            if future.done():
                async with self._lock:
                    if self._builds.get(scope) is future:
                        self._builds.pop(scope, None)
            raise
        async with self._lock:
            self._values[scope] = value
            if self._builds.get(scope) is future:
                self._builds.pop(scope, None)
        return value

    def invalidate(self, scope: str) -> None:
        self._values.pop(scope, None)
        self.mark_freshness_due(scope)

    def mark_freshness_due(self, scope: str) -> None:
        self._freshness_generation[scope] = (
            self._freshness_generation.get(scope, 0) + 1
        )
        self._freshness_due.add(scope)
        self._next_freshness_check_at[scope] = 0.0

    def invalidate_provider_scope(self, scope: str) -> None:
        """Invalidate provider-derived runtime state after a catalog write."""
        self.mark_freshness_due(scope)
        self.reset_scope_bound_model_caches(scope)

    def set_model_cache_reset(
        self,
        reset_model_caches: Callable[[str], None],
    ) -> None:
        """Configure the provider-scope model cache invalidation callback."""
        self._model_cache_reset = reset_model_caches

    def reset_scope_bound_model_caches(self, scope: str) -> None:
        """Clear model instances coupled to one provider scope."""
        if self._model_cache_reset is not None:
            self._model_cache_reset(scope)
            return

        from swe.runtime_cache import reset_scope_bound_model_caches

        reset_scope_bound_model_caches(scope)

    def ensure_freshness_due(self, scope: str) -> None:
        """Schedule a periodic check without invalidating an active refresh."""
        self._freshness_due.add(scope)
        self._next_freshness_check_at.setdefault(scope, 0.0)

    def freshness_check_is_due(self, scope: str) -> bool:
        """Return whether this scope still has a refresh the cache must run."""
        return (
            scope in self._freshness_due
            and time.monotonic()
            >= self._next_freshness_check_at.get(scope, float("inf"))
        )

    def next_freshness_check_at(self, scope: str) -> float:
        """Expose the cache-owned schedule for legacy facade observers."""
        return self._next_freshness_check_at.get(scope, 0.0)

    async def refresh_if_due(
        self,
        scope: str,
        refresh: Callable[[], Awaitable[None] | None],
    ) -> None:
        async with self._lock:
            if (
                scope not in self._freshness_due
                or time.monotonic()
                < self._next_freshness_check_at.get(scope, float("inf"))
            ):
                return
            future = self._refreshes.get(scope)
            if future is None:
                generation = self._freshness_generation.get(scope, 0)
                result = refresh()
                if inspect.isawaitable(result):
                    future = asyncio.ensure_future(result)
                else:
                    future = asyncio.get_running_loop().create_future()
                    future.set_result(None)
                self._refreshes[scope] = future
                self._refresh_generations[scope] = generation
            else:
                generation = self._refresh_generations[scope]
        try:
            await asyncio.shield(future)
        except BaseException:
            if future.done():
                async with self._lock:
                    if self._refreshes.get(scope) is future:
                        self._refreshes.pop(scope, None)
                        self._refresh_generations.pop(scope, None)
            raise
        async with self._lock:
            if self._freshness_generation.get(scope, 0) == generation:
                self._freshness_due.discard(scope)
                self._next_freshness_check_at[scope] = (
                    time.monotonic() + self.freshness_ttl_seconds
                )
            if self._refreshes.get(scope) is future:
                self._refreshes.pop(scope, None)
                self._refresh_generations.pop(scope, None)

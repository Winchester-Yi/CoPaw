# -*- coding: utf-8 -*-
"""MultiAgentManager: Manages multiple agent workspaces with lazy loading.

Provides centralized management for multiple Workspace objects,
including lazy loading, lifecycle management, and hot reloading.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Set, Optional

from .workspace import Workspace
from ..config.utils import (
    get_tenant_storage_config_path,
    load_config,
)

logger = logging.getLogger(__name__)
DEFAULT_WORKSPACE_CACHE_MAX_SIZE = 16
DEFAULT_WORKSPACE_START_MAX_CONCURRENT = 4
DEFAULT_WORKSPACE_IDLE_TTL_SECONDS = 60 * 60
DEFAULT_WORKSPACE_CLEANUP_INTERVAL_SECONDS = 30 * 60


@dataclass
class _WorkspaceCacheEntry:
    workspace: Workspace
    created_at: float
    last_accessed_at: float
    access_sequence: int


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid %s=%r; using default %s",
            name,
            raw_value,
            default,
        )
        return default
    if value <= 0:
        logger.warning(
            "Invalid %s=%r; using default %s",
            name,
            raw_value,
            default,
        )
        return default
    return value


class MultiAgentManager:
    """Manages multiple agent workspaces.

    Features:
    - Lazy loading: Workspaces are created only when first requested
    - Lifecycle management: Start, stop, reload workspaces
    - Thread-safe: Uses async lock for concurrent access
    - Hot reload: Reload individual workspaces without affecting others
    """

    def __init__(
        self,
        *,
        source_system_config_service: object | None = None,
        workflow_config_store: object | None = None,
        continuous_governance_service: object | None = None,
        workspace_cache_max_size: int | None = None,
        workspace_start_max_concurrent: int | None = None,
        workspace_idle_ttl_seconds: int | None = None,
        monotonic_time: Callable[[], float] = time.monotonic,
    ):
        """Initialize multi-agent manager."""
        self.agents: Dict[str, Workspace] = {}
        self._agent_cache_entries: Dict[str, _WorkspaceCacheEntry] = {}
        self._lock = asyncio.Lock()
        self._workspace_cache_eviction_lock = asyncio.Lock()
        self._agent_start_tasks: Dict[str, asyncio.Task[Workspace]] = {}
        self._agent_start_waiters: Dict[str, int] = {}
        self._agent_start_eviction_protected_keys: set[str] = set()
        self.workspace_cache_max_size = (
            workspace_cache_max_size
            if workspace_cache_max_size is not None
            else _get_positive_int_env(
                "SWE_WORKSPACE_CACHE_MAX_SIZE",
                DEFAULT_WORKSPACE_CACHE_MAX_SIZE,
            )
        )
        self.workspace_start_max_concurrent = (
            workspace_start_max_concurrent
            if workspace_start_max_concurrent is not None
            else _get_positive_int_env(
                "SWE_WORKSPACE_START_MAX_CONCURRENT",
                DEFAULT_WORKSPACE_START_MAX_CONCURRENT,
            )
        )
        self.workspace_idle_ttl_seconds = (
            workspace_idle_ttl_seconds
            if workspace_idle_ttl_seconds is not None
            else _get_positive_int_env(
                "SWE_WORKSPACE_IDLE_TTL_SECONDS",
                DEFAULT_WORKSPACE_IDLE_TTL_SECONDS,
            )
        )
        self._workspace_start_limiter = asyncio.Semaphore(
            self.workspace_start_max_concurrent,
        )
        self._monotonic_time = monotonic_time
        self._workspace_access_sequence = 0
        self._workspace_evictions_total = 0
        self._workspace_eviction_stop_failures_total = 0
        self._cleanup_tasks: Set[asyncio.Task] = set()
        self._workspace_cleanup_task: asyncio.Task | None = None
        self._source_system_config_service = source_system_config_service
        self._workflow_config_store = workflow_config_store
        self._continuous_governance_service = continuous_governance_service
        logger.debug("MultiAgentManager initialized")

    def set_source_system_config_service(
        self,
        source_system_config_service: object | None,
    ) -> None:
        """Update the source config service for future workspaces."""
        self._source_system_config_service = source_system_config_service

    def set_workflow_config_store(self, store: object | None) -> None:
        """Supply published workflow bindings to future workspaces."""
        self._workflow_config_store = store

    def set_continuous_governance_service(
        self,
        continuous_governance_service: object | None,
    ) -> None:
        """更新后续工作区使用的持续治理服务。"""
        self._continuous_governance_service = continuous_governance_service

    @staticmethod
    def _cache_key(agent_id: str, tenant_id: Optional[str] = None) -> str:
        """Build a cache key that isolates tenant-local agent runtimes."""
        if tenant_id:
            return f"{tenant_id}:{agent_id}"
        return agent_id

    @staticmethod
    def _load_agent_config_for_tenant(tenant_id: Optional[str] = None):
        """按存储语义加载租户配置，避免请求目录再次回退到 runtime scope。"""
        if tenant_id:
            return load_config(get_tenant_storage_config_path(tenant_id))
        return load_config()

    async def get_agent(
        self,
        agent_id: str,
        tenant_id: Optional[str] = None,
    ) -> Workspace:
        """Get agent workspace by ID (lazy loading).

        If workspace doesn't exist in memory, it will be created and started.
        Thread-safe using async lock.

        Args:
            agent_id: Agent ID to retrieve
            tenant_id: Optional tenant ID for tenant-scoped config lookup

        Returns:
            Workspace: The requested workspace instance

        Raises:
            ValueError: If agent ID not found in configuration
        """
        started_at = time.perf_counter()
        cache_key = self._cache_key(agent_id, tenant_id)
        async with self._lock:
            instance = self.agents.get(cache_key)
            if instance is not None:
                self._touch_cache_entry(cache_key, instance)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                logger.debug(
                    "workspace_cache_hit cache_key=%s duration_ms=%d",
                    cache_key,
                    duration_ms,
                )
                return instance

            start_task = self._agent_start_tasks.get(cache_key)
            if start_task is None:
                start_task = asyncio.create_task(
                    self._start_agent_for_cache_key(
                        cache_key,
                        agent_id,
                        tenant_id,
                    ),
                    name=f"workspace-start-{cache_key}",
                )
                self._agent_start_tasks[cache_key] = start_task
            self._agent_start_waiters[cache_key] = (
                self._agent_start_waiters.get(cache_key, 0) + 1
            )
            self._agent_start_eviction_protected_keys.add(cache_key)

        try:
            instance = await asyncio.shield(start_task)
            await self._evict_workspace_cache(protected_keys={cache_key})
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            logger.debug(
                "workspace_cache_miss cache_key=%s duration_ms=%d",
                cache_key,
                duration_ms,
            )
            return instance
        finally:
            should_retry_capacity_eviction = False
            retry_protected_keys: set[str] = {cache_key}
            should_release_start_eviction_protection = False
            async with self._lock:
                waiters = self._agent_start_waiters.get(cache_key, 0)
                if waiters <= 1:
                    self._agent_start_waiters.pop(cache_key, None)
                    should_release_start_eviction_protection = True
                else:
                    self._agent_start_waiters[cache_key] = waiters - 1
                should_retry_capacity_eviction = (
                    len(self.agents) > self.workspace_cache_max_size
                )
            try:
                if should_retry_capacity_eviction:
                    await self._evict_workspace_cache(
                        protected_keys=retry_protected_keys,
                    )
            finally:
                if should_release_start_eviction_protection:
                    async with self._lock:
                        if self._agent_start_waiters.get(cache_key, 0) <= 0:
                            self._agent_start_eviction_protected_keys.discard(
                                cache_key,
                            )

    async def _start_agent_for_cache_key(
        self,
        cache_key: str,
        agent_id: str,
        tenant_id: Optional[str],
    ) -> Workspace:
        """在全局锁外启动 workspace，并在完成时原子写入缓存。"""
        current_task = asyncio.current_task()
        instance: Workspace | None = None
        instance_cached = False
        try:
            async with self._workspace_start_limiter:
                config = self._load_agent_config_for_tenant(tenant_id)

                if agent_id not in config.agents.profiles:
                    raise ValueError(
                        f"Agent '{agent_id}' not found in configuration. "
                        "Available agents: "
                        f"{list(config.agents.profiles.keys())}",
                    )

                agent_ref = config.agents.profiles[agent_id]

                logger.info(f"Creating new workspace: {cache_key}")
                instance = Workspace(
                    agent_id=agent_id,
                    workspace_dir=agent_ref.workspace_dir,
                    tenant_id=tenant_id,
                    source_system_config_service=(
                        self._source_system_config_service
                    ),
                    workflow_config_store=self._workflow_config_store,
                    continuous_governance_service=(
                        self._continuous_governance_service
                    ),
                )
                await instance.start()
                instance.set_manager(self)
            duplicate_to_stop: Workspace | None = None
            async with self._lock:
                existing = self.agents.get(cache_key)
                if existing is None:
                    self.agents[cache_key] = instance
                    instance_cached = True
                    self._touch_cache_entry(cache_key, instance)
                    result = instance
                else:
                    self._touch_cache_entry(cache_key, existing)
                    duplicate_to_stop = instance
                    result = existing
                if self._agent_start_tasks.get(cache_key) is current_task:
                    self._agent_start_tasks.pop(cache_key, None)

            if duplicate_to_stop is not None:
                await duplicate_to_stop.stop()

            if instance_cached:
                await self._evict_workspace_cache(protected_keys={cache_key})

            logger.info(f"Workspace created and started: {cache_key}")
            return result
        except asyncio.CancelledError:
            if instance is not None and not instance_cached:
                await self._stop_uncached_workspace_start(
                    cache_key,
                    instance,
                    "cancelled workspace start",
                )
            await self._remove_start_task_if_current(cache_key, current_task)
            logger.info("Workspace start cancelled: %s", cache_key)
            raise
        except Exception as e:
            if instance is not None and not instance_cached:
                await self._stop_uncached_workspace_start(
                    cache_key,
                    instance,
                    "workspace after start failure",
                )
            await self._remove_start_task_if_current(cache_key, current_task)
            logger.error(f"Failed to start workspace {cache_key}: {e}")
            raise

    async def _stop_uncached_workspace_start(
        self,
        cache_key: str,
        instance: Workspace,
        reason: str,
        *,
        stop_reused: bool = True,
    ) -> None:
        try:
            await instance.stop(stop_reused=stop_reused)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(
                "Failed to stop %s %s: %s",
                reason,
                cache_key,
                e,
            )

    async def _remove_start_task_if_current(
        self,
        cache_key: str,
        current_task: asyncio.Task | None,
    ) -> None:
        async with self._lock:
            if self._agent_start_tasks.get(cache_key) is current_task:
                self._agent_start_tasks.pop(cache_key, None)

    def _touch_cache_entry(self, cache_key: str, workspace: Workspace) -> None:
        now = self._monotonic_time()
        self._workspace_access_sequence += 1
        access_sequence = self._workspace_access_sequence
        entry = self._agent_cache_entries.get(cache_key)
        if entry is None or entry.workspace is not workspace:
            self._agent_cache_entries[cache_key] = _WorkspaceCacheEntry(
                workspace=workspace,
                created_at=now,
                last_accessed_at=now,
                access_sequence=access_sequence,
            )
            return
        entry.last_accessed_at = now
        entry.access_sequence = access_sequence

    def _workspace_eviction_protected_keys(
        self,
        protected_keys: set[str] | None,
    ) -> set[str]:
        protected = set(protected_keys or set())
        protected.update(self._agent_start_tasks.keys())
        protected.update(self._agent_start_eviction_protected_keys)
        protected.update(
            cache_key
            for cache_key, waiters in self._agent_start_waiters.items()
            if waiters > 0
        )
        return protected

    async def _workspace_has_active_tasks(
        self,
        cache_key: str,
        workspace: Workspace,
    ) -> bool:
        task_tracker = getattr(workspace, "task_tracker", None)
        has_active_tasks = getattr(task_tracker, "has_active_tasks", None)
        if has_active_tasks is None:
            return False
        try:
            return bool(await has_active_tasks())
        except Exception as e:  # pylint: disable=broad-except
            logger.warning(
                "Failed to check active tasks for workspace %s; "
                "skipping eviction: %s",
                cache_key,
                e,
            )
            return True

    async def _evict_workspace_candidates(
        self,
        candidates: list[tuple[str, Workspace, float, int]],
        *,
        protected_keys: set[str] | None,
        max_removals: int | None = None,
        is_still_candidate: (
            Callable[[_WorkspaceCacheEntry, float, int], bool] | None
        ) = None,
    ) -> int:
        removals = 0
        for (
            cache_key,
            workspace,
            last_accessed_at,
            access_sequence,
        ) in candidates:
            if max_removals is not None and removals >= max_removals:
                break
            if await self._workspace_has_active_tasks(cache_key, workspace):
                logger.debug(
                    "Skipping workspace cache eviction with active tasks: %s",
                    cache_key,
                )
                continue

            reserved_entry: _WorkspaceCacheEntry | None = None
            async with self._lock:
                protected = self._workspace_eviction_protected_keys(
                    protected_keys,
                )
                entry = self._agent_cache_entries.get(cache_key)
                if cache_key in protected or entry is None:
                    continue
                if entry.workspace is not workspace:
                    continue
                if self.agents.get(cache_key) is not workspace:
                    continue
                if max_removals is not None:
                    current_overflow = (
                        len(self.agents) - self.workspace_cache_max_size
                    )
                    if current_overflow <= 0:
                        continue
                if is_still_candidate is not None and not is_still_candidate(
                    entry,
                    last_accessed_at,
                    access_sequence,
                ):
                    continue
                reserved_entry = entry
                self.agents.pop(cache_key, None)
                self._agent_cache_entries.pop(cache_key, None)

            try:
                await workspace.stop()
            except asyncio.CancelledError:
                await self._restore_failed_eviction(
                    cache_key,
                    workspace,
                    reserved_entry,
                )
                self._workspace_eviction_stop_failures_total += 1
                logger.warning(
                    "Cancelled while stopping evicted workspace %s",
                    cache_key,
                )
                raise
            except Exception as e:  # pylint: disable=broad-except
                await self._restore_failed_eviction(
                    cache_key,
                    workspace,
                    reserved_entry,
                )
                self._workspace_eviction_stop_failures_total += 1
                logger.warning(
                    "Failed to stop evicted workspace %s: %s",
                    cache_key,
                    e,
                )
                continue

            removals += 1
            self._workspace_evictions_total += 1
            logger.info(
                "Evicted idle workspace cache entry: %s",
                cache_key,
            )
        return removals

    async def _restore_failed_eviction(
        self,
        cache_key: str,
        workspace: Workspace,
        entry: _WorkspaceCacheEntry | None,
    ) -> None:
        async with self._lock:
            if self.agents.get(cache_key) is None:
                self.agents[cache_key] = workspace
            if (
                entry is not None
                and cache_key not in self._agent_cache_entries
            ):
                self._agent_cache_entries[cache_key] = entry

    async def _evict_workspace_cache(
        self,
        *,
        protected_keys: set[str] | None = None,
    ) -> None:
        async with self._workspace_cache_eviction_lock:
            now = self._monotonic_time()
            async with self._lock:
                protected = self._workspace_eviction_protected_keys(
                    protected_keys,
                )
                expired_candidates = sorted(
                    (
                        (
                            cache_key,
                            entry.workspace,
                            entry.last_accessed_at,
                            entry.access_sequence,
                        )
                        for cache_key, entry in (
                            self._agent_cache_entries.items()
                        )
                        if cache_key not in protected
                        and self.agents.get(cache_key) is entry.workspace
                        and now - entry.last_accessed_at
                        > self.workspace_idle_ttl_seconds
                    ),
                    key=lambda item: (item[2], item[3]),
                )

            await self._evict_workspace_candidates(
                expired_candidates,
                protected_keys=protected_keys,
                is_still_candidate=lambda entry, _snapshot_last_accessed_at, _snapshot_access_sequence: (
                    self._monotonic_time() - entry.last_accessed_at
                    > self.workspace_idle_ttl_seconds
                ),
            )

            while True:
                async with self._lock:
                    protected = self._workspace_eviction_protected_keys(
                        protected_keys,
                    )
                    overflow = len(self.agents) - (
                        self.workspace_cache_max_size
                    )
                    if overflow <= 0:
                        return
                    overflow_candidates = sorted(
                        (
                            (
                                cache_key,
                                entry.workspace,
                                entry.last_accessed_at,
                                entry.access_sequence,
                            )
                            for cache_key, entry in (
                                self._agent_cache_entries.items()
                            )
                            if cache_key not in protected
                            and self.agents.get(cache_key) is entry.workspace
                        ),
                        key=lambda item: (item[2], item[3]),
                    )

                if not overflow_candidates:
                    return
                removals = await self._evict_workspace_candidates(
                    overflow_candidates,
                    protected_keys=protected_keys,
                    max_removals=overflow,
                    is_still_candidate=lambda entry, _snapshot_last_accessed_at, snapshot_access_sequence: (
                        entry.access_sequence == snapshot_access_sequence
                    ),
                )
                if removals == 0:
                    return

    async def start_workspace_cleanup_loop(
        self,
        *,
        interval_seconds: float = DEFAULT_WORKSPACE_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        """Start a periodic idle workspace eviction loop."""
        if (
            self._workspace_cleanup_task is not None
            and not self._workspace_cleanup_task.done()
        ):
            return

        async def _cleanup_loop() -> None:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    await self._evict_workspace_cache()
                except Exception:  # pylint: disable=broad-except
                    logger.exception("Workspace cleanup iteration failed")

        self._workspace_cleanup_task = asyncio.create_task(
            _cleanup_loop(),
            name="workspace-cache-cleanup",
        )

    async def stop_workspace_cleanup_loop(self) -> None:
        """Stop the periodic idle workspace eviction loop."""
        task = self._workspace_cleanup_task
        if task is None:
            return
        self._workspace_cleanup_task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def workspace_cache_metrics(self) -> dict[str, int]:
        """Return process-local workspace cache diagnostics."""
        return {
            "workspace_cache_size": len(self.agents),
            "workspace_start_tasks": len(self._agent_start_tasks),
            "workspace_cache_max_size": self.workspace_cache_max_size,
            "workspace_start_max_concurrent": (
                self.workspace_start_max_concurrent
            ),
            "workspace_evictions_total": self._workspace_evictions_total,
            "workspace_eviction_stop_failures_total": (
                self._workspace_eviction_stop_failures_total
            ),
        }

    async def _graceful_stop_old_instance(
        self,
        old_instance: Workspace,
        agent_id: str,
    ) -> None:
        """Gracefully stop old instance after checking for active tasks.

        If active tasks exist, schedule delayed cleanup in background.
        Otherwise, stop immediately.

        Args:
            old_instance: The old workspace instance to stop
            agent_id: Agent ID for logging
        """
        has_active = await old_instance.task_tracker.has_active_tasks()

        if has_active:
            # Active tasks - schedule delayed cleanup in background
            active_tasks = await old_instance.task_tracker.list_active_tasks()
            logger.info(
                f"Old workspace instance has {len(active_tasks)} active "
                f"task(s): {active_tasks}. Scheduling delayed cleanup for "
                f"{agent_id}.",
            )

            async def delayed_cleanup():
                """Wait for tasks to complete, then stop old instance."""
                try:
                    # Wait up to 1 minutes for tasks to complete
                    completed = await old_instance.task_tracker.wait_all_done(
                        timeout=60.0,
                    )
                    if completed:
                        logger.info(
                            f"All tasks completed for old instance "
                            f"{agent_id}. Stopping now.",
                        )
                    else:
                        logger.warning(
                            f"Timeout waiting for tasks to complete for "
                            f"{agent_id}. Forcing stop after 5 minutes.",
                        )

                    await old_instance.stop(final=False)
                    logger.info(
                        f"Old workspace instance stopped: {agent_id}. "
                        f"Delayed cleanup completed.",
                    )
                except asyncio.CancelledError:
                    logger.info(
                        f"Delayed cleanup task for {agent_id} was cancelled. "
                        "Stopping old instance before exit.",
                    )
                    try:
                        await old_instance.stop(final=False)
                    except Exception as e:  # pylint: disable=broad-except
                        logger.warning(
                            f"Failed to stop old workspace instance for "
                            f"{agent_id} after cleanup cancellation: {e}.",
                        )
                    raise
                except Exception as e:
                    logger.warning(
                        f"Error during delayed cleanup for {agent_id}: {e}. "
                        f"New instance is serving requests.",
                    )

            # Create background task for delayed cleanup and track it
            cleanup_task = asyncio.create_task(delayed_cleanup())
            self._cleanup_tasks.add(cleanup_task)

            def _on_cleanup_done(task: asyncio.Task) -> None:
                """Remove task from tracking set and log errors."""
                self._cleanup_tasks.discard(task)
                if task.cancelled():
                    logger.info(
                        f"Delayed cleanup task for {agent_id} was cancelled.",
                    )
                    return
                exc = task.exception()
                if exc is not None:
                    logger.warning(
                        f"Error in delayed cleanup task for {agent_id}: "
                        f"{exc}.",
                    )

            cleanup_task.add_done_callback(_on_cleanup_done)
            logger.info(
                f"Zero-downtime reload completed: {agent_id}. "
                f"Old instance cleanup scheduled in background.",
            )
        else:
            # No active tasks - stop immediately
            logger.debug(
                f"No active tasks in old instance {agent_id}. "
                f"Stopping immediately.",
            )
            try:
                await old_instance.stop(final=False)
                logger.info(
                    f"Old workspace instance stopped: {agent_id}. "
                    f"Zero-downtime reload completed.",
                )
            except Exception as e:
                logger.warning(
                    f"Failed to stop old workspace instance for "
                    f"{agent_id}: {e}. "
                    f"New instance is active and serving requests.",
                )

    async def stop_agent(
        self,
        agent_id: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Stop a specific agent instance.

        Args:
            agent_id: Agent ID to stop

        Returns:
            bool: True if agent was stopped, False if not running
        """
        cache_key = self._cache_key(agent_id, tenant_id)
        cancelled_start = await self._cancel_start_tasks({cache_key})
        async with self._lock:
            if cache_key not in self.agents:
                if cancelled_start:
                    logger.info(
                        "Agent start cancelled and removed: %s",
                        cache_key,
                    )
                    return True
                logger.warning(f"Agent not running: {cache_key}")
                return False

            instance = self.agents[cache_key]
            await instance.stop()
            del self.agents[cache_key]
            self._agent_cache_entries.pop(cache_key, None)
            logger.info(f"Agent stopped and removed: {cache_key}")
            return True

    async def reload_agent(
        self,
        agent_id: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Reload a specific agent instance with zero-downtime.

        This method performs a seamless reload by:
        1. Creating and fully starting a new workspace instance (no lock)
        2. Atomically replacing the old instance with the new one (with lock)
        3. Gracefully stopping the old instance (no lock):
           - If active tasks exist: schedule delayed cleanup in background
           - If no active tasks: stop immediately

        The lock is only held during the atomic swap to minimize blocking
        time for other agent operations.

        This ensures that:
        - New requests are immediately handled by the new instance
        - Ongoing SSE/streaming tasks continue uninterrupted
        - Other agents remain accessible during reload
        - The manager returns quickly without waiting for old tasks
        - Old instance is automatically cleaned up after tasks complete

        Args:
            agent_id: Agent ID to reload

        Returns:
            bool: True if agent was reloaded, False if not running
        """
        cache_key = self._cache_key(agent_id, tenant_id)
        # Step 1: Check if agent exists (quick check with lock)
        async with self._lock:
            if cache_key not in self.agents:
                logger.debug(
                    f"Agent not running, will be loaded on next "
                    f"request: {cache_key}",
                )
                return False
            old_instance = self.agents[cache_key]

        logger.info(f"Reloading agent (zero-downtime): {cache_key}")

        # Step 2: Load configuration (outside lock)
        config = self._load_agent_config_for_tenant(tenant_id)
        if agent_id not in config.agents.profiles:
            logger.error(
                f"Agent '{agent_id}' not found in configuration "
                f"during reload",
            )
            return False

        agent_ref = config.agents.profiles[agent_id]

        # Step 3: Create and start new workspace instance (outside lock)
        # This is the slow part, but doesn't block other agents
        logger.info(f"Creating new workspace instance: {cache_key}")
        new_instance = Workspace(
            agent_id=agent_id,
            workspace_dir=agent_ref.workspace_dir,
            tenant_id=tenant_id,
            source_system_config_service=(self._source_system_config_service),
            workflow_config_store=self._workflow_config_store,
            continuous_governance_service=(
                self._continuous_governance_service
            ),
        )

        # Step 3.5: Set reusable components from old instance (if any)
        async with self._lock:
            old_instance = self.agents.get(cache_key)

        if old_instance:
            # Get all reusable services from old instance's ServiceManager
            # pylint: disable=protected-access
            reusable = old_instance._service_manager.get_reusable_services()
            # pylint: enable=protected-access

            if reusable:
                await new_instance.set_reusable_components(reusable)
                logger.info(
                    f"Set reusable components for {cache_key}: "
                    f"{list(reusable.keys())}",
                )

        new_instance_started = False
        new_instance_swapped = False
        try:
            await new_instance.start()
            new_instance_started = True
            new_instance.set_manager(self)  # Set manager reference
            logger.info(f"New workspace instance started: {cache_key}")
        except asyncio.CancelledError:
            await self._stop_uncached_workspace_start(
                cache_key,
                new_instance,
                "cancelled reload workspace",
                stop_reused=False,
            )
            raise
        except Exception as e:
            logger.exception(
                f"Failed to start new workspace instance for {cache_key}: {e}",
            )
            # Try to clean up the failed new instance
            await self._stop_uncached_workspace_start(
                cache_key,
                new_instance,
                "failed reload workspace",
                stop_reused=False,
            )
            # Old instance is still running and serving requests
            return False

        # Step 4: Atomic swap (minimal lock time)
        # From this point, reload is considered successful
        try:
            async with self._lock:
                # Double-check agent still exists
                if cache_key not in self.agents:
                    logger.warning(
                        f"Agent {cache_key} was removed during reload, "
                        f"stopping new instance",
                    )
                    await new_instance.stop(stop_reused=False)
                    return False

                # Swap instances atomically
                old_instance = self.agents[cache_key]
                self.agents[cache_key] = new_instance
                new_instance_swapped = True
                self._touch_cache_entry(cache_key, new_instance)
                logger.info(f"Workspace instance replaced: {cache_key}")
        except asyncio.CancelledError:
            if new_instance_started and not new_instance_swapped:
                await self._stop_uncached_workspace_start(
                    cache_key,
                    new_instance,
                    "cancelled reload workspace before swap",
                    stop_reused=False,
                )
            raise

        # Step 5: Gracefully stop old instance (outside lock)
        # Delegates to helper method to avoid too-many-statements
        await self._graceful_stop_old_instance(old_instance, agent_id)

        return True

    async def _cancel_start_tasks(
        self,
        cache_keys: set[str] | None = None,
    ) -> int:
        """Cancel workspace starts that have not reached the cache yet."""
        async with self._lock:
            tasks = [
                (cache_key, task)
                for cache_key, task in self._agent_start_tasks.items()
                if cache_keys is None or cache_key in cache_keys
            ]

        if not tasks:
            return 0

        for _cache_key, task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(
            *(task for _cache_key, task in tasks),
            return_exceptions=True,
        )

        async with self._lock:
            for cache_key, task in tasks:
                if self._agent_start_tasks.get(cache_key) is task:
                    self._agent_start_tasks.pop(cache_key, None)
                if self._agent_start_waiters.get(cache_key, 0) <= 0:
                    self._agent_start_waiters.pop(cache_key, None)
                    self._agent_start_eviction_protected_keys.discard(
                        cache_key,
                    )

        return len(tasks)

    async def cancel_all_cleanup_tasks(self) -> None:
        """Cancel and await all pending delayed cleanup tasks.

        This ensures that any in-progress background cleanups are either
        completed or cleanly cancelled before the manager is torn down.
        Called by stop_all() during shutdown.
        """
        if not self._cleanup_tasks:
            return

        logger.info(
            f"Cancelling {len(self._cleanup_tasks)} pending cleanup "
            f"task(s)...",
        )
        tasks = list(self._cleanup_tasks)
        self._cleanup_tasks.clear()

        for task in tasks:
            if not task.done():
                task.cancel()

        # Await completion of all tasks, collecting exceptions
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("All cleanup tasks cancelled/completed")

    async def stop_all(self):
        """Stop all agent instances.

        Called during application shutdown to clean up resources.
        Cancels any pending delayed cleanup tasks and stops all agents.
        """
        logger.info(f"Stopping all agents ({len(self.agents)} running)...")

        await self.stop_workspace_cleanup_loop()
        await self._cancel_start_tasks()

        # Then cancel pending cleanup tasks to avoid orphaned instances
        await self.cancel_all_cleanup_tasks()

        # Create list of agent IDs to avoid modifying dict during iteration
        agent_ids = list(self.agents.keys())

        for agent_id in agent_ids:
            try:
                instance = self.agents[agent_id]
                await instance.stop()
                logger.debug(f"Agent stopped: {agent_id}")
            except Exception as e:
                logger.error(f"Error stopping agent {agent_id}: {e}")

        self.agents.clear()
        self._agent_cache_entries.clear()
        logger.info("All agents stopped")

    def list_loaded_agents(self) -> list[str]:
        """List currently loaded agent IDs.

        Returns:
            list[str]: List of loaded agent IDs
        """
        return list(self.agents.keys())

    def is_agent_loaded(self, agent_id: str) -> bool:
        """Check if agent is currently loaded.

        Args:
            agent_id: Agent ID to check

        Returns:
            bool: True if agent is loaded and running
        """
        return agent_id in self.agents

    async def preload_agent(self, agent_id: str) -> bool:
        """Preload an agent instance during startup.

        Args:
            agent_id: Agent ID to preload

        Returns:
            bool: True if successfully preloaded, False if failed
        """
        try:
            await self.get_agent(agent_id)
            logger.info(f"Successfully preloaded agent: {agent_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to preload agent {agent_id}: {e}")
            return False

    async def start_all_configured_agents(self) -> dict[str, bool]:
        """Start all enabled agents defined in configuration concurrently.

        Only agents with enabled=True will be started.
        Disabled agents are skipped to save resources.

        Returns:
            dict[str, bool]: Mapping of agent_id to success status
        """
        config = load_config()
        # Filter only enabled agents
        enabled_agents = {
            agent_id: ref
            for agent_id, ref in config.agents.profiles.items()
            if getattr(ref, "enabled", True)
        }
        agent_ids = list(enabled_agents.keys())

        if not agent_ids:
            logger.warning("No enabled agents configured in config")
            return {}

        total_agents = len(config.agents.profiles)
        disabled_count = total_agents - len(agent_ids)
        logger.info(
            f"Starting {len(agent_ids)} enabled agent(s) "
            f"({disabled_count} disabled)",
        )

        async def start_single_agent(agent_id: str) -> tuple[str, bool]:
            """Start a single agent with error handling."""
            try:
                logger.info(f"Starting agent: {agent_id}")
                await self.preload_agent(agent_id)
                logger.info(f"Agent started successfully: {agent_id}")
                return (agent_id, True)
            except Exception as e:
                logger.error(
                    f"Failed to start agent {agent_id}: {e}. "
                    f"Continuing with other agents...",
                )
                return (agent_id, False)

        # Start all agents concurrently
        results = await asyncio.gather(
            *[start_single_agent(agent_id) for agent_id in agent_ids],
            return_exceptions=False,
        )

        # Build result mapping
        result_map = dict(results)
        success_count = sum(1 for success in result_map.values() if success)
        logger.info(
            f"Agent startup complete: {success_count}/{len(agent_ids)} "
            f"agents started successfully, {disabled_count} disabled",
        )

        return result_map

    def __repr__(self) -> str:
        """String representation of manager."""
        loaded = list(self.agents.keys())
        return f"MultiAgentManager(loaded_agents={loaded})"

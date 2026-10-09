# -*- coding: utf-8 -*-
"""Tenant workspace pool: registry for tenant-scoped workspace directories.

Provides lazy bootstrap and lifecycle management for tenant workspaces.
Ensures thread-safe access and prevents duplicate concurrent bootstrap.

Note: This pool tracks tenant bootstrap/registry state only. Workspace runtime
creation and startup is handled by MultiAgentManager.get_agent() on demand.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Literal, Optional

from ...config.context import (
    resolve_runtime_identity,
    resolve_storage_tenant_id,
)
from ...runtime_workers import run_runtime_state_work
from .bootstrap_lock import (
    AsyncFlock,
    BootstrapLockFailure,
    BootstrapLockTimeout,
)
from .bootstrap_state import (
    SourceTemplateUnavailable,
    TenantBootstrapUnavailable,
)
from .source_template_provisioner import inspect_source_template_readiness
from .tenant_initializer import TenantInitializer
from .tenant_skill_sync import sync_skills_to_db
from .workspace import Workspace

logger = logging.getLogger(__name__)

DEFAULT_BOOTSTRAP_VALIDATION_TTL_SECONDS = 300.0


@dataclass
class TenantWorkspaceEntry:
    """Entry in the tenant workspace pool.

    Tracks workspace instance and metadata for a tenant.
    """

    tenant_id: str
    workspace: Optional[Workspace] = None
    created_at: float = field(default_factory=time.monotonic)
    last_accessed_at: float = field(default_factory=time.monotonic)
    access_count: int = 0
    bootstrap_ready: bool = False
    bootstrap_generation: int = 0
    validated_at: float = 0.0


@dataclass(frozen=True)
class BootstrapOutcome:
    """Authoritative successful outcome of tenant bootstrap."""

    tenant_id: str
    status: Literal["already_ready", "bootstrapped"]
    duration_ms: int


class TenantWorkspacePool:
    """Pool of tenant workspaces with lazy bootstrap and lifecycle management.

    Each tenant gets their own workspace directory under the base working dir:
        WORKING_DIR/<tenant_id>/

    Features:
    - Minimal bootstrap: Only directory structure and agent declarations
    - Per-tenant locking: Prevents duplicate concurrent bootstrap
    - Access tracking: Tracks last access time and count
    - Registry only: Does NOT create or start workspace runtimes

    Note: Workspace runtime creation and startup is handled by
    MultiAgentManager.get_agent() on demand.
    """

    def __init__(
        self,
        base_working_dir: Path,
        *,
        source_system_config_service: object | None = None,
        workflow_config_store: object | None = None,
        continuous_governance_service: object | None = None,
        bootstrap_validation_ttl_seconds: float = (
            DEFAULT_BOOTSTRAP_VALIDATION_TTL_SECONDS
        ),
    ):
        """Initialize the tenant workspace pool.

        Args:
            base_working_dir: Base directory where tenant workspaces are created.
                Each tenant gets a subdirectory: base_working_dir / tenant_id
        """
        if bootstrap_validation_ttl_seconds < 0:
            raise ValueError(
                "bootstrap_validation_ttl_seconds must be non-negative",
            )

        self._base_working_dir = Path(base_working_dir).expanduser().resolve()
        self._base_working_dir.mkdir(parents=True, exist_ok=True)
        self._source_system_config_service = source_system_config_service
        self._workflow_config_store = workflow_config_store
        self._continuous_governance_service = continuous_governance_service
        self._bootstrap_validation_ttl_seconds = (
            bootstrap_validation_ttl_seconds
        )

        # Tenant workspace registry: tenant_id -> TenantWorkspaceEntry
        self._workspaces: dict[str, TenantWorkspaceEntry] = {}

        # Global lock for registry operations
        self._registry_lock = asyncio.Lock()

        logger.info(
            f"TenantWorkspacePool initialized at {self._base_working_dir}",
        )

    def set_source_system_config_service(
        self,
        source_system_config_service: object | None,
    ) -> None:
        """Update the source config service for future workspaces."""
        self._source_system_config_service = source_system_config_service

    def set_workflow_config_store(self, store: object | None) -> None:
        """Supply published workflow bindings to future tenant workspaces."""
        self._workflow_config_store = store

    @property
    def init_source_store(self):
        """返回 tenant/source 初始化来源存储。"""
        from .tenant_init_source_store import get_tenant_init_source_store

        return get_tenant_init_source_store()

    def set_continuous_governance_service(
        self,
        continuous_governance_service: object | None,
    ) -> None:
        """更新后续工作区使用的持续治理服务。"""
        self._continuous_governance_service = continuous_governance_service

    def _get_tenant_workspace_dir(self, tenant_id: str) -> Path:
        """Get the workspace directory for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            Path to the tenant's workspace directory.
        """
        return self._base_working_dir / tenant_id

    def get_tenant_workspace_dir(self, tenant_id: str) -> Path:
        """Get the workspace directory for a tenant (public).

        This is a public method to compute the tenant's workspace directory
        path without requiring the workspace runtime to be started.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            Path to the tenant's workspace directory.
        """
        return self._get_tenant_workspace_dir(tenant_id)

    def _resolve_bootstrap_tenant_id(
        self,
        tenant_id: str,
        source_id: str | None,
        scope_id: str | None,
    ) -> str:
        """Resolve the bootstrap tenant ID from runtime identity.

        Args:
            tenant_id: The tenant identifier.
            source_id: Optional source identifier.
            scope_id: Optional explicit runtime scope.

        Returns:
            The resolved bootstrap tenant ID.

        Raises:
            ValueError: If scope_id cannot resolve to a canonical scope.
        """
        resolved_tenant_id = resolve_storage_tenant_id(
            tenant_id,
            source_id,
            scope_id=scope_id,
        )
        if resolved_tenant_id is None:
            raise ValueError("tenant_id must resolve to a storage tenant id")
        return resolved_tenant_id

    async def _check_existing_bootstrap(
        self,
        bootstrap_tenant_id: str,
        tenant_id: str,
        source_id: str | None,
        scope_id: str | None,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """Check if tenant already has a complete bootstrap.

        Args:
            bootstrap_tenant_id: The resolved bootstrap tenant ID.
            tenant_id: The original tenant identifier.
            source_id: Optional source identifier.
            scope_id: Optional explicit runtime scope.

        Returns:
            True if already bootstrapped and complete, False otherwise.
        """
        persisted_ready = await run_runtime_state_work(
            self._has_persisted_bootstrap,
            bootstrap_tenant_id,
            tenant_id,
            source_id,
            scope_id,
        )

        async with self._registry_lock:
            entry = self._workspaces.get(bootstrap_tenant_id)
            if (
                expected_generation is not None
                and entry is not None
                and entry.bootstrap_generation != expected_generation
            ):
                return False
            if persisted_ready:
                if entry is None:
                    entry = TenantWorkspaceEntry(
                        tenant_id=bootstrap_tenant_id,
                        bootstrap_generation=expected_generation or 0,
                    )
                    self._workspaces[bootstrap_tenant_id] = entry
                if not entry.bootstrap_ready:
                    entry.bootstrap_ready = True
                entry.validated_at = time.monotonic()
                self._mark_access(entry)
                return True

            if entry is not None:
                entry.bootstrap_ready = False
                entry.validated_at = 0.0
                logger.warning(
                    "Tenant %s cached in pool but scaffold is incomplete. "
                    "Running self-heal bootstrap.",
                    bootstrap_tenant_id,
                )
            return False

    def _has_persisted_bootstrap(
        self,
        bootstrap_tenant_id: str,
        tenant_id: str,
        source_id: str | None,
        scope_id: str | None,
    ) -> bool:
        """Synchronously inspect the persisted bootstrap state."""
        initializer = TenantInitializer(
            self._base_working_dir,
            tenant_id,
            source_id=source_id,
            scope_id=scope_id,
        )
        if source_id and bootstrap_tenant_id == f"default_{source_id}":
            return inspect_source_template_readiness(
                self._base_working_dir,
                source_id,
            ).ready
        return initializer.has_seeded_bootstrap()

    async def _get_ready_entry(
        self,
        bootstrap_tenant_id: str,
    ) -> TenantWorkspaceEntry | None:
        """Return a recently validated ready entry, if one is available."""
        async with self._registry_lock:
            entry = self._workspaces.get(bootstrap_tenant_id)
            if (
                entry is not None
                and entry.bootstrap_ready
                and self._bootstrap_validation_ttl_seconds > 0
                and time.monotonic() - entry.validated_at
                < self._bootstrap_validation_ttl_seconds
            ):
                self._mark_access(entry)
                return entry
            return None

    async def _get_bootstrap_generation(
        self,
        bootstrap_tenant_id: str,
    ) -> int:
        """Return the generation an in-flight bootstrap must publish against."""
        async with self._registry_lock:
            entry = self._workspaces.get(bootstrap_tenant_id)
            return entry.bootstrap_generation if entry is not None else 0

    async def _generation_matches(
        self,
        bootstrap_tenant_id: str,
        expected_generation: int,
    ) -> bool:
        """Check whether an in-flight bootstrap still owns its generation."""
        return expected_generation == await self._get_bootstrap_generation(
            bootstrap_tenant_id,
        )

    async def _mark_bootstrap_ready(
        self,
        bootstrap_tenant_id: str,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """Record a successful bootstrap without creating a runtime."""
        async with self._registry_lock:
            entry = self._workspaces.get(bootstrap_tenant_id)
            if entry is None:
                entry = TenantWorkspaceEntry(
                    tenant_id=bootstrap_tenant_id,
                    bootstrap_generation=expected_generation or 0,
                )
                self._workspaces[bootstrap_tenant_id] = entry
            if (
                expected_generation is not None
                and entry.bootstrap_generation != expected_generation
            ):
                logger.info(
                    "tenant_bootstrap_ready_publish_rejected tenant_id=%s "
                    "expected_generation=%d current_generation=%d",
                    bootstrap_tenant_id,
                    expected_generation,
                    entry.bootstrap_generation,
                )
                return False
            if not entry.bootstrap_ready:
                entry.bootstrap_ready = True
            entry.validated_at = time.monotonic()
            self._mark_access(entry)
            return True

    async def invalidate_bootstrap(
        self,
        bootstrap_tenant_id: str,
        *,
        reason: str,
    ) -> None:
        """Invalidate a ready entry so the next access rechecks persistence."""
        async with self._registry_lock:
            entry = self._workspaces.get(bootstrap_tenant_id)
            if entry is not None:
                entry.bootstrap_ready = False
                entry.validated_at = 0.0
                entry.bootstrap_generation += 1
                logger.info(
                    "tenant_bootstrap_invalidated tenant_id=%s reason=%s",
                    bootstrap_tenant_id,
                    reason,
                )
                return
            entry = TenantWorkspaceEntry(
                tenant_id=bootstrap_tenant_id,
                bootstrap_generation=1,
            )
            self._workspaces[bootstrap_tenant_id] = entry
            logger.info(
                "tenant_bootstrap_invalidation_tombstone tenant_id=%s reason=%s",
                bootstrap_tenant_id,
                reason,
            )

    def _log_seeding_results(
        self,
        tenant_id: str,
        bootstrap_result: dict,
    ) -> None:
        """Log the seeding results from bootstrap.

        Args:
            tenant_id: The tenant identifier.
            bootstrap_result: The result from ensure_seeded_bootstrap().
        """
        pool_seed = bootstrap_result.get("pool_seed", {})
        workspace_seed = bootstrap_result.get("workspace_seed", {})

        if pool_seed.get("seeded"):
            logger.info(
                f"Tenant {tenant_id} skill pool seeded from "
                f"{pool_seed.get('source')}: "
                f"{pool_seed.get('skills', [])}",
            )
        if workspace_seed.get("seeded"):
            logger.info(
                f"Tenant {tenant_id} workspace skills seeded: "
                f"{workspace_seed.get('skills', [])}",
            )

    def _compute_init_source_mapping(
        self,
        tenant_id: str,
        source_id: str | None,
        scope_id: str | None,
        initializer: TenantInitializer,
    ) -> tuple[str, str | None, str]:
        """Compute init source mapping parameters.

        Args:
            tenant_id: The tenant identifier.
            source_id: Optional source identifier.
            scope_id: Optional explicit runtime scope.
            initializer: The TenantInitializer instance.

        Returns:
            Tuple of (logical_tenant_id, resolved_source_id, init_source).
        """
        # init_source records the direct template source
        if tenant_id == "default" and scope_id is None:
            init_source = "default"
        else:
            init_source = initializer.template_name

        # Resolve logical tenant ID for DB mapping
        logical_tenant_id, resolved_source_id, _ = (
            resolve_runtime_identity(scope_id)
            if scope_id is not None
            else resolve_runtime_identity(tenant_id, source_id)
        )
        if logical_tenant_id is None:
            logical_tenant_id = tenant_id

        return logical_tenant_id, resolved_source_id, init_source

    async def _register_tenant_entry(
        self,
        bootstrap_tenant_id: str,
    ) -> TenantWorkspaceEntry:
        """Register or update tenant entry in pool.

        Args:
            bootstrap_tenant_id: The resolved bootstrap tenant ID.

        Returns:
            The tenant workspace entry.
        """
        async with self._registry_lock:
            if bootstrap_tenant_id not in self._workspaces:
                entry = TenantWorkspaceEntry(
                    tenant_id=bootstrap_tenant_id,
                    workspace=None,  # Runtime not started
                )
                self._workspaces[bootstrap_tenant_id] = entry
            else:
                entry = self._workspaces[bootstrap_tenant_id]
            self._mark_access(entry)
            return entry

    async def _perform_bootstrap(
        self,
        bootstrap_tenant_id: str,
        tenant_id: str,
        source_id: str | None,
        scope_id: str | None,
        tenant_name: str | None,
        bbk_id: str | None,
        enable_bootstrap_chat: bool = True,
    ) -> None:
        """Perform the actual bootstrap process.

        Args:
            bootstrap_tenant_id: The resolved bootstrap tenant ID.
            tenant_id: The original tenant identifier.
            source_id: Optional source identifier.
            scope_id: Optional explicit runtime scope.
            tenant_name: Optional tenant name for DB record.
            bbk_id: Optional BBK identifier for DB record.
            enable_bootstrap_chat: Whether to keep BOOTSTRAP.md for first chat.

        Raises:
            TenantBootstrapUnavailable: If bootstrap cannot safely become ready.
        """
        workspace_dir = self._get_tenant_workspace_dir(bootstrap_tenant_id)
        logger.info(
            "Bootstrapping tenant directory: %s at %s",
            bootstrap_tenant_id,
            workspace_dir,
        )

        initializer = TenantInitializer(
            self._base_working_dir,
            tenant_id,
            source_id=source_id,
            scope_id=scope_id,
        )

        try:
            logger.info(
                "tenant_bootstrap_recovery_started tenant_id=%s",
                bootstrap_tenant_id,
            )
            recovery_task = asyncio.create_task(
                asyncio.to_thread(
                    initializer.recover_seeded_bootstrap,
                    enable_bootstrap_chat=enable_bootstrap_chat,
                ),
            )
            try:
                recovery_result = await asyncio.shield(recovery_task)
            except asyncio.CancelledError:
                while not recovery_task.done():
                    try:
                        await asyncio.shield(recovery_task)
                    except asyncio.CancelledError:
                        continue
                await recovery_task
                raise
            logger.info(
                "tenant_bootstrap_recovery_succeeded tenant_id=%s recovered_paths=%d",
                bootstrap_tenant_id,
                len(recovery_result["recovered_paths"]),
            )

            # Record init source mapping
            logical_tenant_id, resolved_source_id, init_source = (
                self._compute_init_source_mapping(
                    tenant_id,
                    source_id,
                    scope_id,
                    initializer,
                )
            )
            await self._record_init_source_mapping(
                logical_tenant_id or tenant_id,
                resolved_source_id or source_id,
                init_source,
                tenant_name=tenant_name,
                bbk_id=bbk_id,
            )

            # Register in pool
            await self._register_tenant_entry(bootstrap_tenant_id)

            logger.info("Tenant bootstrapped: %s", bootstrap_tenant_id)

        except TenantBootstrapUnavailable:
            logger.error(
                "tenant_bootstrap_recovery_failed tenant_id=%s",
                bootstrap_tenant_id,
            )
            raise
        except Exception as e:
            logger.error(
                "tenant_bootstrap_recovery_failed tenant_id=%s error=%s",
                bootstrap_tenant_id,
                type(e).__name__,
            )
            raise TenantBootstrapUnavailable(
                f"tenant bootstrap recovery failed: {bootstrap_tenant_id}",
            ) from e

        # 同步 swe_skills 表（失败仅 warn，不影响 bootstrap 整体成功）
        # 放在 try 块外，独立异常捕获，确保 market 调用失败不影响 tenant 已上线
        try:
            await sync_skills_to_db(bootstrap_tenant_id)
        except Exception as exc:
            logger.warning(
                "swe_skills 同步异常已被吞掉 tenant=%s err=%s",
                bootstrap_tenant_id,
                exc,
            )

    async def ensure_bootstrap(
        self,
        tenant_id: str,
        source_id: str | None = None,
        scope_id: str | None = None,
        tenant_name: str | None = None,
        bbk_id: str | None = None,
        enable_bootstrap_chat: bool = True,
    ) -> BootstrapOutcome:
        """Ensure tenant directory has a complete, strict bootstrap.

        Thread-safe: Uses per-tenant locking to prevent duplicate bootstrap.

        Args:
            tenant_id: The tenant identifier.
            source_id: Optional source identifier from X-Source-Id header.
                Used to select the appropriate default_{source} template.
            scope_id: Optional explicit runtime scope. When set, bootstrap
                state is keyed by this scope instead of re-deriving it.
            tenant_name: Optional tenant/user name for database record.
            bbk_id: Optional BBK identifier for database record.
            enable_bootstrap_chat: Whether to keep BOOTSTRAP.md for first chat.

        Raises:
            RuntimeError: If bootstrap fails.
        """
        started_at = time.perf_counter()

        # Resolve bootstrap tenant ID
        bootstrap_tenant_id = self._resolve_bootstrap_tenant_id(
            tenant_id,
            source_id,
            scope_id,
        )

        def outcome(
            status: Literal["already_ready", "bootstrapped"],
        ) -> BootstrapOutcome:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            return BootstrapOutcome(
                tenant_id=bootstrap_tenant_id,
                status=status,
                duration_ms=duration_ms,
            )

        # Fast path: avoid persisted I/O for a recently validated entry.
        if await self._get_ready_entry(bootstrap_tenant_id):
            result = outcome("already_ready")
            logger.debug(
                "bootstrap_fast_path_hit tenant_id=%s duration_ms=%d",
                bootstrap_tenant_id,
                result.duration_ms,
            )
            return result

        expected_generation = await self._get_bootstrap_generation(
            bootstrap_tenant_id,
        )

        # Strict path: validate persisted state before trusting it.
        if await self._check_existing_bootstrap(
            bootstrap_tenant_id,
            tenant_id,
            source_id,
            scope_id,
            expected_generation=expected_generation,
        ):
            result = outcome("already_ready")
            logger.debug(
                "bootstrap_fast_path_hit tenant_id=%s duration_ms=%d",
                bootstrap_tenant_id,
                result.duration_ms,
            )
            return result
        if not await self._generation_matches(
            bootstrap_tenant_id,
            expected_generation,
        ):
            raise TenantBootstrapUnavailable(
                "tenant bootstrap state changed; retry shortly",
            )

        # Slow path: bootstrap with cross-process file lock.
        try:
            async with AsyncFlock(
                self._get_tenant_workspace_dir(bootstrap_tenant_id)
                / ".bootstrap.lock",
            ):
                # Double-check after acquiring the cross-process lock.
                if await self._get_ready_entry(bootstrap_tenant_id):
                    result = outcome("already_ready")
                    logger.debug(
                        "bootstrap_fast_path_hit tenant_id=%s duration_ms=%d",
                        bootstrap_tenant_id,
                        result.duration_ms,
                    )
                    return result
                if await self._check_existing_bootstrap(
                    bootstrap_tenant_id,
                    tenant_id,
                    source_id,
                    scope_id,
                    expected_generation=expected_generation,
                ):
                    result = outcome("already_ready")
                    logger.debug(
                        "bootstrap_fast_path_hit tenant_id=%s duration_ms=%d",
                        bootstrap_tenant_id,
                        result.duration_ms,
                    )
                    return result
                if not await self._generation_matches(
                    bootstrap_tenant_id,
                    expected_generation,
                ):
                    raise TenantBootstrapUnavailable(
                        "tenant bootstrap state changed; retry shortly",
                    )

                self._require_ready_source_template(source_id)
                logger.info(
                    "tenant_bootstrap_lock_wait tenant_id=%s",
                    bootstrap_tenant_id,
                )
                await self._perform_bootstrap(
                    bootstrap_tenant_id,
                    tenant_id,
                    source_id,
                    scope_id,
                    tenant_name,
                    bbk_id,
                    enable_bootstrap_chat,
                )
                published_ready = await self._mark_bootstrap_ready(
                    bootstrap_tenant_id,
                    expected_generation=expected_generation,
                )
                if not published_ready:
                    raise TenantBootstrapUnavailable(
                        "tenant bootstrap state changed; retry shortly",
                    )
                result = outcome("bootstrapped")
                logger.debug(
                    "bootstrap_fast_path_miss tenant_id=%s duration_ms=%d",
                    bootstrap_tenant_id,
                    result.duration_ms,
                )
                return result
        except BootstrapLockTimeout as exc:
            logger.warning(
                "tenant_bootstrap_lock_timeout tenant_id=%s",
                bootstrap_tenant_id,
            )
            raise TenantBootstrapUnavailable(
                "tenant bootstrap is busy; retry shortly",
            ) from exc
        except BootstrapLockFailure as exc:
            logger.error(
                "tenant_bootstrap_lock_error tenant_id=%s",
                bootstrap_tenant_id,
            )
            raise TenantBootstrapUnavailable(
                "tenant bootstrap lock is unavailable",
            ) from exc

    def _require_ready_source_template(self, source_id: str | None) -> None:
        """Reject normal source-scoped traffic when its template is absent."""
        if not source_id:
            return
        readiness = inspect_source_template_readiness(
            self._base_working_dir,
            source_id,
        )
        if readiness.ready:
            return
        logger.warning(
            "tenant_bootstrap_source_template_not_ready source_id=%s reason=%s",
            source_id,
            readiness.reason,
        )
        raise SourceTemplateUnavailable(
            f"source template default_{source_id} is not ready",
        )

    async def _record_init_source_mapping(
        self,
        tenant_id: str,
        source_id: str | None,
        init_source: str,
        tenant_name: str | None = None,
        bbk_id: str | None = None,
    ) -> None:
        """Record tenant init source mapping to database.

        Args:
            tenant_id: The tenant identifier.
            source_id: The source identifier (from X-Source-Id).
            init_source: The template directory name used for initialization.
            tenant_name: The tenant/user name (optional).
            bbk_id: The BBK identifier (optional).
        """
        try:
            from .tenant_init_source_store import get_tenant_init_source_store

            store = get_tenant_init_source_store()
            if store is None:
                return
            await store.get_or_create(
                tenant_id=tenant_id,
                source_id=source_id or "default",
                init_source=init_source,
                tenant_name=tenant_name,
                bbk_id=bbk_id,
                tenant_type="tenant",
            )
        except Exception as e:
            # Non-fatal: log warning but don't fail bootstrap
            logger.warning(
                f"Failed to record init source mapping for tenant "
                f"{tenant_id}: {e}",
            )

    async def _record_template_init_source_mapping(
        self,
        template_name: str,
        source_id: str,
    ) -> None:
        """Record template init source mapping to database.

        当 default_{source_id} 模板从 default 动态创建时，
        记录映射关系：(default_{source_id}, source_id, default)

        Args:
            template_name: 模板目录名（如 default_ruice）。
            source_id: 来源标识。
        """
        try:
            from .tenant_init_source_store import get_tenant_init_source_store

            store = get_tenant_init_source_store()
            if store is None:
                return
            # 模板条目：tenant_id=模板目录名, source_id=来源, init_source="default"
            await store.get_or_create(
                tenant_id=template_name,
                source_id=source_id,
                init_source="default",
                tenant_name=None,
                bbk_id=None,
                tenant_type="template",
            )
            logger.info(
                f"Recorded template init_source mapping: "
                f"template={template_name}, source={source_id}",
            )
        except Exception as e:
            # Non-fatal: log warning but don't fail bootstrap
            logger.warning(
                f"Failed to record template init source mapping for "
                f"template {template_name}: {e}",
            )

    async def get_or_create(
        self,
        tenant_id: str,
        agent_id: str = "default",
    ) -> Workspace:
        """Get existing workspace or create new one for tenant.

        DEPRECATED: This method is deprecated and no longer provides caching.
        Each call creates a new MultiAgentManager instance, which means:
        - No caching: repeated calls do not guarantee the same workspace instance
        - No lifecycle management: workspaces created via this method are not
          tracked by stop_all() or other pool lifecycle methods

        Use ensure_bootstrap() + MultiAgentManager.get_agent() instead for
        proper lazy loading and caching.

        This method is kept temporarily for backward compatibility but will be
        removed in a future version.

        Args:
            tenant_id: The tenant identifier.
            agent_id: The agent ID to use for the workspace (default: "default").

        Returns:
            Workspace instance for the tenant (already started).

        Raises:
            RuntimeError: If workspace creation or startup fails.
        """
        import warnings

        warnings.warn(
            "TenantWorkspacePool.get_or_create() is deprecated and no longer "
            "provides caching semantics. Use ensure_bootstrap() + "
            "MultiAgentManager.get_agent() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "TenantWorkspacePool.get_or_create() is deprecated for tenant=%s. "
            "Use ensure_bootstrap() + MultiAgentManager.get_agent() instead.",
            tenant_id,
        )

        async with self._registry_lock:
            entry = self._workspaces.get(tenant_id)
            if entry is not None and entry.workspace is not None:
                self._mark_access(entry)
                return entry.workspace

        await self.ensure_bootstrap(tenant_id)

        async with self._registry_lock:
            entry = self._workspaces.get(tenant_id)
            if entry is not None and entry.workspace is not None:
                self._mark_access(entry)
                return entry.workspace

            workspace = Workspace(
                agent_id=agent_id,
                workspace_dir=str(
                    self._get_tenant_workspace_dir(tenant_id)
                    / "workspaces"
                    / agent_id,
                ),
                tenant_id=tenant_id,
                source_system_config_service=(
                    self._source_system_config_service
                ),
                workflow_config_store=self._workflow_config_store,
                continuous_governance_service=(
                    self._continuous_governance_service
                ),
            )

            if entry is None:
                entry = TenantWorkspaceEntry(
                    tenant_id=tenant_id,
                    workspace=workspace,
                )
                self._workspaces[tenant_id] = entry
            else:
                entry.workspace = workspace

            self._mark_access(entry)
            return workspace

    async def get(self, tenant_id: str) -> Optional[Workspace]:
        """Get existing workspace for tenant if it exists.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            Workspace instance if found, None otherwise.
        """
        async with self._registry_lock:
            entry = self._workspaces.get(tenant_id)
            if entry is not None:
                self._mark_access(entry)
                return entry.workspace
            return None

    async def remove(self, tenant_id: str) -> Optional[Workspace]:
        """Remove workspace from pool without stopping it.

        The caller is responsible for stopping the workspace if needed.
        Use stop() for graceful shutdown and removal.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            The removed workspace if it existed, None otherwise.
        """
        async with self._registry_lock:
            entry = self._workspaces.pop(tenant_id, None)
            if entry is not None:
                logger.info(f"Removed workspace from pool: {tenant_id}")
                return entry.workspace
            return None

    async def stop(self, tenant_id: str, final: bool = True) -> bool:
        """Stop and remove workspace for a tenant.

        Args:
            tenant_id: The tenant identifier.
            final: If True, stop all services including reusable ones.

        Returns:
            True if workspace was found and stopped, False otherwise.
        """
        workspace = await self.remove(tenant_id)
        if workspace is None:
            return False

        try:
            # workspace is not None here due to the check above
            await workspace.stop(final=final)  # type: ignore[union-attr]
            logger.info(f"Stopped workspace for tenant: {tenant_id}")
            return True
        except Exception as e:
            logger.error(
                f"Error stopping workspace for tenant {tenant_id}: {e}",
            )
            raise

    async def mark_access(self, tenant_id: str) -> bool:
        """Mark access time for a tenant's workspace.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            True if workspace exists and was marked, False otherwise.
        """
        async with self._registry_lock:
            entry = self._workspaces.get(tenant_id)
            if entry is not None:
                self._mark_access(entry)
                return True
            return False

    def _mark_access(self, entry: TenantWorkspaceEntry) -> None:
        """Update access time and count for an entry (registry lock held).

        Args:
            entry: The tenant workspace entry to mark.
        """
        entry.last_accessed_at = time.monotonic()
        entry.access_count += 1

    async def stop_all(self, final: bool = True) -> None:
        """Stop all workspaces in the pool.

        Note: This only stops workspaces that were registered with a
        non-None workspace instance. Workspaces created by MultiAgentManager
        should be stopped via MultiAgentManager.stop_all().

        Args:
            final: If True, stop all services including reusable ones.
        """
        async with self._registry_lock:
            entries = list(self._workspaces.values())
            self._workspaces.clear()

        if not entries:
            logger.debug("No workspaces to stop")
            return

        # Filter entries that have a workspace instance
        entries_with_workspace = [
            e for e in entries if e.workspace is not None
        ]
        if not entries_with_workspace:
            logger.debug("No workspace instances to stop")
            return

        logger.info(
            f"Stopping {len(entries_with_workspace)} tenant workspaces",
        )

        # Stop all workspaces concurrently
        exceptions = []
        for entry in entries_with_workspace:
            # Skip entries without a workspace instance (shouldn't happen due to filter)
            if entry.workspace is None:
                continue
            try:
                await entry.workspace.stop(final=final)
                logger.debug(f"Stopped workspace: {entry.tenant_id}")
            except Exception as e:
                logger.error(
                    f"Error stopping workspace {entry.tenant_id}: {e}",
                )
                exceptions.append((entry.tenant_id, e))

        if exceptions:
            tenant_ids = [tid for tid, _ in exceptions]
            raise RuntimeError(
                f"Failed to stop workspaces for tenants: {', '.join(tenant_ids)}",
            )

        logger.info("All tenant workspaces stopped")

    async def get_stats(self) -> dict:
        """Get statistics about the pool.

        Returns:
            Dictionary with pool statistics.
        """
        async with self._registry_lock:
            return {
                "tenant_count": len(self._workspaces),
                "tenants": {
                    tenant_id: {
                        "created_at": entry.created_at,
                        "last_accessed_at": entry.last_accessed_at,
                        "access_count": entry.access_count,
                    }
                    for tenant_id, entry in self._workspaces.items()
                },
            }

    def __contains__(self, tenant_id: str) -> bool:
        """Check if a tenant has a workspace in the pool.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            True if the tenant has a workspace, False otherwise.
        """
        return tenant_id in self._workspaces

    def __len__(self) -> int:
        """Return the number of workspaces in the pool.

        Returns:
            Number of tenant workspaces.
        """
        return len(self._workspaces)

    def __repr__(self) -> str:
        """String representation of the pool."""
        count = len(self._workspaces)
        tenants = list(self._workspaces.keys())
        return (
            f"TenantWorkspacePool("
            f"base={self._base_working_dir}, "
            f"tenants={count}, "
            f"ids={tenants}"
            f")"
        )


__all__ = [
    "TenantWorkspacePool",
    "TenantWorkspaceEntry",
]

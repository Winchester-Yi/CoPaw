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
from typing import Optional

from .tenant_initializer import TenantInitializer
from .workspace import Workspace

logger = logging.getLogger(__name__)


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

    def __init__(self, base_working_dir: Path):
        """Initialize the tenant workspace pool.

        Args:
            base_working_dir: Base directory where tenant workspaces are created.
                Each tenant gets a subdirectory: base_working_dir / tenant_id
        """
        self._base_working_dir = Path(base_working_dir).expanduser().resolve()
        self._base_working_dir.mkdir(parents=True, exist_ok=True)

        # Tenant workspace registry: tenant_id -> TenantWorkspaceEntry
        self._workspaces: dict[str, TenantWorkspaceEntry] = {}

        # Per-tenant bootstrap locks to prevent duplicate concurrent bootstrap
        self._bootstrap_locks: dict[str, asyncio.Lock] = {}

        # Global lock for registry operations
        self._registry_lock = asyncio.Lock()

        logger.info(
            f"TenantWorkspacePool initialized at {self._base_working_dir}",
        )

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

    async def _get_or_create_bootstrap_lock(
        self,
        tenant_id: str,
    ) -> asyncio.Lock:
        """Get or create a bootstrap lock for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            Lock for the tenant's bootstrap.
        """
        async with self._registry_lock:
            if tenant_id not in self._bootstrap_locks:
                self._bootstrap_locks[tenant_id] = asyncio.Lock()
            return self._bootstrap_locks[tenant_id]

    async def ensure_bootstrap(self, tenant_id: str) -> None:
        """Ensure tenant directory is bootstrapped (minimal).

        Thread-safe: Uses per-tenant locking to prevent duplicate bootstrap.

        Args:
            tenant_id: The tenant identifier.

        Raises:
            RuntimeError: If bootstrap fails.
        """
        # Fast path: check if already bootstrapped
        async with self._registry_lock:
            entry = self._workspaces.get(tenant_id)
            if entry is not None:
                initializer = TenantInitializer(
                    self._base_working_dir,
                    tenant_id,
                )
                if initializer.has_seeded_bootstrap():
                    self._mark_access(entry)
                    return
                logger.warning(
                    "Tenant %s cached in pool but scaffold is incomplete. "
                    "Running self-heal bootstrap.",
                    tenant_id,
                )

        # Slow path: need to bootstrap or self-heal (with per-tenant lock)
        bootstrap_lock = await self._get_or_create_bootstrap_lock(tenant_id)
        async with bootstrap_lock:
            # Double-check after acquiring lock
            async with self._registry_lock:
                entry = self._workspaces.get(tenant_id)
            initializer = TenantInitializer(
                self._base_working_dir,
                tenant_id,
            )
            if entry is not None and initializer.has_seeded_bootstrap():
                self._mark_access(entry)
                return

            # Perform seeded bootstrap (outside registry lock to avoid blocking)
            workspace_dir = self._get_tenant_workspace_dir(tenant_id)
            logger.info(
                f"Bootstrapping tenant directory: {tenant_id} at {workspace_dir}",
            )

            try:
                # Bootstrap tenant with seeded skills (no QA agent, no runtime start)
                bootstrap_result = initializer.ensure_seeded_bootstrap()

                # Log seeding results
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

                # Register in pool (no workspace runtime created)
                async with self._registry_lock:
                    if tenant_id not in self._workspaces:
                        entry = TenantWorkspaceEntry(
                            tenant_id=tenant_id,
                            workspace=None,  # Runtime not started
                        )
                        self._workspaces[tenant_id] = entry
                    else:
                        entry = self._workspaces[tenant_id]
                    self._mark_access(entry)

                logger.info(f"Tenant bootstrapped: {tenant_id}")

            except Exception as e:
                logger.error(
                    f"Failed to bootstrap tenant {tenant_id}: {e}",
                )
                raise RuntimeError(
                    f"Failed to bootstrap tenant {tenant_id}: {e}",
                ) from e

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

        from ..multi_agent_manager import MultiAgentManager

        # Ensure tenant is bootstrapped first
        await self.ensure_bootstrap(tenant_id)

        # Delegate workspace creation and startup to MultiAgentManager
        # Note: This creates a new MultiAgentManager instance each time,
        # which breaks caching semantics. This is why the method is deprecated.
        multi_agent_manager = MultiAgentManager()
        return await multi_agent_manager.get_agent(
            agent_id,
            tenant_id=tenant_id,
        )

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

# -*- coding: utf-8 -*-
"""Configuration manager for tenant model configurations."""

import json
import threading
from pathlib import Path
from typing import Optional

from swe.constant import SECRET_DIR
from swe.tenant_models.exceptions import TenantModelNotFoundError
from swe.tenant_models.models import TenantModelConfig


class TenantModelManager:
    """Manages tenant model configurations with caching support.

    This class provides methods to load, save, and manage tenant-specific
    model configurations. It supports caching to avoid repeated file I/O
    and provides automatic fallback to a "default" tenant configuration.

    Attributes:
        _cache: Class-level dictionary storing cached configurations.
        _lock: Thread-safe lock for cache access.
    """

    _cache: dict[str, TenantModelConfig] = {}
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get_config_path(cls, tenant_id: str) -> Path:
        """Get the configuration file path for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            Path to the tenant's configuration file.
            Path format: ~/.swe.secret/{tenant_id}/tenant_models.json
        """
        return SECRET_DIR / tenant_id / "tenant_models.json"

    @classmethod
    def load(cls, tenant_id: str) -> TenantModelConfig:
        """Load a tenant's configuration with caching.

        If the configuration for the specified tenant doesn't exist,
        this method falls back to the "default" tenant configuration.
        If neither exists, raises TenantModelNotFoundError.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            The tenant's configuration.

        Raises:
            TenantModelNotFoundError: If neither the tenant's config nor
                the default config exists.
        """
        # Check cache first (with lock for thread safety)
        with cls._lock:
            if tenant_id in cls._cache:
                return cls._cache[tenant_id]

        # Try to load the tenant's config
        config_path = cls.get_config_path(tenant_id)
        if config_path.exists():
            config = cls._load_from_file(config_path)
            with cls._lock:
                cls._cache[tenant_id] = config
            return config

        # Fall back to default tenant
        if tenant_id != "default":
            default_path = cls.get_config_path("default")
            if default_path.exists():
                config = cls._load_from_file(default_path)
                with cls._lock:
                    cls._cache[tenant_id] = config
                return config

        # Neither tenant nor default exists
        raise TenantModelNotFoundError(tenant_id)

    @classmethod
    def save(cls, tenant_id: str, config: TenantModelConfig) -> None:
        """Save a tenant's configuration.

        Creates the tenant directory if it doesn't exist and writes
        the configuration to a JSON file. Also updates the cache.

        Args:
            tenant_id: The tenant identifier.
            config: The configuration to save.
        """
        config_path = cls.get_config_path(tenant_id)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config.model_dump_json(indent=2))

        # Update cache (with lock for thread safety)
        with cls._lock:
            cls._cache[tenant_id] = config

    @classmethod
    def invalidate_cache(cls, tenant_id: Optional[str] = None) -> None:
        """Invalidate the configuration cache.

        Args:
            tenant_id: If provided, invalidate cache for this specific tenant.
                      If None, invalidate the entire cache.
        """
        with cls._lock:
            if tenant_id is None:
                cls._cache.clear()
            else:
                cls._cache.pop(tenant_id, None)

    @classmethod
    def exists(cls, tenant_id: str) -> bool:
        """Check if a configuration exists for a tenant.

        Args:
            tenant_id: The tenant identifier.

        Returns:
            True if the configuration file exists, False otherwise.
        """
        return cls.get_config_path(tenant_id).exists()

    @classmethod
    def _load_from_file(cls, config_path: Path) -> TenantModelConfig:
        """Load configuration from a file.

        Args:
            config_path: Path to the configuration file.

        Returns:
            The loaded configuration.

        Raises:
            ValidationError: If the configuration is invalid.
            JSONDecodeError: If the file contains invalid JSON.
        """
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        return TenantModelConfig(**data)

# -*- coding: utf-8 -*-
"""Exception classes for tenant model configuration management."""


class TenantModelError(RuntimeError):
    """Base exception for tenant model configuration errors."""


class TenantModelNotFoundError(TenantModelError):
    """Raised when tenant model configuration is not found."""

    def __init__(self, tenant_id: str):
        """
        Initialize TenantModelNotFoundError.

        Args:
            tenant_id: The tenant ID for which configuration was not found.
        """
        self.tenant_id = tenant_id
        super().__init__(
            f"Tenant model config not found for tenant: {tenant_id}",
        )


class TenantModelProviderError(TenantModelError):
    """Raised when a provider instance cannot be created."""

    def __init__(self, provider_id: str, reason: str):
        """
        Initialize TenantModelProviderError.

        Args:
            provider_id: The provider ID that failed to instantiate.
            reason: The reason for the failure.
        """
        self.provider_id = provider_id
        super().__init__(
            f"Failed to instantiate provider '{provider_id}': {reason}",
        )


class TenantModelValidationError(TenantModelError):
    """Raised when tenant model configuration validation fails."""

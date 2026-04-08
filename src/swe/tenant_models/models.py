# -*- coding: utf-8 -*-
"""Data models for tenant model configuration."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class TenantProviderConfig(BaseModel):
    """Configuration for a model provider within a tenant.

    Attributes:
        id: Unique identifier for this provider configuration.
        type: Type of provider (openai, anthropic, ollama).
        api_key: API key for the provider, supports ${ENV:XXX} format.
        base_url: Optional base URL for the provider API.
        models: List of model names available through this provider.
        enabled: Whether this provider is enabled.
        extra: Additional provider-specific configuration.
    """

    id: str
    type: Literal["openai", "anthropic", "ollama"]
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    models: List[str] = Field(default_factory=list)
    enabled: bool = True
    extra: Dict[str, Any] = Field(default_factory=dict)


class ModelSlot(BaseModel):
    """Represents a model slot in the routing configuration.

    A slot maps to a specific model from a specific provider.

    Attributes:
        provider_id: ID of the provider to use.
        model: Name of the model to use.
    """

    provider_id: str
    model: str


class RoutingConfig(BaseModel):
    """Routing configuration for model selection.

    Attributes:
        mode: Routing mode (local_first or cloud_first).
        slots: Dictionary of named model slots.
    """

    mode: Literal["local_first", "cloud_first"]
    slots: Dict[str, ModelSlot]


class TenantModelConfig(BaseModel):
    """Root model for tenant model configuration.

    Attributes:
        version: Configuration version.
        providers: List of provider configurations.
        routing: Routing configuration for model selection.
    """

    version: str = "1.0"
    providers: List[TenantProviderConfig]
    routing: RoutingConfig

    def get_active_slot(self) -> ModelSlot:
        """Get the active model slot from routing configuration.

        Returns:
            The active ModelSlot based on routing mode.

        Raises:
            KeyError: If the active slot is not found in routing configuration.
        """
        slot_key = self.routing.mode.replace("_first", "")
        return self.routing.slots[slot_key]

    def get_other_slot(self) -> ModelSlot:
        """Get the other (fallback) model slot from routing configuration.

        Returns:
            The fallback ModelSlot.

        Raises:
            KeyError: If the fallback slot is not found in routing configuration.
        """
        current_key = self.routing.mode.replace("_first", "")
        other_key = "cloud" if current_key == "local" else "local"
        return self.routing.slots[other_key]

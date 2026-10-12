# -*- coding: utf-8 -*-
"""Choose a worker model from the current tenant's configured catalog."""

import json
import logging

from .client import JevClient
from .config import JevConfig
from ..providers.models import ModelSlotConfig

logger = logging.getLogger(__name__)


async def select_subagent_model(
    config: JevConfig | None,
    tenant_id: str,
    definition,
    task: dict,
) -> ModelSlotConfig | None:
    if config is None or not config.subagent_model_selection.enabled:
        return None
    metadata = definition.skill_owned or definition.agent_owned
    if metadata is not None and metadata.model is not None:
        return None
    from ..providers.provider_manager import ProviderManager

    manager = ProviderManager.get_instance(tenant_id)
    slots = {}
    criteria = {}
    providers = {
        **manager.builtin_providers,
        **manager.custom_providers,
    }
    seen = set()
    for provider in providers.values():
        # Unconfigured built-in provider catalogs are not routable.
        if provider.require_api_key and not provider.api_key:
            continue
        for model in provider.models + provider.extra_models:
            identity = (provider.id, model.id)
            if identity in seen:
                continue
            seen.add(identity)
            key = f"model_{len(slots)}"
            slots[key] = ModelSlotConfig(
                provider_id=provider.id,
                model=model.id,
            )
            criteria[key] = f"{provider.name}: {model.name} ({model.id})"
    if not slots:
        return None
    if len(slots) == 1:
        selected = next(iter(slots))
    else:
        selected = await JevClient(config).choose_model(
            json.dumps(task, ensure_ascii=False),
            criteria,
        )
    slot = slots.get(selected)
    logger.info(
        "jev_model candidates=%d selected=%s",
        len(slots),
        slot.model_dump() if slot else None,
    )
    return slot

"""Resolve workflow dispatch pools from published shared configuration."""

from __future__ import annotations

import json
from typing import Any


async def resolve_workflow_batch_jobs(
    jobs: list[dict[str, Any]],
    db: Any,
) -> None:
    """Freeze workflow version and model before intent creation."""
    workflow_jobs = [job for job in jobs if job.get("task_type") == "workflow"]
    if not workflow_jobs:
        return
    binding_ids = sorted(
        {str(job.get("workflow_binding_id") or "") for job in workflow_jobs},
    )
    if not binding_ids or not binding_ids[0]:
        raise RuntimeError("workflow job requires a binding")
    rows = await db.fetch_all(
        """SELECT b.binding_id, b.source_id, b.current_version, b.enabled,
                  v.config_json
           FROM swe_workflow_bindings b
           JOIN swe_workflow_binding_versions v
             ON v.binding_id = b.binding_id
            AND v.version = b.current_version
           WHERE b.binding_id IN ("""
        + ",".join(["%s"] * len(binding_ids))
        + ")",
        tuple(binding_ids),
    )
    bindings = {str(row["binding_id"]): row for row in rows}
    for job in workflow_jobs:
        binding_id = str(job["workflow_binding_id"])
        binding = bindings.get(binding_id)
        if (
            binding is None
            or not binding.get("enabled")
            or str(binding.get("source_id") or "")
            != str(job.get("source_id") or "")
        ):
            raise RuntimeError(f"workflow binding unavailable: {binding_id}")
        raw_config = binding["config_json"]
        if isinstance(raw_config, bytes):
            raw_config = raw_config.decode("utf-8")
        config = (
            json.loads(raw_config)
            if isinstance(raw_config, str)
            else raw_config
        )
        if not isinstance(config, dict):
            raise RuntimeError("workflow binding config is invalid")
        provider_id = str(config.get("provider_id") or "").strip()
        model_id = str(config.get("model_id") or "").strip()
        if not provider_id or not model_id:
            raise RuntimeError("workflow binding dispatch model is missing")
        job["provider_id"] = provider_id
        job["model_id"] = model_id
        job["workflow_config_version"] = int(binding["current_version"])

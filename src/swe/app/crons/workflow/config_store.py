"""Shared, versioned workflow binding storage for SWE and Scheduler."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from .models import WorkflowConfig
from .engine import (
    allowed_workflow_hosts,
    ensure_workflow_host_approved,
    is_registered_workflow_renderer,
)

CREATE_BINDINGS_TABLE = """
CREATE TABLE IF NOT EXISTS swe_workflow_bindings (
    binding_id VARCHAR(64) PRIMARY KEY,
    source_id VARCHAR(64) NOT NULL,
    skill_id VARCHAR(200) NOT NULL,
    current_version INT NOT NULL DEFAULT 0,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_workflow_source_skill (source_id, skill_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

CREATE_VERSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS swe_workflow_binding_versions (
    binding_id VARCHAR(64) NOT NULL,
    version INT NOT NULL,
    skill_id VARCHAR(200) NOT NULL,
    config_json TEXT NOT NULL,
    published_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (binding_id, version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


class WorkflowConfigStore:
    """Read published versions and atomically publish new versions."""

    def __init__(
        self,
        db: Any | None,
        *,
        allowed_hosts: set[str] | None = None,
    ):
        self._db = db
        self._allowed_hosts = allowed_workflow_hosts(allowed_hosts)

    def _require_db(self) -> Any:
        if self._db is None or not getattr(self._db, "is_connected", False):
            raise RuntimeError("workflow configuration database unavailable")
        return self._db

    async def ensure_schema(self) -> None:
        db = self._require_db()
        await db.execute(CREATE_BINDINGS_TABLE)
        await db.execute(CREATE_VERSIONS_TABLE)

    async def resolve_first_skill(
        self,
        source_id: str,
        skill_ids: str,
    ) -> WorkflowConfig:
        first_skill_id = next(
            (item.strip() for item in skill_ids.split(",") if item.strip()),
            "",
        )
        if not first_skill_id:
            raise ValueError("workflow requires a first skill ID")
        binding = await self._require_db().fetch_one(
            """SELECT binding_id, source_id, skill_id, current_version, enabled
               FROM swe_workflow_bindings
               WHERE source_id = %s AND skill_id = %s""",
            (source_id, first_skill_id),
        )
        if not binding or not binding.get("enabled"):
            raise ValueError("workflow binding is missing or disabled")
        return await self.get_version(
            str(binding["binding_id"]),
            int(binding["current_version"]),
            source_id=source_id,
        )

    async def get_current(
        self,
        binding_id: str,
        *,
        source_id: str | None = None,
    ) -> WorkflowConfig:
        binding = await self._require_db().fetch_one(
            """SELECT binding_id, source_id, current_version, enabled
               FROM swe_workflow_bindings WHERE binding_id = %s""",
            (binding_id,),
        )
        if not binding or not binding.get("enabled"):
            raise ValueError("workflow binding is missing or disabled")
        if source_id is not None and binding["source_id"] != source_id:
            raise ValueError("workflow binding source mismatch")
        return await self.get_version(
            binding_id,
            int(binding["current_version"]),
            source_id=source_id,
        )

    async def get_version(
        self,
        binding_id: str,
        version: int,
        *,
        source_id: str | None = None,
    ) -> WorkflowConfig:
        if version < 1:
            raise ValueError("workflow version must be positive")
        row = await self._require_db().fetch_one(
            """SELECT v.binding_id, v.version, v.skill_id, v.config_json,
                      b.source_id, b.enabled
               FROM swe_workflow_binding_versions v
               JOIN swe_workflow_bindings b ON b.binding_id = v.binding_id
               WHERE v.binding_id = %s AND v.version = %s""",
            (binding_id, version),
        )
        if row is None:
            raise ValueError("workflow binding version not found")
        if not row.get("enabled"):
            raise ValueError("workflow binding is disabled")
        if source_id is not None and row["source_id"] != source_id:
            raise ValueError("workflow binding source mismatch")
        raw_config = row["config_json"]
        config_data = (
            json.loads(raw_config)
            if isinstance(raw_config, str)
            else raw_config
        )
        if not isinstance(config_data, dict):
            raise ValueError(
                "workflow binding configuration must be an object"
            )
        return WorkflowConfig.model_validate(
            {
                **config_data,
                "binding_id": row["binding_id"],
                "version": row["version"],
                "skill_id": row["skill_id"],
            },
        )

    async def publish(
        self,
        *,
        source_id: str,
        skill_id: str,
        definition: dict[str, Any],
    ) -> WorkflowConfig:
        """Publish an immutable version and move the binding pointer."""
        if not source_id or not skill_id:
            raise ValueError("workflow source and skill are required")
        db = self._require_db()
        async with db.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """INSERT IGNORE INTO swe_workflow_bindings
                           (binding_id, source_id, skill_id, current_version)
                           VALUES (%s, %s, %s, 0)""",
                        (str(uuid4()), source_id, skill_id),
                    )
                    await cursor.execute(
                        """SELECT binding_id, current_version
                           FROM swe_workflow_bindings
                           WHERE source_id = %s AND skill_id = %s
                           FOR UPDATE""",
                        (source_id, skill_id),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise RuntimeError(
                            "workflow binding insert was not visible"
                        )
                    binding_id = str(row[0])
                    version = int(row[1]) + 1
                    config = WorkflowConfig.model_validate(
                        {
                            **definition,
                            "binding_id": binding_id,
                            "version": version,
                            "skill_id": skill_id,
                        },
                    )
                    ensure_workflow_host_approved(
                        config.url,
                        self._allowed_hosts,
                    )
                    if not is_registered_workflow_renderer(
                        config.renderer_key,
                    ):
                        raise ValueError("workflow renderer is not registered")
                    config_text = config.model_dump_json(
                        exclude={"binding_id", "version", "skill_id"},
                    )
                    if len(config_text.encode("utf-8")) > 60_000:
                        raise ValueError(
                            "workflow binding configuration is too large"
                        )
                    await cursor.execute(
                        """INSERT INTO swe_workflow_binding_versions
                           (binding_id, version, skill_id, config_json)
                           VALUES (%s, %s, %s, %s)""",
                        (
                            binding_id,
                            version,
                            skill_id,
                            config_text,
                        ),
                    )
                    await cursor.execute(
                        """UPDATE swe_workflow_bindings
                           SET current_version = %s, enabled = 1
                           WHERE binding_id = %s""",
                        (version, binding_id),
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return config

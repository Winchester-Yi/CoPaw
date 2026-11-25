# -*- coding: utf-8 -*-
"""Installed scope mappings inject SQL storage into original workspaces."""

import os

from .chat_mutations import load_metadata_json
from .paths import existing_root
from .identity import WorkspaceScope, service_unit_bytes, service_unit_from_env

AUTHORITY_ENV = "SWE_CONVERSATION_STORAGE_AUTHORITY"
METADATA_AUTHORITY = "mysql-metadata"
_metadata_database = None


def resolved_scope(workspace, installed=None, *, service_unit_id):
    """Use the trusted runtime identity, never filesystem directory encodings."""
    from ...config.context import canonicalize_scope_id, decode_scope_id

    service_unit_bytes(service_unit_id)

    explicit = getattr(workspace, "conversation_scope", None)
    if explicit is not None:
        if (
            not isinstance(explicit, WorkspaceScope)
            or explicit.agent_id != workspace.agent_id
            or explicit.service_unit_id != service_unit_id
        ):
            raise ValueError("workspace conversation service scope is invalid")
        return explicit
    tenant = getattr(workspace, "tenant_id", None)
    source = getattr(workspace, "source_id", None)
    if tenant is None:
        if (
            installed is not None
            and installed.service_unit_id == service_unit_id
            and installed.tenant_id == "default"
            and installed.agent_id == workspace.agent_id
            and source in (None, installed.source_id)
        ):
            return installed
        if source is None:
            raise ValueError(
                "workspace requires a resolved conversation scope",
            )
        tenant = "default"
    try:
        canonicalize_scope_id(tenant)
        logical, decoded_source = decode_scope_id(tenant)
    except ValueError:
        if tenant.startswith("default_"):
            logical, decoded_source = "default", tenant[len("default_") :]
        elif source is not None:
            logical, decoded_source = tenant, source
        else:
            raise ValueError(
                "workspace requires a resolved conversation scope",
            ) from None
    if source is not None and source != decoded_source:
        raise ValueError("workspace conversation source scope mismatch")
    return WorkspaceScope(
        logical,
        decoded_source,
        workspace.agent_id,
        service_unit_id=service_unit_id,
    )


def require_empty_conversation(root):
    """A normal new workspace may join SQL; omitted old data requires migration."""
    for name in ("sessions", "dialog"):
        path = root / name
        if path.is_symlink() or (
            path.exists()
            and (not path.is_dir() or next(path.iterdir(), None) is not None)
        ):
            raise ValueError("workspace conversation data requires migration")
    registry = root / "chats.json"
    if registry.is_symlink():
        raise ValueError("workspace conversation registry requires migration")
    if registry.exists():
        if not registry.is_file() or registry.stat().st_size > 1024 * 1024:
            raise ValueError(
                "workspace conversation registry requires migration",
            )
        document = load_metadata_json(registry.read_bytes())
        if (
            document != {"version": 1, "chats": []}
            or type(document["version"]) is not int
        ):
            raise ValueError(
                "workspace conversation registry requires migration",
            )


def storage_authority():
    authority = os.environ.get(AUTHORITY_ENV, "legacy")
    if authority not in ("legacy", METADATA_AUTHORITY):
        raise ValueError(
            "invalid conversation storage authority; use legacy or mysql-metadata",
        )
    return authority


async def configure_runtime(database):
    reset_runtime()
    authority = storage_authority()
    if authority == "legacy":
        return
    if database is None or not database.is_connected:
        raise RuntimeError(
            "conversation storage requires a connected database",
        )
    if authority == METADATA_AUTHORITY:
        await _configure_metadata_runtime(database)
        return


async def _configure_metadata_runtime(database):
    global _metadata_database
    from .metadata_repository import WORKSPACES

    service_unit_from_env()
    await database.fetch_all(
        f"SELECT workspace_root FROM {WORKSPACES} LIMIT 0",
    )
    await database.fetch_all(
        "SELECT nas_filename FROM swe_conversation_sessions LIMIT 0",
    )
    await database.fetch_all(
        "SELECT extended_metadata_text,resources_cleaned_at FROM swe_conversation_chats LIMIT 0",
    )
    _metadata_database = database


async def workspace_metadata_repository(workspace):
    if storage_authority() != METADATA_AUTHORITY:
        return None
    from .metadata_repository import (
        MySQLMetadataRepository,
        WORKSPACES,
        SCOPE_PREDICATE,
    )
    from .tenant_admission import TenantNotPublished

    if _metadata_database is None:
        raise RuntimeError("conversation metadata authority is not ready")
    root = existing_root(workspace.workspace_dir).resolve(strict=True)
    service_unit_id = service_unit_from_env()
    root_key = str(root).encode()
    installed = None
    if (
        getattr(workspace, "tenant_id", None) is None
        and getattr(workspace, "source_id", None) is None
    ):
        row = await _metadata_database.fetch_one(
            f"SELECT service_unit_key,tenant_key,source_key,agent_key FROM {WORKSPACES} "
            "WHERE service_unit_key=%s AND workspace_root=%s",
            (service_unit_bytes(service_unit_id), root_key),
        )
        if row is not None:
            installed = WorkspaceScope.from_row(row)
    scope = resolved_scope(
        workspace,
        installed,
        service_unit_id=service_unit_id,
    )
    binding = await _metadata_database.fetch_one(
        f"SELECT workspace_root FROM {WORKSPACES} WHERE " + SCOPE_PREDICATE,
        scope.sql_values,
    )
    if binding is None:
        require_empty_conversation(root)
        await _metadata_database.execute(
            f"INSERT INTO {WORKSPACES} "
            "(service_unit_key,tenant_key,source_key,agent_key,workspace_root,registry_digest) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE workspace_root=workspace_root",
            (*scope.sql_values, root_key, bytes(32)),
        )
        binding = await _metadata_database.fetch_one(
            f"SELECT workspace_root FROM {WORKSPACES} WHERE "
            + SCOPE_PREDICATE,
            scope.sql_values,
        )
    if binding is None or binding["workspace_root"] != root_key:
        raise TenantNotPublished()
    return MySQLMetadataRepository(_metadata_database, scope)


def reset_runtime():
    """Discard process adapters when their application database shuts down."""
    global _metadata_database
    _metadata_database = None

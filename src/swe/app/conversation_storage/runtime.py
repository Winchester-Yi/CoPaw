# -*- coding: utf-8 -*-
"""Installed scope mappings inject SQL storage into original workspaces."""

import os
from pathlib import Path

from .bundle import BundleStore
from .chat_mutations import load_metadata_json
from .factory import WorkspaceConversationStorage, existing_root
from .identity import WorkspaceScope
from .shares import ShareStorage

AUTHORITY_ENV = "SWE_CONVERSATION_STORAGE_AUTHORITY"
MAPPING_ENV = "SWE_CONVERSATION_SCOPE_MANIFEST"
CURSOR_ENV = "SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET"
_runtime = None


def resolved_scope(workspace, installed=None):
    """Use the trusted runtime identity, never filesystem directory encodings."""
    from ...config.context import canonicalize_scope_id, decode_scope_id

    explicit = getattr(workspace, "conversation_scope", None)
    if explicit is not None:
        if (
            not isinstance(explicit, WorkspaceScope)
            or explicit.agent_id != workspace.agent_id
        ):
            raise ValueError("workspace conversation scope is invalid")
        return explicit
    tenant = getattr(workspace, "tenant_id", None)
    source = getattr(workspace, "source_id", None)
    if tenant is None:
        if (
            installed is not None
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
    return WorkspaceScope(logical, decoded_source, workspace.agent_id)


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


def mysql_authority():
    authority = os.environ.get(AUTHORITY_ENV, "legacy")
    if authority not in ("legacy", "mysql"):
        raise ValueError("invalid conversation storage authority")
    return authority == "mysql"


class ConversationRuntime:
    """One database and NAS root, mapped to existing workspace identities."""

    def __init__(self, database, document, cursor_secret):
        if document.get("version") != 1 or set(document) != {
            "version",
            "nas_root",
            "workspaces",
        }:
            raise ValueError("invalid installed conversation scope manifest")
        self.database = database
        self.nas_root = existing_root(document["nas_root"])
        self.cursor_secret = cursor_secret
        self.mappings = {}
        self._workspaces = {}
        scopes = set()
        for item in document["workspaces"]:
            if set(item) != {"root", "scope"} or len(item["scope"]) != 3:
                raise ValueError("invalid installed workspace mapping")
            root = existing_root(item["root"])
            scope = WorkspaceScope(*item["scope"])
            if root in self.mappings or scope in scopes:
                raise ValueError("duplicate installed workspace identity")
            self.mappings[root] = scope
            scopes.add(scope)
        if not self.mappings:
            raise ValueError("conversation scope manifest has no workspaces")
        self.shares = ShareStorage(database, BundleStore(self.nas_root))

    def for_workspace(self, workspace):
        root = existing_root(workspace.workspace_dir)
        scope = self.mappings.get(root)
        resolved = resolved_scope(workspace, scope)
        if scope is None:
            require_empty_conversation(root)
            if resolved in self.mappings.values():
                raise ValueError(
                    "conversation scope already belongs to another workspace",
                )
            scope = resolved
            self.mappings[root] = scope
        if scope != resolved:
            raise ValueError("workspace conversation scope mismatch")
        if root not in self._workspaces:
            self._workspaces[root] = WorkspaceConversationStorage(
                self.database,
                scope,
                root,
                cursor_secret=self.cursor_secret,
                nas_root=self.nas_root,
            )
        return self._workspaces[root]

    async def verify(self):
        root, scope = next(iter(self.mappings.items()))
        await WorkspaceConversationStorage(
            self.database,
            scope,
            root,
            cursor_secret=self.cursor_secret,
            nas_root=self.nas_root,
        ).verify_schema()


async def configure_runtime(database):
    global _runtime
    _runtime = None
    if not mysql_authority():
        return
    if database is None or not database.is_connected:
        raise RuntimeError(
            "conversation storage requires a connected database",
        )
    path = os.environ.get(MAPPING_ENV)
    secret = os.environ.get(CURSOR_ENV)
    if not path or not secret:
        raise RuntimeError(
            "conversation storage requires installed scopes and cursor secret",
        )
    document = load_metadata_json(Path(path).read_bytes())
    candidate = ConversationRuntime(database, document, secret.encode())
    await candidate.verify()
    _runtime = candidate


def reset_runtime():
    """Discard process adapters when their application database shuts down."""
    global _runtime
    _runtime = None


def workspace_storage(workspace):
    if not mysql_authority():
        return None
    if _runtime is None:
        raise RuntimeError("conversation database authority is not ready")
    return _runtime.for_workspace(workspace)


def runtime_shares():
    if not mysql_authority():
        return None
    if _runtime is None:
        raise RuntimeError("conversation database authority is not ready")
    return _runtime.shares

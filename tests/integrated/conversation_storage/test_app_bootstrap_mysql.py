# -*- coding: utf-8 -*-
"""Exercise app auth, tenant bootstrap and SQL storage on an existing root."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from httpx import ASGITransport, AsyncClient
import pytest

from tests.integrated.conversation_storage.conftest import (
    SQL_ROOT,
    apply_sql_script,
)
from tests.integrated.conversation_storage.test_schema import (
    insert_chat,
    insert_session,
)

EXISTING_WORKSPACE = os.environ.get("SWE_TEST_EXISTING_WORKSPACE")
EXISTING_SECRET = os.environ.get("SWE_TEST_EXISTING_SECRET")
AUTH_SECRET = "isolated-app-bootstrap-auth-secret"


def _copy_existing_root(source: Path, destination: Path) -> Path:
    """Copy the selected existing workspace without touching the source."""
    target = destination / "default_RMASSIST"
    shutil.copytree(source, target)
    config_path = target / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["agents"]["profiles"]["default"]["workspace_dir"] = str(
        target / "workspaces" / "default",
    )
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    agent_path = target / "workspaces/default/agent.json"
    agent = json.loads(agent_path.read_text(encoding="utf-8"))
    agent["workspace_dir"] = str(target / "workspaces/default")
    agent_path.write_text(
        json.dumps(agent, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


@pytest.mark.asyncio
# pylint: disable=too-many-statements
async def test_real_app_auth_bootstrap_uses_existing_workspace_and_sql(
    conversation_test_database,
    tmp_path,
    monkeypatch,
):
    """Auth and tenant middleware route a request through real SQL storage."""
    db = conversation_test_database
    base = tmp_path / "swe"
    secret_root = tmp_path / "swe.secret"
    if not EXISTING_WORKSPACE or not EXISTING_SECRET:
        pytest.skip("existing local workspace sample is not configured")
    workspace_path = Path(EXISTING_WORKSPACE)
    secret_path = Path(EXISTING_SECRET)
    assert workspace_path.is_dir()
    assert secret_path.is_dir()
    existing = _copy_existing_root(workspace_path, base)
    existing_agent = existing / "workspaces/default/agent.json"
    assert existing_agent.is_file()
    source_secret = secret_root / "default_RMASSIST"
    shutil.copytree(secret_path, source_secret)
    (source_secret / "providers").mkdir(parents=True, exist_ok=True)
    assert db.config.database.startswith("test_conversation_")

    monkeypatch.setenv("SWE_WORKING_DIR", str(base))
    monkeypatch.setenv("SWE_SECRET_DIR", str(secret_root))
    monkeypatch.setenv("SWE_DB_HOST", db.config.host)
    monkeypatch.setenv("SWE_DB_PORT", str(db.config.port))
    monkeypatch.setenv("SWE_DB_USER", db.config.user)
    monkeypatch.setenv("SWE_DB_ACCESS", "BEE_" + db.config.password)
    monkeypatch.setenv("SWE_DB_NAME", db.config.database)
    monkeypatch.setenv("SWE_DB_MIN_CONN", "1")
    monkeypatch.setenv("SWE_DB_MAX_CONN", "2")

    from swe import constant
    from swe.app import _app
    from swe.app import auth
    from swe.app import migration
    from swe.app.chat_sharing.store import ChatShareStore
    from swe.app.conversation_storage import runtime as conversation_runtime
    from swe.app.workspace import source_template_provisioner
    from swe.config import utils as config_utils

    nas_root = tmp_path / "nas"
    nas_root.mkdir()
    manifest = tmp_path / "conversation-scopes.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 2,
                "service_unit_id": "schema-test-service",
                "nas_root": str(nas_root),
                "workspaces": [
                    {
                        "root": str(existing / "workspaces" / "default"),
                        "scope": [
                            "schema-test-service",
                            "default",
                            "RMASSIST",
                            "default",
                        ],
                    },
                ],
            },
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("CMB_CAAS_SERVICEUNITID", "schema-test-service")
    monkeypatch.setenv("SWE_AUTH_ENABLED", "true")
    monkeypatch.setenv("SWE_CONVERSATION_STORAGE_AUTHORITY", "mysql")
    monkeypatch.setenv("SWE_CONVERSATION_SCOPE_MANIFEST", str(manifest))
    monkeypatch.setenv("SWE_CONVERSATION_ARCHIVE_CURSOR_SECRET", AUTH_SECRET)
    monkeypatch.setenv("SWE_DISABLED_CHANNELS", "zhaohu")
    monkeypatch.setenv("SWE_SERVICE_HEARTBEAT_ENABLED", "false")
    monkeypatch.setattr(constant, "WORKING_DIR", base)
    monkeypatch.setattr(constant, "SECRET_DIR", secret_root)
    monkeypatch.setattr(config_utils, "WORKING_DIR", base)
    monkeypatch.setattr(config_utils, "SECRET_DIR", secret_root)
    monkeypatch.setattr(_app, "WORKING_DIR", base)
    monkeypatch.setattr(migration, "WORKING_DIR", base)
    monkeypatch.setattr(auth, "AUTH_FILE", secret_root / "auth.json")
    monkeypatch.setattr(source_template_provisioner, "SECRET_DIR", secret_root)
    readiness = source_template_provisioner.inspect_source_template_readiness(
        base,
        "RMASSIST",
    )
    assert readiness.ready, readiness

    await ChatShareStore(db).ensure_schema()
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_publication_extension.sql",
    )
    await apply_sql_script(
        db,
        SQL_ROOT / "conversation_share_service_unit_finalize.sql",
    )
    session_id = await insert_session(
        db,
        tenant_key=b"default",
        source_key=b"RMASSIST",
        agent_key=b"default",
        user_key=b"alice",
        channel_key=b"console",
        external_session_key=b"existing-session",
    )
    await insert_chat(
        db,
        session_id,
        b"existing-chat",
        tenant_key=b"default",
        source_key=b"RMASSIST",
        agent_key=b"default",
        user_key=b"alice",
        channel_key=b"console",
    )

    token = auth.register_user("bootstrap-test", "not-a-real-account")
    assert token
    initial_config = (existing / "config.json").read_bytes()
    initial_agent = (existing / "workspaces/default/agent.json").read_bytes()
    app = _app.app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(
            app=app,
            client=("198.51.100.7", 43210),
        )
        async with AsyncClient(
            transport=transport,
            base_url="http://integration.test",
        ) as client:
            path = "/api/agents/default/chats"
            headers = {
                "X-Tenant-Id": "default",
                "X-Source-Id": "RMASSIST",
                "X-User-Id": "alice",
                "X-User-Name": "Alice",
                "X-Bbk-Id": "bbk-test",
            }
            rejected = await client.get(
                path,
                headers={**headers, "Authorization": "Bearer invalid-token"},
            )
            assert rejected.status_code == 401
            accepted = await client.get(
                path,
                headers={**headers, "Authorization": f"Bearer {token}"},
            )
            assert accepted.status_code == 200, accepted.text
            payload = accepted.json()
            assert [item["id"] for item in payload] == ["existing-chat"]
            assert accepted.headers.get("x-tenant-workspace-loaded") == "true"
            assert app.state.db_connection is not None
            assert (existing / "config.json").read_bytes() == initial_config
            assert (
                existing / "workspaces/default/agent.json"
            ).read_bytes() == initial_agent
            assert (base / "default_RMASSIST").is_dir()
            assert (
                conversation_runtime.workspace_storage(
                    app.state.multi_agent_manager.agents[
                        "default_RMASSIST:default"
                    ],
                ).workspace_root
                == existing / "workspaces/default"
            )

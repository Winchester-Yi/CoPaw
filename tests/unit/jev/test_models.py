import json
from types import SimpleNamespace

import httpx
import pytest

from swe.jev.config import JevConfig
from swe.jev.models import select_subagent_model
from swe.providers.provider import ProviderInfo, ModelInfo
from swe.providers.provider_manager import ProviderManager


def provider(id, model, key="secret"):
    return ProviderInfo(
        id=id,
        name=id,
        api_key=key,
        models=[ModelInfo(id=model, name=model)],
    )


@pytest.fixture
def manager(monkeypatch):
    value = SimpleNamespace(
        builtin_providers={"small": provider("small", "cheap")},
        custom_providers={"large": provider("large", "large")},
    )
    monkeypatch.setattr(ProviderManager, "get_instance", lambda tenant: value)
    return value


def config():
    return JevConfig(
        endpoint="https://jev.test/decide",
        timeout_seconds=1,
        subagent_model_selection={"enabled": True},
    )


async def test_choice_uses_only_current_tenant_configuration(
    monkeypatch, manager
):
    original = httpx.AsyncClient

    def respond(request):
        body = json.loads(request.content)
        criteria = body["questions"]["model"]["criteria"]
        assert "summarize" in body["state"]
        assert "secret" not in request.content.decode()
        assert len(criteria) == 2
        chosen = next(key for key, name in criteria.items() if "cheap" in name)
        return httpx.Response(
            200,
            json={
                "model": "jev",
                "answers": {
                    "model": {
                        "type": "choice",
                        "choice": chosen,
                    }
                },
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    slot = await select_subagent_model(
        config(),
        "alice",
        SimpleNamespace(
            skill_owned=None,
            agent_owned=None,
        ),
        {"objective": "summarize"},
    )
    assert slot.provider_id == "small"
    assert slot.model == "cheap"


async def test_explicit_model_and_disabled_feature_skip_decision(monkeypatch):
    monkeypatch.setattr(
        ProviderManager,
        "get_instance",
        lambda tenant: pytest.fail("Unexpected provider lookup"),
    )
    explicit = SimpleNamespace(
        skill_owned=SimpleNamespace(model=object()),
        agent_owned=None,
    )
    assert await select_subagent_model(config(), "alice", explicit, {}) is None
    assert (
        await select_subagent_model(JevConfig(), "alice", explicit, {}) is None
    )


async def test_one_candidate_avoids_http(monkeypatch, manager):
    manager.custom_providers = {}
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: pytest.fail("Unexpected HTTP")
    )
    slot = await select_subagent_model(
        config(),
        "alice",
        SimpleNamespace(
            skill_owned=None,
            agent_owned=None,
        ),
        {},
    )
    assert slot.model == "cheap"


async def test_foreign_choice_preserves_original_model_path(
    monkeypatch, manager
):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "answers": {
                            "model": {
                                "type": "choice",
                                "choice": "other-tenant",
                            }
                        },
                    },
                )
            ),
            **kw,
        ),
    )
    assert (
        await select_subagent_model(
            config(),
            "alice",
            SimpleNamespace(
                skill_owned=None,
                agent_owned=None,
            ),
            {},
        )
        is None
    )


def test_selected_model_is_frozen_with_parent_fallback(monkeypatch, tmp_path):
    from swe.app.subagents.launch_snapshot import capture_model_launch_snapshot
    from swe.app.subagents.models import SubAgentDefinition
    from swe.providers.models import ModelSlotConfig

    class ConfiguredProvider(ProviderInfo):
        def has_model(self, name):
            return any(model.id == name for model in self.models)

    providers = {
        name: ConfiguredProvider(
            id=name, name=name, models=[ModelInfo(id=name, name=name)]
        )
        for name in ("cheap", "parent")
    }
    value = SimpleNamespace(
        get_active_model=lambda: ModelSlotConfig(
            provider_id="parent", model="parent"
        ),
        get_provider=providers.get,
    )
    monkeypatch.setattr(ProviderManager, "get_instance", lambda tenant: value)
    path, selected = capture_model_launch_snapshot(
        tenant_id="alice",
        run_store_dir=tmp_path,
        run_id="run-jev",
        definition=SubAgentDefinition(
            name="worker", description="Worker", instruction="Work"
        ),
        model_slot_override=ModelSlotConfig(
            provider_id="cheap", model="cheap"
        ),
    )
    from pathlib import Path

    payload = json.loads(Path(path).read_text())
    assert selected.model == "cheap"
    assert payload["selected"]["slot"]["model"] == "cheap"
    assert payload["parent"]["slot"]["model"] == "parent"


async def test_supervisor_passes_selection_to_launch_snapshot(
    monkeypatch, tmp_path, manager
):
    from swe.app.subagents import (
        AgentRegistry,
        BackgroundSubAgentScope,
        BackgroundSubAgentSupervisor,
        DelegationSpec,
        InMemoryDefinitionProvider,
    )
    from swe.app.subagents.models import SubAgentDefinition
    from swe.config.config import AgentProfileConfig
    from swe.app.subagents import supervisor as module

    manager.custom_providers = {}
    captured = []

    def snapshot(**kwargs):
        captured.append(kwargs["model_slot_override"])
        return None, kwargs["model_slot_override"]

    monkeypatch.setattr(module, "capture_model_launch_snapshot", snapshot)
    worker = SubAgentDefinition(
        name="worker", description="Worker", instruction="Work"
    )
    supervisor = BackgroundSubAgentSupervisor(
        popen_factory=lambda *args, **kwargs: SimpleNamespace(pid=1234),
        registry=AgentRegistry([InMemoryDefinitionProvider([worker])]),
    )
    scope = BackgroundSubAgentScope(
        tenant_id="alice",
        agent_id="agent",
        run_store_dir=tmp_path / "runs",
    )
    record = await supervisor.start(
        scope=scope,
        spec=DelegationSpec(
            parent_thread_id="session", name="worker", objective="Summarize"
        ),
        parent_agent_config=AgentProfileConfig(
            id="agent",
            name="Agent",
            workspace_dir=str(tmp_path),
            jev=config(),
        ),
        workspace_dir=tmp_path,
    )
    assert len(captured) == 1
    assert captured[0].model == "cheap"
    assert (scope.run_store_dir / f"{record.run_id}.launch.json").exists()

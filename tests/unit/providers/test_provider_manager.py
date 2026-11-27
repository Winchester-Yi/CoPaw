# -*- coding: utf-8 -*-
# pylint: disable=redefined-outer-name,unused-argument,protected-access,wrong-import-position,reimported
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

fcntl_stub = types.ModuleType("fcntl")
fcntl_stub.flock = lambda *args, **kwargs: None
fcntl_stub.LOCK_EX = 1
fcntl_stub.LOCK_NB = 2
fcntl_stub.LOCK_UN = 8
sys.modules.setdefault("fcntl", fcntl_stub)

import swe.providers.provider_manager as provider_manager_module
import swe.tenant_models.manager as tenant_models_manager_module
from swe.config.context import encode_scope_id, tenant_context
from swe.providers.models import ModelSlotConfig
from swe.providers.openai_provider import OpenAIProvider
from swe.providers.provider import ModelInfo
from swe.providers.provider_manager import ProviderManager
from swe.tenant_models.manager import TenantModelManager

LEGACY_PROVIDER = {
    "providers": {
        "modelscope": {
            "base_url": "https://api-inference.modelscope.cn/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "dashscope": {
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-test-legacy-secret",
            "extra_models": [{"id": "qwen-plus", "name": "Qwen Plus"}],
            "chat_model": "",
        },
        "aliyun-codingplan": {
            "base_url": "https://coding.dashscope.aliyuncs.com/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "azure-openai": {
            "base_url": "",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
        "ollama": {
            "base_url": "http://myhost:11434/v1",
            "api_key": "",
            "extra_models": [],
            "chat_model": "",
        },
    },
    "custom_providers": {
        "mydash": {
            "id": "mydash",
            "name": "MyDash",
            "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",  # noqa: E501
            "api_key_prefix": "sk-",
            "models": [{"id": "qwen3-max", "name": "qwen3-max"}],
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "sk-test-legacy-custom-secret",
            "chat_model": "OpenAIChatModel",
        },
    },
    "active_llm": {"provider_id": "dashscope", "model": "qwen3-max"},
}


@pytest.fixture
def isolated_secret_dir(monkeypatch, tmp_path):
    secret_dir = tmp_path / ".swe.secret"
    monkeypatch.setattr(provider_manager_module, "SECRET_DIR", secret_dir)
    monkeypatch.setattr(tenant_models_manager_module, "SECRET_DIR", secret_dir)
    ProviderManager._instances.clear()
    ProviderManager._instance = None
    TenantModelManager.invalidate_cache()
    yield secret_dir
    ProviderManager._instances.clear()
    ProviderManager._instance = None
    TenantModelManager.invalidate_cache()


def _openai_provider() -> OpenAIProvider:
    return OpenAIProvider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_prefix="sk-",
        models=[ModelInfo(id="gpt-5", name="GPT-5")],
        freeze_url=True,
    )


def _azure_openai_provider() -> OpenAIProvider:
    return OpenAIProvider(
        id="azure-openai",
        name="Azure OpenAI",
        models=[ModelInfo(id="gpt-5-chat", name="GPT-5 Chat")],
    )


def _dashscope_provider() -> OpenAIProvider:
    return OpenAIProvider(
        id="dashscope",
        name="DashScope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_prefix="sk",
        models=[ModelInfo(id="qwen3-max", name="Qwen3 Max")],
        freeze_url=True,
    )


def _seed_builtins(manager: ProviderManager, *providers) -> None:
    for provider in providers:
        manager.overwrite_provider_payload(provider.model_dump())


def _patch_builtins(monkeypatch, *providers) -> None:
    def _init_builtins(self):
        for provider in providers:
            self._add_builtin(provider.model_copy(deep=True))

    monkeypatch.setattr(ProviderManager, "_init_builtins", _init_builtins)


async def test_add_custom_provider_and_reload_from_storage(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider())
    custom = OpenAIProvider(
        id="custom-openai",
        name="Custom OpenAI",
        base_url="https://custom.example/v1",
        api_key="sk-custom",
        models=[ModelInfo(id="custom-model", name="Custom Model")],
    )

    created = await manager.add_custom_provider(custom)
    builtin_conflict = await manager.add_custom_provider(
        OpenAIProvider(
            id="openai",
            name="Conflict OpenAI",
        ),
    )
    duplicate = await manager.add_custom_provider(custom)

    reloaded = ProviderManager()
    loaded = reloaded.get_provider("custom-openai")
    loaded_builtin_conflict = reloaded.get_provider("openai-custom")
    loaded_duplicate = reloaded.get_provider("custom-openai-new")

    assert created.id == "custom-openai"
    assert builtin_conflict.id == "openai-custom"
    assert duplicate.id == "custom-openai-new"
    assert loaded is not None
    assert isinstance(loaded, OpenAIProvider)
    assert loaded.is_custom is True
    assert loaded.base_url == "https://custom.example/v1"
    assert loaded.api_key == "sk-custom"
    assert [m.id for m in loaded.models] == ["custom-model"]
    assert loaded_builtin_conflict is not None
    assert isinstance(loaded_builtin_conflict, OpenAIProvider)
    assert loaded_duplicate is not None
    assert isinstance(loaded_duplicate, OpenAIProvider)


async def test_add_custom_provider_uses_runtime_state_worker_for_local_save(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    worker_calls = []

    async def fake_run_runtime_state_work(func, /, *args, **kwargs):
        worker_calls.append((func.__name__, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(
        provider_manager_module,
        "run_runtime_state_work",
        fake_run_runtime_state_work,
        raising=False,
    )
    custom = OpenAIProvider(
        id="worker-provider",
        name="Worker Provider",
        base_url="https://worker.example/v1",
        api_key="sk-worker",
        models=[ModelInfo(id="worker-model", name="Worker Model")],
    )

    created = await manager.add_custom_provider(custom)

    assert created.id == "worker-provider"
    assert len(worker_calls) == 1
    func_name, args, kwargs = worker_calls[0]
    assert func_name == "_save_provider"
    assert args[0].id == "worker-provider"
    assert kwargs == {"is_builtin": False, "skip_if_exists": False}


async def test_activate_provider_persists_active_model(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider())

    class FakeCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(id="ok", request=kwargs)

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
    )

    monkeypatch.setattr(
        OpenAIProvider,
        "_client",
        lambda self, timeout=5: fake_client,
    )

    await manager.activate_model("openai", "gpt-5")

    assert manager.active_model is not None
    assert manager.active_model.provider_id == "openai"
    assert manager.active_model.model == "gpt-5"

    reloaded = ProviderManager()
    assert reloaded.active_model is not None
    assert reloaded.active_model.provider_id == "openai"
    assert reloaded.active_model.model == "gpt-5"


async def test_activate_model_uses_runtime_state_worker_for_active_model_save(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider())
    worker_calls = []

    async def fake_run_runtime_state_work(func, /, *args, **kwargs):
        worker_calls.append((func.__name__, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(
        provider_manager_module,
        "run_runtime_state_work",
        fake_run_runtime_state_work,
        raising=False,
    )
    monkeypatch.setattr(manager, "maybe_probe_multimodal", lambda *_: None)

    await manager.activate_model("openai", "gpt-5")

    assert manager.active_model == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5",
    )
    assert len(worker_calls) == 1
    func_name, args, kwargs = worker_calls[0]
    assert func_name == "save_active_model"
    assert args == (manager.active_model,)
    assert kwargs == {}


def test_provider_manager_no_longer_exposes_local_model_restore() -> None:
    manager = ProviderManager()

    assert not hasattr(manager, "start_local_model_resume")
    assert not hasattr(manager, "_resume_local_model")


def test_get_instance_isolated_by_source_for_non_default_tenant(
    isolated_secret_dir,
) -> None:
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        manager_a = ProviderManager.get_instance("tenant-a")
    with tenant_context(tenant_id="tenant-a", source_id="source-b"):
        manager_b = ProviderManager.get_instance("tenant-a")

    assert manager_a is not manager_b
    assert manager_a.tenant_id != manager_b.tenant_id


def test_get_instance_uses_requested_tenant_with_current_source(
    isolated_secret_dir,
) -> None:
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        manager = ProviderManager.get_instance("tenant-b")

    assert manager.tenant_id == encode_scope_id("tenant-b", "source-a")


def test_ensure_storage_uses_requested_tenant_with_current_source(
    isolated_secret_dir,
) -> None:
    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        ProviderManager.ensure_tenant_provider_storage("tenant-b")

    target_scope_id = encode_scope_id("tenant-b", "source-a")
    current_scope_id = encode_scope_id("tenant-a", "source-a")
    assert (isolated_secret_dir / target_scope_id / "providers").exists()
    assert not (isolated_secret_dir / current_scope_id / "providers").exists()


def test_get_instance_preserves_explicit_scoped_target_tenant(
    isolated_secret_dir,
) -> None:
    target_scope_id = encode_scope_id("tenant-b", "source-b")

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        manager = ProviderManager.get_instance(target_scope_id)

    assert manager.tenant_id == target_scope_id


def test_ensure_storage_preserves_explicit_scoped_target_tenant(
    isolated_secret_dir,
) -> None:
    target_scope_id = encode_scope_id("tenant-b", "source-b")
    current_scope_id = encode_scope_id("tenant-a", "source-a")

    with tenant_context(tenant_id="tenant-a", source_id="source-a"):
        ProviderManager.ensure_tenant_provider_storage(target_scope_id)

    assert (isolated_secret_dir / target_scope_id / "providers").exists()
    assert not (isolated_secret_dir / current_scope_id / "providers").exists()


def test_ensure_storage_uses_legacy_initializer_before_repository_prepare(
    isolated_secret_dir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[Path, str, Path]] = []

    def legacy_initialize(lock_file, tenant_id, provider_dir):  # noqa: ANN001
        observed.append((lock_file, tenant_id, provider_dir))
        provider_dir.mkdir(parents=True)

    def repository_prepare_must_not_run(self, scope):  # noqa: ANN001
        raise AssertionError(f"new repository prepare ran for {scope}")

    monkeypatch.setenv("SWE_ENABLE_LEGACY_PROVIDER_STORAGE", "1")
    monkeypatch.setattr(
        provider_manager_module.TenantProviderRepository,
        "prepare_scope",
        repository_prepare_must_not_run,
    )
    monkeypatch.setattr(
        ProviderManager,
        "_initialize_with_lock",
        staticmethod(legacy_initialize),
    )

    ProviderManager.ensure_tenant_provider_storage("tenant-a")

    assert observed == [
        (
            isolated_secret_dir / "tenant-a" / ".provider_init.lock",
            "tenant-a",
            isolated_secret_dir / "tenant-a" / "providers",
        ),
    ]


def test_provider_storage_keeps_legacy_scope_directory_untouched(
    isolated_secret_dir,
) -> None:
    canonical_scope_id = encode_scope_id("tenant-a", "source-a")
    legacy_scope_id = f"scope.v1.{canonical_scope_id}"
    legacy_provider_dir = isolated_secret_dir / legacy_scope_id / "providers"
    legacy_provider_dir.mkdir(parents=True)
    (legacy_provider_dir / "active_model.json").write_text(
        '{"provider_id": "openai", "model": "gpt-5"}',
        encoding="utf-8",
    )

    ProviderManager.ensure_tenant_provider_storage(legacy_scope_id)

    canonical_provider_dir = (
        isolated_secret_dir / canonical_scope_id / "providers"
    )
    assert canonical_provider_dir.exists()
    assert legacy_provider_dir.parent.exists()
    assert not (canonical_provider_dir / "active_model.json").exists()


async def test_remove_custom_provider_missing_file_is_safe(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    custom = OpenAIProvider(
        id="custom-to-remove",
        name="Custom To Remove",
        base_url="https://remove.example/v1",
        api_key="sk-remove",
    )
    await manager.add_custom_provider(custom)

    custom_path = manager.custom_path / "custom-to-remove.json"
    custom_path.unlink()

    manager.remove_custom_provider("custom-to-remove")

    assert manager.get_provider("custom-to-remove") is None


def test_load_provider_invalid_json_returns_none(isolated_secret_dir) -> None:
    manager = ProviderManager()
    bad_file = manager.custom_path / "bad-provider.json"
    bad_file.write_text("{invalid-json", encoding="utf-8")

    loaded = manager.load_provider("bad-provider", is_builtin=False)

    assert loaded is None


def test_get_active_model_refreshes_external_file_changes(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    active_path = manager.root_path / "active_model.json"
    active_path.write_text(
        json.dumps({"provider_id": "openai", "model": "gpt-5"}),
        encoding="utf-8",
    )
    manager._next_freshness_check_at = 0.0

    assert manager.get_active_model() == ModelSlotConfig(
        provider_id="openai",
        model="gpt-5",
    )


async def test_get_active_model_returns_none_after_external_delete(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider())
    monkeypatch.setattr(manager, "maybe_probe_multimodal", lambda *_: None)
    await manager.activate_model("openai", "gpt-5")

    (manager.root_path / "active_model.json").unlink()
    await manager.refresh_if_due()

    assert manager.get_active_model() is None


async def test_get_active_model_returns_none_for_invalid_external_file(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider())
    monkeypatch.setattr(manager, "maybe_probe_multimodal", lambda *_: None)
    await manager.activate_model("openai", "gpt-5")

    (manager.root_path / "active_model.json").write_text(
        "{invalid-json",
        encoding="utf-8",
    )
    await manager.refresh_if_due()

    assert manager.get_active_model() is None


async def test_invalid_external_custom_provider_file_removes_cached_provider(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    custom = OpenAIProvider(
        id="custom-bad",
        name="Custom Bad",
        base_url="https://custom.example/v1",
        api_key="sk-custom",
    )
    await manager.add_custom_provider(custom)

    (manager.custom_path / "custom-bad.json").write_text(
        "{invalid-json",
        encoding="utf-8",
    )

    await manager.list_provider_info()

    assert manager.get_provider("custom-bad") is None


async def test_invalid_external_builtin_provider_file_falls_back_to_default(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    _patch_builtins(monkeypatch, _openai_provider())
    manager = ProviderManager()
    assert manager.update_provider("openai", {"api_key": "sk-customized"})
    assert manager.get_provider("openai").api_key == "sk-customized"

    (manager.builtin_path / "openai.json").write_text(
        "{invalid-json",
        encoding="utf-8",
    )

    await manager.list_provider_info()

    assert manager.get_provider("openai").api_key == ""


def test_provider_manager_ignores_legacy_providers_json(
    isolated_secret_dir,
    monkeypatch,
) -> None:
    _patch_builtins(
        monkeypatch,
        _dashscope_provider(),
        _openai_provider(),
        _azure_openai_provider(),
    )
    isolated_secret_dir.mkdir(parents=True, exist_ok=True)
    legacy_file = isolated_secret_dir / "providers.json"
    legacy_file.write_text(
        json.dumps(
            LEGACY_PROVIDER,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    manager = ProviderManager()

    assert legacy_file.exists()
    assert manager.active_model is None

    dashscope_provider = manager.get_provider("dashscope")
    assert dashscope_provider is not None
    assert dashscope_provider.api_key == ""

    assert manager.get_provider("mydash") is None

    assert manager.get_provider("ollama") is None

    active_model_file = (
        isolated_secret_dir / "default" / "providers" / "active_model.json"
    )
    assert not active_model_file.exists()


async def test_add_custom_provider_conflict_resolution_loops_until_unique(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider())
    conflict = OpenAIProvider(
        id="openai",
        name="Conflict OpenAI",
    )

    first = await manager.add_custom_provider(conflict)
    second = await manager.add_custom_provider(conflict)
    third = await manager.add_custom_provider(conflict)

    assert first.id == "openai-custom"
    assert second.id == "openai-custom-new"
    assert third.id == "openai-custom-new-new"

    assert manager.get_provider("openai-custom") is not None
    assert manager.get_provider("openai-custom-new") is not None
    assert manager.get_provider("openai-custom-new-new") is not None


def test_update_provider_for_builtin_persists_to_builtin_path(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider(), _azure_openai_provider())

    ok = manager.update_provider(
        "openai",
        {
            "base_url": "https://updated.example/v1",  # not taken effect
            "api_key": "sk-updated",
        },
    )

    assert ok is True
    persisted = manager.load_provider("openai", is_builtin=True)
    assert persisted is not None
    assert isinstance(persisted, OpenAIProvider)
    assert persisted.base_url == "https://api.openai.com/v1"
    assert persisted.api_key == "sk-updated"

    ok = manager.update_provider(
        "azure-openai",
        {
            "base_url": "https://azure-updated.example/v1",
            "api_key": "sk-azure-updated",
        },
    )
    assert ok is True
    persisted_azure = manager.load_provider("azure-openai", is_builtin=True)
    assert persisted_azure is not None
    assert isinstance(persisted_azure, OpenAIProvider)
    assert persisted_azure.base_url == "https://azure-updated.example/v1"
    assert persisted_azure.api_key == "sk-azure-updated"


def test_update_provider_for_unknown_returns_false(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    ok = manager.update_provider("unknown-provider", {"api_key": "sk-x"})

    assert ok is False


async def test_activate_provider_invalid_provider_raises(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()

    with pytest.raises(ValueError, match="Provider 'missing' not found"):
        await manager.activate_model("missing", "gpt-5")


async def test_activate_provider_invalid_model_raises(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    _seed_builtins(manager, _openai_provider())

    with pytest.raises(ValueError, match="Model 'not-exists' not found"):
        await manager.activate_model("openai", "not-exists")


def test_save_provider_skip_if_exists_does_not_overwrite(
    isolated_secret_dir,
) -> None:
    manager = ProviderManager()
    provider = OpenAIProvider(
        id="custom-skip",
        name="Original",
        api_key="sk-original",
    )
    manager._save_provider(provider, is_builtin=False)

    provider.name = "Changed"
    provider.api_key = "sk-changed"
    manager._save_provider(provider, is_builtin=False, skip_if_exists=True)

    loaded = manager.load_provider("custom-skip", is_builtin=False)
    assert loaded is not None
    assert loaded.name == "Original"
    assert loaded.api_key == "sk-original"


def test_load_provider_missing_returns_none(isolated_secret_dir) -> None:
    manager = ProviderManager()

    loaded = manager.load_provider("not-found", is_builtin=False)

    assert loaded is None


def test_provider_from_data_fallback_to_openai(isolated_secret_dir) -> None:
    manager = ProviderManager()

    provider = manager._provider_from_data(
        {
            "id": "custom-openai-like",
            "name": "OpenAI Like",
            "base_url": "https://custom.example/v1",
        },
    )

    assert isinstance(provider, OpenAIProvider)


def test_openai_provider_can_resolve_kimi_chat_model_cls() -> None:
    provider = OpenAIProvider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        chat_model="KimiChatModel",
    )

    from swe.providers.kimi_chat_model import KimiChatModel

    assert provider.get_chat_model_cls() == KimiChatModel


# =============================================================================
# Tenant Isolation Tests
# =============================================================================


class TestProviderManagerTenantIsolation:
    """Tests for ProviderManager tenant isolation functionality."""

    def test_default_tenant_uses_default_path(
        self,
        isolated_secret_dir,
    ) -> None:
        """Default tenant uses path with 'default' in it."""
        manager = ProviderManager()

        assert manager.tenant_id == "default"
        assert "default" in str(manager.root_path)
        assert (
            manager.root_path == isolated_secret_dir / "default" / "providers"
        )

    def test_custom_tenant_uses_tenant_path(self, isolated_secret_dir) -> None:
        """Custom tenant uses path with tenant_id in it."""
        manager = ProviderManager(tenant_id="tenant-a")

        assert manager.tenant_id == "tenant-a"
        assert "tenant-a" in str(manager.root_path)
        assert (
            manager.root_path == isolated_secret_dir / "tenant-a" / "providers"
        )

    def test_get_instance_returns_default_without_args(
        self,
        isolated_secret_dir,
    ) -> None:
        """get_instance() without args returns default tenant manager."""
        manager = ProviderManager.get_instance()

        assert manager.tenant_id == "default"

    def test_get_instance_returns_specific_tenant(
        self,
        isolated_secret_dir,
    ) -> None:
        """get_instance(tenant_id) returns specific tenant manager."""
        manager_a = ProviderManager.get_instance("tenant-a")
        manager_b = ProviderManager.get_instance("tenant-b")

        assert manager_a.tenant_id == "tenant-a"
        assert manager_b.tenant_id == "tenant-b"
        assert manager_a is not manager_b

    def test_get_instance_caches_instances(self, isolated_secret_dir) -> None:
        """get_instance caches and returns same instance for same tenant."""
        manager_1 = ProviderManager.get_instance("cached-tenant")
        manager_2 = ProviderManager.get_instance("cached-tenant")

        assert manager_1 is manager_2

    def test_different_tenants_have_isolated_storage(
        self,
        isolated_secret_dir,
    ) -> None:
        """Different tenants have isolated storage directories."""
        manager_a = ProviderManager.get_instance("tenant-a")
        manager_b = ProviderManager.get_instance("tenant-b")
        _seed_builtins(manager_a, _openai_provider())
        _seed_builtins(manager_b, _openai_provider())

        # Update provider for tenant-a
        manager_a.update_provider("openai", {"api_key": "sk-tenant-a"})

        # Update provider for tenant-b
        manager_b.update_provider("openai", {"api_key": "sk-tenant-b"})

        # Verify isolation
        assert manager_a.get_provider("openai").api_key == "sk-tenant-a"
        assert manager_b.get_provider("openai").api_key == "sk-tenant-b"

        # Verify files are in different directories
        assert manager_a.root_path != manager_b.root_path
        assert (manager_a.root_path / "builtin" / "openai.json").exists()
        assert (manager_b.root_path / "builtin" / "openai.json").exists()

    def test_active_model_isolated_per_tenant(
        self,
        isolated_secret_dir,
    ) -> None:
        """Active model configuration is isolated per tenant."""
        manager_a = ProviderManager.get_instance("active-a")
        manager_b = ProviderManager.get_instance("active-b")

        # Set different active models
        manager_a.active_model = ModelSlotConfig(
            provider_id="openai",
            model="gpt-4",
        )
        manager_a.save_active_model(manager_a.active_model)

        manager_b.active_model = ModelSlotConfig(
            provider_id="anthropic",
            model="claude-3",
        )
        manager_b.save_active_model(manager_b.active_model)

        # Reload and verify isolation
        reloaded_a = ProviderManager.get_instance("active-a")
        reloaded_b = ProviderManager.get_instance("active-b")

        assert reloaded_a.active_model.provider_id == "openai"
        assert reloaded_a.active_model.model == "gpt-4"
        assert reloaded_b.active_model.provider_id == "anthropic"
        assert reloaded_b.active_model.model == "claude-3"

    def test_get_tenant_root_path_static_method(
        self,
        isolated_secret_dir,
    ) -> None:
        """_get_tenant_root_path returns correct path for tenant."""
        path_default = ProviderManager._get_tenant_root_path("default")
        path_tenant = ProviderManager._get_tenant_root_path("my-tenant")

        assert path_default == isolated_secret_dir / "default" / "providers"
        assert path_tenant == isolated_secret_dir / "my-tenant" / "providers"

    def test_instances_dict_is_class_attribute(self) -> None:
        """_instances is a class-level attribute shared across instances."""
        assert hasattr(ProviderManager, "_instances")
        assert isinstance(ProviderManager._instances, dict)

    def test_instances_lock_is_class_attribute(self) -> None:
        """_instances_lock is a class-level lock."""
        assert hasattr(ProviderManager, "_instances_lock")
        import threading

        assert isinstance(
            ProviderManager._instances_lock,
            type(threading.Lock()),
        )


class TestLegacyTenantModelsRecovery:
    """Tests for ignoring legacy tenant_models.json."""

    def test_ignores_legacy_tenant_models_without_active_model_file(
        self,
        isolated_secret_dir,
    ) -> None:
        """ProviderManager only reads providers/active_model.json."""
        import json
        from swe.tenant_models.manager import TenantModelManager

        # Create legacy tenant_models.json
        tenant_id = "legacy-tenant"
        tenant_dir = isolated_secret_dir / tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)

        legacy_config = {
            "version": "1.0",
            "providers": [
                {
                    "id": "test-openai",
                    "type": "openai",
                    "api_key": "test-key",
                    "models": ["gpt-4"],
                    "enabled": True,
                },
            ],
            "routing": {
                "mode": "cloud_first",
                "slots": {
                    "cloud": {"provider_id": "test-openai", "model": "gpt-4"},
                    "local": {"provider_id": "ollama", "model": "llama2"},
                },
            },
        }

        legacy_path = TenantModelManager.get_config_path(tenant_id)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(legacy_config, indent=2),
            encoding="utf-8",
        )

        # Create provider config for this tenant
        provider_dir = (
            isolated_secret_dir / tenant_id / "providers" / "builtin"
        )
        provider_dir.mkdir(parents=True, exist_ok=True)
        provider_config = {
            "id": "test-openai",
            "name": "Test OpenAI",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
            "models": [{"id": "gpt-4", "name": "GPT-4"}],
        }
        (provider_dir / "test-openai.json").write_text(
            json.dumps(provider_config, indent=2),
            encoding="utf-8",
        )

        # Initialize ProviderManager for this tenant
        manager = ProviderManager(tenant_id=tenant_id)

        assert manager.active_model is None

        active_model_file = (
            isolated_secret_dir / tenant_id / "providers" / "active_model.json"
        )
        assert not active_model_file.exists()

    def test_no_recovery_when_active_model_exists(
        self,
        isolated_secret_dir,
    ) -> None:
        """No recovery when active_model.json already exists."""
        import json
        from swe.tenant_models.manager import TenantModelManager

        tenant_id = "existing-tenant"

        # Create provider directory structure
        provider_dir = isolated_secret_dir / tenant_id / "providers"
        provider_dir.mkdir(parents=True, exist_ok=True)
        (provider_dir / "builtin").mkdir(exist_ok=True)

        # Create existing active_model.json
        active_config = {
            "provider_id": "existing-provider",
            "model": "existing-model",
        }
        (provider_dir / "active_model.json").write_text(
            json.dumps(active_config, indent=2),
            encoding="utf-8",
        )

        # Create legacy config with different values
        legacy_config = {
            "version": "1.0",
            "providers": [],
            "routing": {
                "mode": "cloud_first",
                "slots": {
                    "cloud": {
                        "provider_id": "legacy-provider",
                        "model": "legacy-model",
                    },
                    "local": {"provider_id": "ollama", "model": "llama2"},
                },
            },
        }
        legacy_path = TenantModelManager.get_config_path(tenant_id)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps(legacy_config, indent=2),
            encoding="utf-8",
        )

        # Initialize ProviderManager
        manager = ProviderManager(tenant_id=tenant_id)

        # Should use existing active_model.json, not legacy
        assert manager.active_model is not None
        assert manager.active_model.provider_id == "existing-provider"
        assert manager.active_model.model == "existing-model"

    def test_no_fallback_to_default_tenant_models(
        self,
        isolated_secret_dir,
    ) -> None:
        """ProviderManager does not inherit default tenant_models.json."""
        import json
        from swe.tenant_models.manager import TenantModelManager

        # Create default tenant legacy config
        default_tenant_id = "default"
        default_legacy_config = {
            "version": "1.0",
            "providers": [],
            "routing": {
                "mode": "local_first",
                "slots": {
                    "local": {"provider_id": "ollama", "model": "llama3"},
                    "cloud": {"provider_id": "openai", "model": "gpt-4"},
                },
            },
        }
        default_legacy_path = TenantModelManager.get_config_path(
            default_tenant_id,
        )
        default_legacy_path.parent.mkdir(parents=True, exist_ok=True)
        default_legacy_path.write_text(
            json.dumps(default_legacy_config, indent=2),
            encoding="utf-8",
        )

        # Create new tenant without its own config
        new_tenant_id = "new-inherited-tenant"
        provider_dir = isolated_secret_dir / new_tenant_id / "providers"
        provider_dir.mkdir(parents=True, exist_ok=True)
        (provider_dir / "builtin").mkdir(exist_ok=True)

        # Create ollama provider
        provider_config = {
            "id": "ollama",
            "name": "Ollama",
            "base_url": "http://localhost:11434/v1",
            "api_key": "",
            "models": [{"id": "llama3", "name": "Llama 3"}],
        }
        (provider_dir / "builtin" / "ollama.json").write_text(
            json.dumps(provider_config, indent=2),
            encoding="utf-8",
        )

        # Initialize ProviderManager for new tenant
        manager = ProviderManager(tenant_id=new_tenant_id)

        assert manager.active_model is None

        new_active_file = provider_dir / "active_model.json"
        assert not new_active_file.exists()


class TestProviderManagerEnsureStorage:
    """Tests for ensure_tenant_provider_storage."""

    def test_ensure_storage_creates_directory_structure(
        self,
        isolated_secret_dir,
    ) -> None:
        """ensure_tenant_provider_storage creates directory structure."""
        tenant_id = "ensure-test-tenant"

        ProviderManager.ensure_tenant_provider_storage(tenant_id)

        tenant_providers_dir = isolated_secret_dir / tenant_id / "providers"
        assert tenant_providers_dir.exists()
        assert (tenant_providers_dir / "builtin").exists()
        assert (tenant_providers_dir / "custom").exists()

    def test_ensure_storage_is_idempotent(
        self,
        isolated_secret_dir,
    ) -> None:
        """ensure_tenant_provider_storage is safe to call multiple times."""
        tenant_id = "idempotent-test-tenant"

        # Call multiple times
        ProviderManager.ensure_tenant_provider_storage(tenant_id)
        ProviderManager.ensure_tenant_provider_storage(tenant_id)
        ProviderManager.ensure_tenant_provider_storage(tenant_id)

        tenant_providers_dir = isolated_secret_dir / tenant_id / "providers"
        assert tenant_providers_dir.exists()

    def test_ensure_storage_with_none_uses_default(
        self,
        isolated_secret_dir,
    ) -> None:
        """ensure_tenant_provider_storage with None uses default tenant."""
        ProviderManager.ensure_tenant_provider_storage(None)

        default_providers_dir = isolated_secret_dir / "default" / "providers"
        assert default_providers_dir.exists()

    def test_ensure_storage_copies_from_default_when_exists(
        self,
        isolated_secret_dir,
    ) -> None:
        """ensure_tenant_provider_storage copies config from default tenant."""
        import json

        # Create default tenant with config
        default_dir = isolated_secret_dir / "default" / "providers"
        default_dir.mkdir(parents=True)
        (default_dir / "builtin").mkdir()
        (default_dir / "custom").mkdir()

        # Create a provider in default
        provider_config = {"id": "test-provider", "name": "Test Provider"}
        (default_dir / "builtin" / "test-provider.json").write_text(
            json.dumps(provider_config, indent=2),
            encoding="utf-8",
        )

        # Create active_model in default
        active_config = {"provider_id": "test-provider", "model": "test-model"}
        (default_dir / "active_model.json").write_text(
            json.dumps(active_config, indent=2),
            encoding="utf-8",
        )

        # Now ensure storage for new tenant
        new_tenant_id = "copy-from-default"
        ProviderManager.ensure_tenant_provider_storage(new_tenant_id)

        # Should have copied the structure
        new_dir = isolated_secret_dir / new_tenant_id / "providers"
        assert new_dir.exists()
        assert (new_dir / "builtin" / "test-provider.json").exists()
        assert (new_dir / "active_model.json").exists()

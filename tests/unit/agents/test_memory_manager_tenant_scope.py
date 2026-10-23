# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timedelta
import os

import pytest
from agentscope.message import Msg

from swe.agents.memory.base_memory_manager import BaseMemoryManager
from swe.agents.memory.reme_light_memory_manager import ReMeLightMemoryManager
from swe.app.workspace import Workspace
from swe.config.config import ToolResultCompactConfig


class _ConcreteMemoryManager(BaseMemoryManager):
    async def start(self) -> None:  # pragma: no cover - test stub
        return None

    async def close(self) -> bool:  # pragma: no cover - test stub
        return True

    async def compact_tool_result(
        self,
        **kwargs,
    ) -> None:  # pragma: no cover - test stub
        return None

    async def check_context(
        self,
        **kwargs,
    ) -> tuple:  # pragma: no cover - test stub
        return (), (), True

    async def compact_memory(
        self,
        messages,
        previous_summary="",
        **kwargs,
    ) -> str:  # pragma: no cover - test stub
        return ""

    async def summary_memory(
        self,
        messages,
        **kwargs,
    ) -> str:  # pragma: no cover - test stub
        return ""

    async def archive_checkpoint_messages(self, **kwargs):  # pragma: no cover
        return None

    async def schedule_precompaction(
        self,
        **kwargs,
    ) -> bool:  # pragma: no cover
        return False

    async def install_ready_precompaction(
        self,
        **kwargs,
    ) -> bool:  # pragma: no cover
        return False

    async def recover_evidence(self, **kwargs):  # pragma: no cover
        return []

    async def reset_context_epoch(self, **kwargs):  # pragma: no cover
        return None

    async def dream_memory(
        self,
        tenant_id: str | None = None,
        **kwargs,
    ) -> None:  # pragma: no cover - test stub
        return None

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ):  # pragma: no cover - test stub
        return None

    def get_in_memory_memory(self, **kwargs):  # pragma: no cover - test stub
        return None


def test_base_memory_manager_constructor_accepts_tenant_id() -> None:
    manager = _ConcreteMemoryManager(
        working_dir="/tmp/ws",
        agent_id="default",
        tenant_id="tenant-a",
    )

    assert manager.working_dir == "/tmp/ws"
    assert manager.agent_id == "default"
    assert manager.tenant_id == "tenant-a"


def test_memory_manager_load_agent_config_prefers_explicit_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_load_agent_config(agent_id: str, tenant_id: str | None = None):
        calls.append((agent_id, tenant_id))
        return SimpleNamespace(
            running=SimpleNamespace(
                embedding_config=SimpleNamespace(
                    backend="openai",
                    api_key="",
                    base_url="https://tenant.example",
                    model_name="embed-tenant",
                    dimensions=1024,
                    enable_cache=True,
                    use_dimensions=True,
                    max_cache_size=256,
                    max_input_length=8192,
                    max_batch_size=32,
                ),
            ),
        )

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        fake_load_agent_config,
    )

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager._warn_if_version_mismatch = lambda: None

    config = manager._load_agent_config(tenant_id="tenant-b")

    assert config.running.embedding_config.base_url == "https://tenant.example"
    assert calls == [("default", "tenant-b")]


def test_memory_manager_load_agent_config_uses_instance_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_load_agent_config(agent_id: str, tenant_id: str | None = None):
        calls.append((agent_id, tenant_id))
        return SimpleNamespace(
            running=SimpleNamespace(
                embedding_config=SimpleNamespace(
                    backend="openai",
                    api_key="",
                    base_url="https://tenant.example",
                    model_name="embed-tenant",
                    dimensions=1024,
                    enable_cache=True,
                    use_dimensions=True,
                    max_cache_size=256,
                    max_input_length=8192,
                    max_batch_size=32,
                ),
            ),
        )

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        fake_load_agent_config,
    )

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager._warn_if_version_mismatch = lambda: None

    config = manager.get_embedding_config()

    assert config["base_url"] == "https://tenant.example"
    assert calls == [("default", "tenant-a")]


@pytest.mark.asyncio
async def test_workspace_memory_manager_factory_receives_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, tuple[str, str, str | None]] = {}

    class RecordingMemoryManager(BaseMemoryManager):
        def __init__(
            self,
            working_dir: str,
            agent_id: str,
            tenant_id: str | None = None,
        ):
            super().__init__(
                working_dir=working_dir,
                agent_id=agent_id,
                tenant_id=tenant_id,
            )
            observed["constructor"] = (working_dir, agent_id, tenant_id)

        async def start(self) -> None:  # pragma: no cover - test stub
            return None

        async def close(self) -> bool:  # pragma: no cover - test stub
            return True

        async def compact_tool_result(
            self,
            **kwargs,
        ) -> None:  # pragma: no cover - test stub
            return None

        async def check_context(
            self,
            **kwargs,
        ) -> tuple:  # pragma: no cover - test stub
            return (), (), True

        async def compact_memory(
            self,
            messages,
            previous_summary="",
            **kwargs,
        ) -> str:  # pragma: no cover - test stub
            return ""

        async def summary_memory(
            self,
            messages,
            **kwargs,
        ) -> str:  # pragma: no cover - test stub
            return ""

        async def archive_checkpoint_messages(
            self,
            **kwargs,
        ):  # pragma: no cover
            return None

        async def schedule_precompaction(
            self,
            **kwargs,
        ) -> bool:  # pragma: no cover
            return False

        async def install_ready_precompaction(
            self,
            **kwargs,
        ) -> bool:  # pragma: no cover
            return False

        async def recover_evidence(self, **kwargs):  # pragma: no cover
            return []

        async def reset_context_epoch(self, **kwargs):  # pragma: no cover
            return None

        async def dream_memory(
            self,
            tenant_id: str | None = None,
            **kwargs,
        ) -> None:  # pragma: no cover - test stub
            return None

        async def memory_search(
            self,
            query: str,
            max_results: int = 5,
            min_score: float = 0.1,
        ):  # pragma: no cover - test stub
            return None

        def get_in_memory_memory(
            self,
            **kwargs,
        ):  # pragma: no cover - test stub
            return None

    monkeypatch.setattr(
        "swe.app.workspace.workspace._resolve_memory_class",
        lambda backend: RecordingMemoryManager,
    )

    workspace = Workspace(
        agent_id="default",
        workspace_dir=str(tmp_path / "ws"),
        tenant_id="tenant-a",
    )
    workspace._config = SimpleNamespace(  # pylint: disable=protected-access
        running=SimpleNamespace(memory_manager_backend="remelight"),
    )

    descriptor = workspace._service_manager.descriptors[
        "memory_manager"
    ]  # pylint: disable=protected-access
    service = await workspace._service_manager._get_or_create_service(  # pylint: disable=protected-access
        descriptor,
        is_reused=False,
    )

    assert observed["constructor"] == (
        str(workspace.workspace_dir),
        "default",
        "tenant-a",
    )
    assert service.tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_summary_memory_uses_tenant_scoped_tool_result_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, int] = {}
    import threading

    timezone_threads = []

    def read_timezone():
        timezone_threads.append(threading.get_ident())
        return SimpleNamespace(user_timezone="Asia/Shanghai")

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_config",
        read_timezone,
    )

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        lambda agent_id, tenant_id=None: SimpleNamespace(
            agent_id=agent_id,
            language="zh",
            running=SimpleNamespace(
                max_input_length=128000,
                context_compact=SimpleNamespace(
                    memory_compact_ratio=0.8,
                    compact_with_thinking_block=False,
                ),
                tool_result_compact=ToolResultCompactConfig(
                    enabled=True,
                    recent_n=10 if tenant_id == "tenant-a" else 2,
                    old_max_bytes=50000 if tenant_id == "tenant-a" else 3000,
                    recent_max_bytes=(
                        50000 if tenant_id == "tenant-a" else 3000
                    ),
                    retention_days=5,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.get_swe_token_counter",
        lambda _config: object(),
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.set_current_recent_max_bytes",
        lambda value: observed.setdefault("recent_max_bytes", value),
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.set_current_workspace_dir",
        lambda _path: None,
    )

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager.working_dir = str(Path("/tmp/ws"))
    manager._warn_if_version_mismatch = lambda: None
    manager._create_execution_model_formatter_async = AsyncMock(
        return_value=(
            object(),
            object(),
        ),
    )
    manager.summary_toolkit = object()
    manager._reme = SimpleNamespace(
        summary_memory=AsyncMock(return_value="ok"),
    )

    await manager.summary_memory(messages=[])

    assert observed["recent_max_bytes"] == 50000
    assert timezone_threads and threading.get_ident() not in timezone_threads
    assert (
        manager._reme.summary_memory.call_args.kwargs["timezone"]
        == "Asia/Shanghai"
    )


@pytest.mark.asyncio
async def test_compact_memory_uses_instance_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []
    import threading

    loop_thread = threading.get_ident()

    def fake_load_agent_config(agent_id: str, tenant_id: str | None = None):
        assert threading.get_ident() != loop_thread
        calls.append((agent_id, tenant_id))
        return SimpleNamespace(
            agent_id=agent_id,
            language="zh",
            workspace_dir="/tmp/ws",
            running=SimpleNamespace(
                max_input_length=128000,
                context_compact=SimpleNamespace(
                    memory_compact_ratio=0.8,
                    compact_with_thinking_block=False,
                ),
            ),
        )

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        fake_load_agent_config,
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.get_swe_token_counter",
        lambda _config: object(),
    )

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager._warn_if_version_mismatch = lambda: None
    manager._create_execution_model_formatter_async = AsyncMock(
        return_value=(
            object(),
            object(),
        ),
    )
    manager._reme = SimpleNamespace(
        compact_memory=AsyncMock(
            return_value={"is_valid": True, "history_compact": "ok"},
        ),
    )

    result = await manager.compact_memory(messages=[])

    assert result == "ok"
    assert calls == [("default", "tenant-a")]


def test_get_in_memory_memory_uses_instance_tenant_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_load_agent_config(agent_id: str, tenant_id: str | None = None):
        calls.append((agent_id, tenant_id))
        return SimpleNamespace()

    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.load_agent_config",
        fake_load_agent_config,
    )
    monkeypatch.setattr(
        "swe.agents.memory.reme_light_memory_manager.get_swe_token_counter",
        lambda _config: "token-counter",
    )

    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager._warn_if_version_mismatch = lambda: None
    manager._reme = SimpleNamespace(
        get_in_memory_memory=lambda **kwargs: kwargs["as_token_counter"],
    )

    token_counter = manager.get_in_memory_memory()

    assert token_counter == "token-counter"
    assert calls == [("default", "tenant-a")]


@pytest.mark.asyncio
async def test_compact_tool_result_uses_native_compaction_and_cleans_artifacts(
    tmp_path: Path,
) -> None:
    workspace_dir = tmp_path / "workspace"
    artifact_dir = workspace_dir / "tool_result"
    artifact_dir.mkdir(parents=True)
    expired_artifact = artifact_dir / "expired.txt"
    expired_artifact.write_text("expired", encoding="utf-8")
    expired_at = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(expired_artifact, (expired_at, expired_at))
    vendor_compactor = AsyncMock()
    manager = object.__new__(ReMeLightMemoryManager)
    manager.agent_id = "default"
    manager.tenant_id = "tenant-a"
    manager.working_dir = str(workspace_dir)
    manager._warn_if_version_mismatch = lambda: None
    manager._reme = SimpleNamespace(compact_tool_result=vendor_compactor)
    message = Msg(
        name="assistant",
        role="assistant",
        content=[
            {
                "type": "tool_result",
                "id": "tool-call-id",
                "output": [{"type": "text", "text": "tool output\n" * 80}],
            },
        ],
    )

    result = await manager.compact_tool_result(
        messages=[message],
        old_max_bytes=200,
        recent_max_bytes=200,
        recent_n=1,
        retention_days=1,
    )

    assert result == [message]
    vendor_compactor.assert_not_awaited()
    display = message.get_content_blocks("tool_result")[0]["output"][0]["text"]
    assert "<<<TRUNCATED>>>" in display
    assert not expired_artifact.exists()
    assert len(list(artifact_dir.glob("*.txt"))) == 1

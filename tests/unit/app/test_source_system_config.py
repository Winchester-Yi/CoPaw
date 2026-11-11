# -*- coding: utf-8 -*-
"""Source 系统配置模型、存储、运行时和 API 的单元测试。"""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from swe.config.config import QueryRetryConfig, ToolResultCompactConfig
from swe.providers.retry_chat_model import RateLimitConfig
from swe.app.middleware.tenant_identity import TenantIdentityMiddleware
from swe.app.source_system_config import router as source_config_router
from swe.app.source_system_config.middleware import (
    SourceSystemConfigMiddleware,
)
from swe.app.source_system_config.models import (
    DEFAULT_SOURCE_SYSTEM_CONFIG,
    CurrentSourceSystemConfigResponse,
    EffectiveSourceSystemConfig,
    SourceSystemConfig,
    SourceSystemConfigRecord,
    SourceSystemConfigUpsert,
)
from swe.app.source_system_config.runtime import (
    bind_source_system_config,
    get_current_source_system_config,
    get_system_prompt_injections,
    is_zhaohu_tool_guard_notification_enabled,
    resolve_archive_maintenance_config,
    resolve_cron_notification_config,
    resolve_cron_task_session_cleanup_config,
    resolve_cron_unread_auto_pause_config,
    resolve_llm_rate_limiter_config,
    resolve_query_retry_config,
    resolve_tool_result_compact_config,
)
from swe.app.source_system_config.registry import (
    CURRENT_SOURCE_SYSTEM_CONFIG_SETTINGS,
)
from swe.app.source_system_config.service import (
    SourceSystemConfigDataInvalid,
    SourceSystemConfigService,
    SourceSystemConfigUnavailable,
)
from swe.app.source_system_config.store import (
    SourceSystemConfigStore,
    SourceSystemConfigStoreUnavailable,
)

DEFAULT_EXPECTED_SOURCE_CONFIG = {
    "feature_switches": {
        "chat_task_progress_enabled": True,
        "database_access_guard_enabled": True,
        "normal_mode_plan_interaction_tools_enabled": False,
        "subagent_expert_discovery_enabled": False,
        "expert_as_main_enabled": False,
    },
    "system_prompt_injections": [],
    "tool_result_compact": {
        "enabled": True,
        "recent_n": 2,
        "old_max_bytes": 3000,
        "recent_max_bytes": 50000,
        "retention_days": 5,
    },
    "cron_unread_auto_pause": {
        "enabled": True,
        "threshold": 10,
    },
    "cron_task_session_cleanup": {
        "enabled": False,
        "retention_days": 30,
        "cron": "0 1 * * *",
    },
    "archive_maintenance": {
        "enabled": True,
        "cron": "0 3 * * *",
        "old_orphan_days": 3,
        "max_workspaces_per_run": 200,
        "max_files_per_workspace": 100,
        "max_files_per_run": 5000,
        "timeout_seconds": 900,
    },
    "cron_notifications": {
        "skip_weekend_zhaohu_enabled": False,
    },
    "approval_notifications": {
        "zhaohu_tool_guard_enabled": False,
    },
    "query_retry": {
        "enabled": False,
        "max_retries": 3,
        "backoff_base": 2.0,
        "backoff_cap": 30.0,
    },
    "llm_rate_limiter": {
        "llm_max_concurrent": 5,
        "llm_chat_max_concurrent": 2,
        "llm_cron_max_concurrent": 3,
        "llm_max_qpm": 100,
        "llm_rate_limit_pause": 5.0,
        "llm_rate_limit_jitter": 1.0,
        "llm_acquire_timeout": 300.0,
        "llm_chat_acquire_timeout": None,
        "llm_cron_acquire_timeout": None,
    },
}


class TestSourceSystemConfigModels:
    """验证 source 系统配置模型。"""

    def test_default_config_is_empty_object(self):
        """默认配置应包含代码注册的 task progress 开关默认值。"""
        assert DEFAULT_SOURCE_SYSTEM_CONFIG.as_dict() == (
            DEFAULT_EXPECTED_SOURCE_CONFIG
        )

    def test_arbitrary_object_config_is_allowed(self):
        """任意 JSON object key 都应允许由业务方自行解释。"""
        config = SourceSystemConfig.model_validate(
            {
                "provider_policy": {"default_model": "qwen-max"},
                "feature_switches": {"experimental_tooling": True},
            },
        )

        assert config.as_dict() == {
            "provider_policy": {"default_model": "qwen-max"},
            "feature_switches": {"experimental_tooling": True},
        }

    def test_deprecated_file_read_truncation_is_dropped_from_model_and_merge(
        self,
    ):
        """废弃文件读取配置不能回流，其他未注册配置仍应保留。"""
        config = SourceSystemConfig.model_validate(
            {
                "provider_policy": {"default_model": "qwen-max"},
                "file_read_truncation": {"enabled": False, "max_bytes": 1},
            },
        )

        assert config.as_dict() == {
            "provider_policy": {"default_model": "qwen-max"},
        }
        assert config.merged_with_defaults().as_dict() == {
            **DEFAULT_EXPECTED_SOURCE_CONFIG,
            "provider_policy": {"default_model": "qwen-max"},
        }

    def test_non_object_config_is_rejected(self):
        """数组或标量不能作为 source 系统配置根对象。"""
        with pytest.raises(ValueError, match="JSON object"):
            SourceSystemConfig.model_validate(["not", "object"])

    def test_nested_switch_override_keeps_default_merge_semantics(self):
        """显式关闭 task progress 时，effective 合并结果应保留其他默认结构。"""
        config = SourceSystemConfig.model_validate(
            {
                "feature_switches": {
                    "chat_task_progress_enabled": False,
                },
                "provider_policy": {"default_model": "qwen-max"},
            },
        )

        assert config.merged_with_defaults().as_dict() == {
            **DEFAULT_EXPECTED_SOURCE_CONFIG,
            "feature_switches": {
                "chat_task_progress_enabled": False,
                "database_access_guard_enabled": True,
                "normal_mode_plan_interaction_tools_enabled": False,
                "subagent_expert_discovery_enabled": False,
                "expert_as_main_enabled": False,
            },
            "provider_policy": {"default_model": "qwen-max"},
        }

    def test_tool_result_compact_config_is_accepted(self):
        """合法工具结果压缩配置应被接受并保持数值字段。"""
        config = SourceSystemConfig.model_validate(
            {
                "tool_result_compact": {
                    "enabled": False,
                    "recent_n": 4,
                    "old_max_bytes": 1200,
                    "recent_max_bytes": 8000,
                    "retention_days": 7,
                },
            },
        )

        assert config.as_dict() == {
            "tool_result_compact": {
                "enabled": False,
                "recent_n": 4,
                "old_max_bytes": 1200,
                "recent_max_bytes": 8000,
                "retention_days": 7,
            },
        }

    def test_tool_result_compact_partial_config_is_accepted(self):
        """局部 source 覆盖应允许缺失字段，运行时再继承 Agent 配置。"""
        config = SourceSystemConfig.model_validate(
            {
                "tool_result_compact": {
                    "recent_max_bytes": 8000,
                },
            },
        )

        assert config.as_dict() == {
            "tool_result_compact": {
                "recent_max_bytes": 8000,
            },
        }

    def test_file_read_truncation_is_not_a_registered_config_section(self):
        """file_read_truncation 不应再有注册的 source 配置入口。"""
        assert all(
            not setting.key.startswith("file_read_truncation.")
            for setting in CURRENT_SOURCE_SYSTEM_CONFIG_SETTINGS
        )

    def test_cron_unread_auto_pause_config_is_accepted(self):
        """定时任务未读自动暂停配置应允许按 source 覆盖。"""
        config = SourceSystemConfig.model_validate(
            {
                "cron_unread_auto_pause": {
                    "enabled": False,
                    "threshold": 12,
                },
            },
        )

        assert config.as_dict() == {
            "cron_unread_auto_pause": {
                "enabled": False,
                "threshold": 12,
            },
        }

    def test_cron_task_session_cleanup_config_is_accepted(self):
        """任务会话清理配置应允许按 source 覆盖默认保留策略。"""
        config = SourceSystemConfig.model_validate(
            {
                "cron_task_session_cleanup": {
                    "enabled": False,
                    "retention_days": 45,
                    "cron": "30 2 * * *",
                },
            },
        )

        assert config.as_dict() == {
            "cron_task_session_cleanup": {
                "enabled": False,
                "retention_days": 45,
                "cron": "30 2 * * *",
            },
        }

    def test_archive_maintenance_config_is_accepted(self):
        config = SourceSystemConfig.model_validate(
            {
                "archive_maintenance": {
                    "enabled": False,
                    "cron": "30 3 * * *",
                    "old_orphan_days": 5,
                    "max_workspaces_per_run": 50,
                    "max_files_per_workspace": 25,
                    "max_files_per_run": 1000,
                    "timeout_seconds": 120,
                },
            },
        )

        assert config.as_dict() == {
            "archive_maintenance": {
                "enabled": False,
                "cron": "30 3 * * *",
                "old_orphan_days": 5,
                "max_workspaces_per_run": 50,
                "max_files_per_workspace": 25,
                "max_files_per_run": 1000,
                "timeout_seconds": 120,
            },
        }

    def test_system_prompt_injections_are_normalized(self):
        config = SourceSystemConfig.model_validate(
            {
                "system_prompt_injections": [
                    "  keep first  ",
                    "",
                    "  ",
                    "keep first",
                    "line one\nline two",
                ],
            },
        )

        assert config.as_dict() == {
            "system_prompt_injections": [
                "keep first",
                "line one\nline two",
            ],
        }

    def test_system_prompt_injections_must_be_a_list(self):
        with pytest.raises(ValueError, match="system_prompt_injections"):
            SourceSystemConfig.model_validate(
                {"system_prompt_injections": "not-a-list"},
            )

    def test_get_system_prompt_injections_reads_effective_defaults(self):
        assert get_system_prompt_injections(None) == []
        assert get_system_prompt_injections(
            SourceSystemConfig.model_validate(
                {
                    "system_prompt_injections": [
                        "source prompt",
                        " source prompt ",
                    ],
                },
            ),
        ) == ["source prompt"]

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ({"threshold": 0}, "threshold"),
            ({"threshold": "many"}, "threshold"),
            ({"enabled": "disabled"}, "enabled"),
        ],
    )
    def test_invalid_cron_unread_auto_pause_config_is_rejected(
        self,
        payload,
        match,
    ):
        """未读暂停配置只接受可识别布尔值和大于等于 1 的整数阈值。"""
        with pytest.raises(ValueError, match=match):
            SourceSystemConfig.model_validate(
                {"cron_unread_auto_pause": payload},
            )

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ({"retention_days": 0}, "retention_days"),
            ({"retention_days": "30"}, "retention_days"),
            ({"enabled": "disabled"}, "enabled"),
            ({"cron": "*/5 * * * *"}, "cron"),
            ({"cron": "0 1 * * 1"}, "cron"),
            ({"cron": "0 24 * * *"}, "cron"),
            ({"cron": "60 1 * * *"}, "cron"),
        ],
    )
    def test_invalid_cron_task_session_cleanup_config_is_rejected(
        self,
        payload,
        match,
    ):
        """清理配置只能保存可靠的每日运行策略。"""
        with pytest.raises(ValueError, match=match):
            SourceSystemConfig.model_validate(
                {"cron_task_session_cleanup": payload},
            )

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ({"enabled": "disabled"}, "enabled"),
            ({"cron": "*/5 * * * *"}, "cron"),
            ({"cron": "0 3 * * 1"}, "cron"),
            ({"cron": "0 24 * * *"}, "cron"),
            ({"old_orphan_days": 0}, "old_orphan_days"),
            ({"max_workspaces_per_run": 0}, "max_workspaces_per_run"),
            ({"max_files_per_workspace": 0}, "max_files_per_workspace"),
            ({"max_files_per_run": 0}, "max_files_per_run"),
            ({"timeout_seconds": 0}, "timeout_seconds"),
        ],
    )
    def test_invalid_archive_maintenance_config_is_rejected(
        self,
        payload,
        match,
    ):
        with pytest.raises(ValueError, match=match):
            SourceSystemConfig.model_validate(
                {"archive_maintenance": payload},
            )

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ({"recent_n": 0}, "recent_n"),
            ({"old_max_bytes": 99}, "old_max_bytes"),
            ({"recent_max_bytes": "large"}, "recent_max_bytes"),
            ({"retention_days": 11}, "retention_days"),
            (
                {"old_max_bytes": 3000, "recent_max_bytes": 1000},
                "recent_max_bytes",
            ),
        ],
    )
    def test_invalid_tool_result_compact_config_is_rejected(
        self,
        payload,
        match,
    ):
        """非法类型、越界值和反向阈值不能被写入 source 配置。"""
        with pytest.raises(ValueError, match=match):
            SourceSystemConfig.model_validate(
                {"tool_result_compact": payload},
            )

    def test_registered_boolean_switch_normalizes_common_string_values(self):
        """历史脏值中的常见布尔字符串应被收敛为真实布尔值。"""
        config = SourceSystemConfig.model_validate(
            {
                "feature_switches": {
                    "chat_task_progress_enabled": "false",
                },
            },
        )

        assert config.as_dict() == {
            "feature_switches": {
                "chat_task_progress_enabled": False,
            },
        }

    def test_registered_boolean_switch_rejects_unknown_string_values(self):
        """无法识别的布尔脏值不能静默写入配置。"""
        with pytest.raises(ValueError, match="chat_task_progress_enabled"):
            SourceSystemConfig.model_validate(
                {
                    "feature_switches": {
                        "chat_task_progress_enabled": "disabled",
                    },
                },
            )

    def test_deprecated_external_tool_output_truncation_is_dropped(self):
        """已下线的外部工具输出截断配置在读写链路中应被清理。"""
        config = SourceSystemConfig.model_validate(
            {
                "external_tool_output_truncation": {
                    "enabled": True,
                    "max_bytes": 64000,
                },
                "provider_policy": {"default_model": "qwen-max"},
            },
        )

        assert config.as_dict() == {
            "provider_policy": {"default_model": "qwen-max"},
        }


def test_expert_main_defaults_off():
    from swe.app.source_system_config.registry import (
        build_default_source_system_config_payload,
        is_expert_as_main_enabled,
    )

    defaults = build_default_source_system_config_payload()
    assert defaults["feature_switches"]["expert_as_main_enabled"] is False
    assert is_expert_as_main_enabled(None) is False
    assert is_expert_as_main_enabled({}) is False


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [(True, True), (False, False), ("true", True), ("off", False)],
)
def test_expert_main_normalizes_boolean_values(raw_value, expected):
    from swe.app.source_system_config.registry import (
        is_expert_as_main_enabled,
        is_subagent_expert_discovery_enabled,
    )

    config = SourceSystemConfig.model_validate(
        {
            "feature_switches": {
                "expert_as_main_enabled": raw_value,
                "subagent_expert_discovery_enabled": not expected,
            },
        },
    )
    assert is_expert_as_main_enabled(config) is expected
    assert is_subagent_expert_discovery_enabled(config) is not expected
    assert (
        config.as_dict()["feature_switches"]["expert_as_main_enabled"]
        is expected
    )


def test_expert_main_rejects_invalid_boolean_values():
    with pytest.raises(ValueError, match="expert_as_main_enabled"):
        SourceSystemConfig.model_validate(
            {"feature_switches": {"expert_as_main_enabled": "disabled"}},
        )


class TestSourceSystemConfigStore:
    """验证 source 系统配置数据库存储。"""

    class _ConcurrentVersionDb:
        """通过可控并发时序验证 version 递增行为的内存 DB。"""

        def __init__(self):
            """初始化可并发读写的测试 DB。"""
            self.is_connected = True
            self.rows: dict[str, dict] = {}
            self.fetch_all = AsyncMock(return_value=[])
            self._version_probe_count = 0
            self._version_probe_ready = asyncio.Event()
            self._version_probe_snapshot: dict | None = None

        async def fetch_one(self, query, params):
            """按查询类型返回版本快照或完整行。"""
            source_id = params[0]
            if "SELECT version FROM swe_source_system_config" in query:
                self._version_probe_count += 1
                if self._version_probe_count == 1:
                    row = self.rows.get(source_id)
                    self._version_probe_snapshot = (
                        None if row is None else {"version": row["version"]}
                    )
                if self._version_probe_count < 2:
                    await self._version_probe_ready.wait()
                else:
                    self._version_probe_ready.set()
                return self._version_probe_snapshot
            row = self.rows.get(source_id)
            return None if row is None else dict(row)

        async def execute(self, query, params):
            """模拟数据库在单条 UPSERT 语句内原子更新 version。"""
            await asyncio.sleep(0)
            source_id, config_text, version, updated_by = params
            current = self.rows.get(source_id)
            if "version = version + 1" in query:
                next_version = 1 if current is None else current["version"] + 1
            else:
                next_version = version
            self.rows[source_id] = {
                "source_id": source_id,
                "config_text": config_text,
                "version": next_version,
                "updated_by": updated_by,
                "updated_at": datetime.now(),
            }
            return 1

    @pytest.fixture
    def mock_db(self):
        """创建 mock 数据库连接。"""
        db = MagicMock()
        db.is_connected = True
        db.fetch_one = AsyncMock()
        db.fetch_all = AsyncMock()
        db.execute = AsyncMock(return_value=1)
        return db

    @pytest.fixture
    def store(self, mock_db):
        """创建带 mock 数据库的存储。"""
        return SourceSystemConfigStore(db=mock_db)

    @pytest.mark.asyncio
    async def test_get_config_returns_none_when_row_missing(
        self,
        store,
        mock_db,
    ):
        """缺少记录时存储层返回 None，默认值由 service 合成。"""
        mock_db.fetch_one.return_value = None

        result = await store.get_config("portal")

        assert result is None
        mock_db.fetch_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_config_parses_valid_row(self, store, mock_db):
        """有效 JSON 行应解析为配置记录。"""
        updated_at = datetime.now()
        mock_db.fetch_one.return_value = {
            "source_id": "portal",
            "config_text": json.dumps(
                {"provider_policy": {"default_model": "qwen-max"}},
            ),
            "version": 3,
            "updated_by": "admin",
            "updated_at": updated_at,
        }

        result = await store.get_config("portal")

        assert isinstance(result, SourceSystemConfigRecord)
        assert result.source_id == "portal"
        assert result.config.as_dict() == {
            "provider_policy": {"default_model": "qwen-max"},
        }
        assert result.version == 3
        assert result.updated_by == "admin"
        assert result.updated_at == updated_at

    @pytest.mark.asyncio
    async def test_get_config_rejects_invalid_json(self, store, mock_db):
        """损坏 JSON 应作为存储数据异常抛出。"""
        mock_db.fetch_one.return_value = {
            "source_id": "portal",
            "config_text": "{bad-json",
            "version": 1,
            "updated_by": None,
            "updated_at": datetime.now(),
        }

        with pytest.raises(ValueError, match="invalid source system config"):
            await store.get_config("portal")

    @pytest.mark.asyncio
    async def test_get_config_rejects_non_object_schema(self, store, mock_db):
        """存量 JSON 如果不是 object，不能被静默接受。"""
        mock_db.fetch_one.return_value = {
            "source_id": "portal",
            "config_text": json.dumps(["not", "object"]),
            "version": 1,
            "updated_by": None,
            "updated_at": datetime.now(),
        }

        with pytest.raises(ValueError, match="JSON object"):
            await store.get_config("portal")

    @pytest.mark.asyncio
    async def test_upsert_config_increments_existing_version(
        self,
        store,
        mock_db,
    ):
        """更新已有记录时 version 必须递增并写入审计字段。"""
        updated_at = datetime.now()
        mock_db.fetch_one.return_value = {
            "source_id": "portal",
            "config_text": json.dumps({"source_name": "Portal"}),
            "version": 8,
            "updated_by": "admin",
            "updated_at": updated_at,
        }

        result = await store.upsert_config(
            "portal",
            SourceSystemConfigUpsert(
                config=SourceSystemConfig.model_validate(
                    {"source_name": "Portal"},
                ),
                updated_by="admin",
            ),
        )

        assert result.version == 8
        assert result.config.as_dict() == {"source_name": "Portal"}
        assert mock_db.execute.await_args.args[1][2] == 1
        assert mock_db.execute.await_args.args[1][3] == "admin"
        assert json.loads(mock_db.execute.await_args.args[1][1]) == {
            "source_name": "Portal",
        }
        assert "config_text" in mock_db.execute.await_args.args[0]
        assert "config_json" not in mock_db.execute.await_args.args[0]
        assert "version = version + 1" in mock_db.execute.await_args.args[0]

    @pytest.mark.asyncio
    async def test_upsert_config_keeps_version_monotonic_under_concurrency(
        self,
    ):
        """并发写入同一 source 时 version 仍应按数据库原子递增。"""
        db = self._ConcurrentVersionDb()
        store = SourceSystemConfigStore(db=db)

        await asyncio.gather(
            store.upsert_config(
                "portal",
                SourceSystemConfigUpsert(
                    config=SourceSystemConfig.model_validate(
                        {"source_name": "Portal A"},
                    ),
                    updated_by="alice",
                ),
            ),
            store.upsert_config(
                "portal",
                SourceSystemConfigUpsert(
                    config=SourceSystemConfig.model_validate(
                        {"source_name": "Portal B"},
                    ),
                    updated_by="bob",
                ),
            ),
        )

        record = await store.get_config("portal")

        assert record is not None
        assert record.version == 2
        assert db._version_probe_count == 0

    @pytest.mark.asyncio
    async def test_list_configs_parses_rows(self, store, mock_db):
        """列表查询应返回解析后的配置记录。"""
        updated_at = datetime.now()
        mock_db.fetch_all.return_value = [
            {
                "source_id": "portal",
                "config_text": json.dumps({"source_name": "Portal"}),
                "version": 2,
                "updated_by": "admin",
                "updated_at": updated_at,
            },
        ]

        result = await store.list_configs()

        assert len(result) == 1
        assert result[0].source_id == "portal"
        assert result[0].config.as_dict() == {"source_name": "Portal"}

    @pytest.mark.asyncio
    async def test_get_config_version_returns_version(self, store, mock_db):
        """版本探测查询应返回整数版本。"""
        mock_db.fetch_one.return_value = {"version": 9}

        result = await store.get_config_version("portal")

        assert result == 9

    @pytest.mark.asyncio
    async def test_delete_config_removes_row(self, store, mock_db):
        """删除配置时应按 source_id 删除数据库行。"""
        result = await store.delete_config("portal")

        assert result is True
        mock_db.execute.assert_awaited_once_with(
            "DELETE FROM swe_source_system_config WHERE source_id = %s",
            ("portal",),
        )

    @pytest.mark.asyncio
    async def test_store_operations_fail_when_db_unavailable(self):
        """DB 不可用时存储层必须显式失败，避免静默丢数据。"""
        store = SourceSystemConfigStore(db=None)
        payload = SourceSystemConfigUpsert(
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal"},
            ),
            updated_by="admin",
        )

        with pytest.raises(SourceSystemConfigStoreUnavailable):
            await store.get_config("portal")
        with pytest.raises(SourceSystemConfigStoreUnavailable):
            await store.get_config_version("portal")
        with pytest.raises(SourceSystemConfigStoreUnavailable):
            await store.list_configs()
        with pytest.raises(SourceSystemConfigStoreUnavailable):
            await store.upsert_config("portal", payload)
        with pytest.raises(SourceSystemConfigStoreUnavailable):
            await store.delete_config("portal")


class _FakeStore:
    """为 service 测试提供可控的异步 store。"""

    def __init__(self, *responses, version_responses=None):
        self.responses = list(responses)
        self.version_responses = list(version_responses or [])
        self.get_calls: list[str] = []
        self.version_calls: list[str] = []

    async def get_config(self, source_id: str):
        """按顺序返回测试指定的响应。"""
        self.get_calls.append(source_id)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def get_config_version(self, source_id: str):
        """按顺序返回版本探测结果。"""
        self.version_calls.append(source_id)
        if not self.version_responses:
            return None
        response = self.version_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeManagementStore:
    """为 API 测试提供可变的内存 store。"""

    def __init__(self):
        self.records: dict[str, SourceSystemConfigRecord] = {}
        self.upserts: list[tuple[str, SourceSystemConfigUpsert]] = []
        self.deleted: list[str] = []

    async def get_config(self, source_id: str):
        """返回指定 source 的配置记录。"""
        return self.records.get(source_id)

    async def list_configs(self):
        """返回所有配置记录。"""
        return list(self.records.values())

    async def upsert_config(self, source_id, payload):
        """写入配置并记录审计参数。"""
        self.upserts.append((source_id, payload))
        current = self.records.get(source_id)
        next_version = 1 if current is None else current.version + 1
        record = SourceSystemConfigRecord(
            source_id=source_id,
            config=payload.config,
            version=next_version,
            updated_by=payload.updated_by,
        )
        self.records[source_id] = record
        return record

    async def delete_config(self, source_id: str):
        """删除指定 source 配置。"""
        self.deleted.append(source_id)
        return self.records.pop(source_id, None) is not None


class TestSourceSystemConfigService:
    """验证 effective config 合成、缓存和失败行为。"""

    @pytest.mark.asyncio
    async def test_default_effective_config_is_empty(self):
        """缺少 source 记录时应返回内置默认配置。"""
        service = SourceSystemConfigService(
            _FakeStore(None),
            ttl_seconds=30,
            time_fn=lambda: 100,
        )

        result = await service.resolve_config("portal")

        assert result.is_default is True
        assert result.version == 0
        assert result.config.as_dict() == DEFAULT_EXPECTED_SOURCE_CONFIG

    @pytest.mark.asyncio
    async def test_default_raw_config_response_is_empty_object(self):
        """current-source 原始配置在无记录时应返回默认态空对象。"""
        service = SourceSystemConfigService(
            _FakeManagementStore(),
            ttl_seconds=30,
            time_fn=lambda: 100,
        )

        result = await service.resolve_raw_config("portal")

        assert result == CurrentSourceSystemConfigResponse(
            source_id="portal",
            config=SourceSystemConfig.model_validate({}),
            version=0,
            is_default=True,
            updated_by=None,
            updated_at=None,
        )

    @pytest.mark.asyncio
    async def test_upsert_current_source_config_prunes_default_switch(
        self,
    ):
        """保存等于默认值的已注册开关后应裁剪显式覆盖并删除空记录。"""
        store = _FakeManagementStore()
        service = SourceSystemConfigService(
            store,
            ttl_seconds=30,
            time_fn=lambda: 100,
        )

        result = await service.upsert_current_source_config(
            "portal",
            SourceSystemConfig.model_validate(
                {
                    "feature_switches": {
                        "chat_task_progress_enabled": True,
                    },
                },
            ),
            updated_by="alice",
        )

        assert result.is_default is True
        assert result.config.as_dict() == {}
        assert store.records == {}
        assert store.deleted == ["portal"]

    @pytest.mark.asyncio
    async def test_upsert_current_source_config_prunes_default_tool_result(
        self,
    ):
        """保存等于注册默认值的工具结果配置时应删除无效显式覆盖。"""
        store = _FakeManagementStore()
        service = SourceSystemConfigService(
            store,
            ttl_seconds=30,
            time_fn=lambda: 100,
        )

        result = await service.upsert_current_source_config(
            "portal",
            SourceSystemConfig.model_validate(
                {
                    "tool_result_compact": {
                        "enabled": True,
                        "recent_n": 2,
                        "old_max_bytes": 3000,
                        "recent_max_bytes": 50000,
                        "retention_days": 5,
                    },
                },
            ),
            updated_by="alice",
        )

        assert result.is_default is True
        assert result.config.as_dict() == {}
        assert store.records == {}
        assert store.deleted == ["portal"]

    @pytest.mark.asyncio
    async def test_upsert_current_source_config_keeps_model_call_policy_defaults(
        self,
    ):
        """模型调用策略默认等值配置仍代表显式 source 覆盖。"""
        store = _FakeManagementStore()
        service = SourceSystemConfigService(
            store,
            ttl_seconds=30,
            time_fn=lambda: 100,
        )

        result = await service.upsert_current_source_config(
            "portal",
            SourceSystemConfig.model_validate(
                {
                    "query_retry": {
                        "enabled": False,
                        "max_retries": 3,
                        "backoff_base": 2.0,
                        "backoff_cap": 30.0,
                    },
                    "llm_rate_limiter": {
                        "llm_max_concurrent": 5,
                        "llm_chat_max_concurrent": 2,
                        "llm_cron_max_concurrent": 3,
                        "llm_max_qpm": 100,
                        "llm_rate_limit_pause": 5.0,
                        "llm_rate_limit_jitter": 1.0,
                        "llm_acquire_timeout": 300.0,
                        "llm_chat_acquire_timeout": None,
                        "llm_cron_acquire_timeout": None,
                    },
                },
            ),
            updated_by="alice",
        )

        assert result.is_default is False
        assert result.config.as_dict() == {
            "query_retry": {
                "enabled": False,
                "max_retries": 3,
                "backoff_base": 2.0,
                "backoff_cap": 30.0,
            },
            "llm_rate_limiter": {
                "llm_max_concurrent": 5,
                "llm_chat_max_concurrent": 2,
                "llm_cron_max_concurrent": 3,
                "llm_max_qpm": 100,
                "llm_rate_limit_pause": 5.0,
                "llm_rate_limit_jitter": 1.0,
                "llm_acquire_timeout": 300.0,
                "llm_chat_acquire_timeout": None,
                "llm_cron_acquire_timeout": None,
            },
        }
        assert (
            store.records["portal"].config.as_dict() == result.config.as_dict()
        )

    @pytest.mark.asyncio
    async def test_upsert_current_source_config_preserves_unknown_keys(
        self,
    ):
        """保存 current-source 配置时应保留未知键，仅裁剪已注册默认值。"""
        store = _FakeManagementStore()
        service = SourceSystemConfigService(
            store,
            ttl_seconds=30,
            time_fn=lambda: 100,
        )

        result = await service.upsert_current_source_config(
            "portal",
            SourceSystemConfig.model_validate(
                {
                    "feature_switches": {
                        "chat_task_progress_enabled": False,
                    },
                    "tool_result_compact": {
                        "recent_max_bytes": 12000,
                    },
                    "provider_policy": {"default_model": "qwen-max"},
                    "custom_flags": {"new_homepage": True},
                },
            ),
            updated_by="alice",
        )

        assert result.is_default is False
        assert result.config.as_dict() == {
            "feature_switches": {
                "chat_task_progress_enabled": False,
            },
            "tool_result_compact": {
                "recent_max_bytes": 12000,
            },
            "provider_policy": {"default_model": "qwen-max"},
            "custom_flags": {"new_homepage": True},
        }
        assert store.records["portal"].config.as_dict() == {
            "feature_switches": {
                "chat_task_progress_enabled": False,
            },
            "tool_result_compact": {
                "recent_max_bytes": 12000,
            },
            "provider_policy": {"default_model": "qwen-max"},
            "custom_flags": {"new_homepage": True},
        }

    @pytest.mark.asyncio
    async def test_cache_refreshes_after_probe_interval(self):
        """探测窗口内复用缓存，超时后按 version 刷新。"""
        now = {"value": 100.0}
        first = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal"},
            ),
            version=1,
        )
        second = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal 2"},
            ),
            version=2,
        )
        store = _FakeStore(
            first,
            second,
            version_responses=[1, 2],
        )
        service = SourceSystemConfigService(
            store,
            ttl_seconds=30,
            probe_interval_seconds=10,
            time_fn=lambda: now["value"],
        )

        cached = await service.resolve_config("portal")
        still_cached = await service.resolve_config("portal")
        now["value"] = 111.0
        unchanged = await service.resolve_config("portal")
        now["value"] = 122.0
        refreshed = await service.resolve_config("portal")

        assert cached.version == 1
        assert still_cached.version == 1
        assert unchanged.version == 1
        assert refreshed.version == 2
        assert refreshed.config.as_dict() == {
            **DEFAULT_EXPECTED_SOURCE_CONFIG,
            "source_name": "Portal 2",
        }
        assert store.get_calls == ["portal", "portal"]
        assert store.version_calls == ["portal", "portal"]

    @pytest.mark.asyncio
    async def test_no_version_probe_within_10_seconds(self):
        """10 秒探测窗口内，重复请求不触发版本查询。"""
        now = {"value": 100.0}
        store = _FakeStore(
            SourceSystemConfigRecord(
                source_id="portal",
                config=SourceSystemConfig.model_validate(
                    {"source_name": "Portal"},
                ),
                version=2,
            ),
            version_responses=[2],
        )
        service = SourceSystemConfigService(
            store,
            probe_interval_seconds=10,
            time_fn=lambda: now["value"],
        )

        await service.resolve_config("portal")
        now["value"] = 105.0
        await service.resolve_config("portal")

        assert store.get_calls == ["portal"]
        assert store.version_calls == []

    @pytest.mark.asyncio
    async def test_ttl_expiry_forces_full_reload_before_probe_interval(self):
        """TTL 到期后应立即重载，不能被 probe 窗口静默延长。"""
        now = {"value": 100.0}
        store = _FakeStore(
            SourceSystemConfigRecord(
                source_id="portal",
                config=SourceSystemConfig.model_validate(
                    {"source_name": "Portal V1"},
                ),
                version=1,
            ),
            SourceSystemConfigRecord(
                source_id="portal",
                config=SourceSystemConfig.model_validate(
                    {"source_name": "Portal V2"},
                ),
                version=2,
            ),
        )
        service = SourceSystemConfigService(
            store,
            ttl_seconds=1,
            probe_interval_seconds=10,
            time_fn=lambda: now["value"],
        )

        first = await service.resolve_config("portal")
        now["value"] = 102.0
        refreshed = await service.resolve_config("portal")

        assert first.version == 1
        assert refreshed.version == 2
        assert refreshed.config.as_dict() == {
            **DEFAULT_EXPECTED_SOURCE_CONFIG,
            "source_name": "Portal V2",
        }
        assert store.get_calls == ["portal", "portal"]
        assert store.version_calls == []

    @pytest.mark.asyncio
    async def test_storage_failure_uses_last_known_good_cache(self):
        """缓存过期后 DB 异常时应返回 last-known-good。"""
        now = {"value": 100.0}
        record = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal"},
            ),
            version=3,
        )
        service = SourceSystemConfigService(
            _FakeStore(
                record,
                RuntimeError("db down"),
                version_responses=[4],
            ),
            ttl_seconds=30,
            probe_interval_seconds=1,
            time_fn=lambda: now["value"],
        )

        first = await service.resolve_config("portal")
        now["value"] = 102.0
        fallback = await service.resolve_config("portal")

        assert first.version == 3
        assert fallback.version == 3
        assert fallback.stale is True
        assert "db down" in (fallback.last_error or "")

    @pytest.mark.asyncio
    async def test_cross_instance_sync_by_version_probe(self):
        """实例 A 更新后，实例 B 在探测窗口后可感知新版本。"""
        now = {"value": 100.0}

        class _SharedStore:
            """两个 service 共享的可变 store。"""

            def __init__(self):
                self.record = None

            async def get_config(self, source_id: str):
                return self.record

            async def get_config_version(self, source_id: str):
                if self.record is None:
                    return None
                return self.record.version

        shared_store = _SharedStore()
        service_a = SourceSystemConfigService(
            shared_store,
            probe_interval_seconds=10,
            time_fn=lambda: now["value"],
        )
        service_b = SourceSystemConfigService(
            shared_store,
            probe_interval_seconds=10,
            time_fn=lambda: now["value"],
        )

        before_a = await service_a.resolve_config("portal")
        before_b = await service_b.resolve_config("portal")
        shared_store.record = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal V2"},
            ),
            version=1,
        )

        now["value"] = 105.0
        within_window = await service_b.resolve_config("portal")
        now["value"] = 111.0
        after_probe = await service_b.resolve_config("portal")

        assert before_a.version == 0
        assert before_b.version == 0
        assert within_window.version == 0
        assert after_probe.version == 1
        assert after_probe.config.as_dict() == {
            **DEFAULT_EXPECTED_SOURCE_CONFIG,
            "source_name": "Portal V2",
        }

    @pytest.mark.asyncio
    async def test_storage_failure_without_cache_fails(self):
        """无缓存且 DB 异常时不能静默返回默认配置。"""
        service = SourceSystemConfigService(
            _FakeStore(RuntimeError("db down")),
            ttl_seconds=1,
            time_fn=lambda: 100,
        )

        with pytest.raises(SourceSystemConfigUnavailable):
            await service.resolve_config("portal")

    @pytest.mark.asyncio
    async def test_invalid_data_without_cache_raises_data_invalid(self):
        """无缓存且读到脏数据时应显式暴露数据异常。"""
        service = SourceSystemConfigService(
            _FakeStore(ValueError("invalid source system config for portal")),
            ttl_seconds=1,
            time_fn=lambda: 100,
        )

        with pytest.raises(SourceSystemConfigDataInvalid):
            await service.resolve_config("portal")

    @pytest.mark.asyncio
    async def test_store_unavailable_returns_stale_default(self):
        """存储不可用时返回 stale 默认配置，避免误报已读取持久化结果。"""
        service = SourceSystemConfigService(
            SourceSystemConfigStore(db=None),
            ttl_seconds=30,
            time_fn=lambda: 100,
        )

        result = await service.resolve_config("portal")

        assert result.is_default is True
        assert result.stale is True
        assert "storage unavailable" in (result.last_error or "")


@pytest.mark.asyncio
async def test_expert_main_source_isolation_and_restore_default():
    from swe.app.source_system_config.registry import (
        is_expert_as_main_enabled,
        is_subagent_expert_discovery_enabled,
    )

    store = _FakeManagementStore()
    service = SourceSystemConfigService(store)
    await service.upsert_current_source_config(
        "source-a",
        SourceSystemConfig.model_validate(
            {"feature_switches": {"expert_as_main_enabled": True}},
        ),
        updated_by="alice",
    )
    await service.upsert_current_source_config(
        "source-b",
        SourceSystemConfig.model_validate(
            {"feature_switches": {"subagent_expert_discovery_enabled": True}},
        ),
        updated_by="alice",
    )

    source_a = await service.resolve_config("source-a")
    source_b = await service.resolve_config("source-b")
    assert is_expert_as_main_enabled(source_a.config) is True
    assert is_expert_as_main_enabled(source_b.config) is False
    assert is_subagent_expert_discovery_enabled(source_a.config) is False
    assert is_subagent_expert_discovery_enabled(source_b.config) is True

    restored = await service.upsert_current_source_config(
        "source-a",
        SourceSystemConfig.model_validate(
            {"feature_switches": {"expert_as_main_enabled": False}},
        ),
        updated_by="alice",
    )
    assert restored.config.as_dict() == {}
    assert "source-a" not in store.records
    assert (
        is_expert_as_main_enabled(
            (await service.resolve_config("source-a")).config,
        )
        is False
    )
    assert (
        is_subagent_expert_discovery_enabled(
            (await service.resolve_config("source-b")).config,
        )
        is True
    )


class TestSourceSystemConfigRuntime:
    """验证请求级 source 系统配置上下文 helper。"""

    def test_runtime_helpers_read_bound_config(self):
        """helper 应从 ContextVar 读取当前 source 配置。"""
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal"},
            ),
            version=4,
        )

        with bind_source_system_config(effective):
            assert get_current_source_system_config() == effective

        assert get_current_source_system_config() is None

    def test_tool_result_config_uses_agent_config_without_override(self):
        """无 source 显式覆盖时应完整继承 Agent 运行配置。"""
        base = ToolResultCompactConfig(
            enabled=True,
            recent_n=6,
            old_max_bytes=1800,
            recent_max_bytes=9000,
            retention_days=8,
        )

        result = resolve_tool_result_compact_config(base, None)

        assert result == base

    def test_tool_result_config_ignores_effective_defaults_without_raw(self):
        """effective 默认值不能被误判为 source 显式工具结果覆盖。"""
        base = ToolResultCompactConfig(
            enabled=False,
            recent_n=6,
            old_max_bytes=1800,
            recent_max_bytes=9000,
            retention_days=8,
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                DEFAULT_EXPECTED_SOURCE_CONFIG,
            ),
            raw_config=None,
            version=0,
            is_default=True,
        )

        result = resolve_tool_result_compact_config(base, effective)

        assert result == base

    def test_tool_result_config_merges_partial_source_override(self):
        """source 局部覆盖只替换显式字段，其余字段继承 Agent 配置。"""
        base = ToolResultCompactConfig(
            enabled=True,
            recent_n=6,
            old_max_bytes=1800,
            recent_max_bytes=9000,
            retention_days=8,
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                DEFAULT_EXPECTED_SOURCE_CONFIG,
            ),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "tool_result_compact": {
                        "enabled": False,
                        "recent_max_bytes": 12000,
                    },
                },
            ),
            version=3,
        )

        result = resolve_tool_result_compact_config(base, effective)

        assert result == ToolResultCompactConfig(
            enabled=False,
            recent_n=6,
            old_max_bytes=1800,
            recent_max_bytes=12000,
            retention_days=8,
        )

    def test_query_retry_ignores_effective_defaults_without_raw(self):
        """effective 默认值不能被误判为 source 显式 Query 重试覆盖。"""
        base = QueryRetryConfig(
            enabled=True,
            max_retries=5,
            backoff_base=1.5,
            backoff_cap=12.0,
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                DEFAULT_EXPECTED_SOURCE_CONFIG,
            ),
            raw_config=None,
            version=0,
            is_default=True,
        )

        assert resolve_query_retry_config(base, effective) == base

    def test_query_retry_merges_partial_source_override(self):
        """source Query 重试局部覆盖只替换显式字段。"""
        base = QueryRetryConfig(
            enabled=False,
            max_retries=5,
            backoff_base=1.5,
            backoff_cap=12.0,
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                DEFAULT_EXPECTED_SOURCE_CONFIG,
            ),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "query_retry": {
                        "enabled": True,
                        "max_retries": 2,
                    },
                },
            ),
            version=3,
        )

        assert resolve_query_retry_config(base, effective) == QueryRetryConfig(
            enabled=True,
            max_retries=2,
            backoff_base=1.5,
            backoff_cap=12.0,
        )

    def test_llm_rate_limiter_merges_partial_source_override(self):
        """source LLM 限流局部覆盖只替换显式字段。"""
        base = RateLimitConfig(
            max_concurrent=7,
            chat_max_concurrent=4,
            cron_max_concurrent=6,
            max_qpm=70,
            pause_seconds=4.0,
            jitter_range=0.5,
            acquire_timeout=30.0,
            chat_acquire_timeout=None,
            cron_acquire_timeout=45.0,
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                DEFAULT_EXPECTED_SOURCE_CONFIG,
            ),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "llm_rate_limiter": {
                        "llm_chat_max_concurrent": 1,
                        "llm_max_qpm": 12,
                    },
                },
            ),
            version=3,
        )

        result = resolve_llm_rate_limiter_config(base, effective)

        assert result.max_concurrent == 7
        assert result.max_concurrent_for("chat") == 1
        assert result.max_concurrent_for("cron") == 6
        assert result.max_qpm == 12
        assert result.pause_seconds == 4.0
        assert result.jitter_range == 0.5
        assert result.acquire_timeout_for("chat") == 30.0
        assert result.acquire_timeout_for("cron") == 45.0

    def test_llm_rate_limiter_recovers_resolved_invalid_timeouts(
        self,
        monkeypatch,
    ):
        """局部覆盖与 Agent 等待时间冲突时应修正，避免快速超时。"""
        from swe.app.source_system_config import runtime

        warning = MagicMock()
        monkeypatch.setattr(runtime.logger, "warning", warning)
        base = RateLimitConfig(
            max_concurrent=7,
            max_qpm=70,
            pause_seconds=4.0,
            jitter_range=0.5,
            acquire_timeout=30.0,
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                DEFAULT_EXPECTED_SOURCE_CONFIG,
            ),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "llm_rate_limiter": {
                        "llm_rate_limit_pause": 40,
                        "llm_rate_limit_jitter": 5,
                    },
                },
            ),
            version=3,
        )

        result = resolve_llm_rate_limiter_config(base, effective)

        assert result.pause_seconds == 40.0
        assert result.jitter_range == 5.0
        assert result.acquire_timeout == 46.0
        warning.assert_called_once()
        assert warning.call_args.args[1] == "portal"

    def test_tool_result_config_accepts_full_source_override(self):
        """source 完整覆盖应成为本请求最终工具结果压缩配置。"""
        base = ToolResultCompactConfig(
            enabled=True,
            recent_n=6,
            old_max_bytes=1800,
            recent_max_bytes=9000,
            retention_days=8,
        )

        result = resolve_tool_result_compact_config(
            base,
            SourceSystemConfig.model_validate(
                {
                    "tool_result_compact": {
                        "enabled": False,
                        "recent_n": 1,
                        "old_max_bytes": 500,
                        "recent_max_bytes": 2000,
                        "retention_days": 2,
                    },
                },
            ),
        )

        assert result == ToolResultCompactConfig(
            enabled=False,
            recent_n=1,
            old_max_bytes=500,
            recent_max_bytes=2000,
            retention_days=2,
        )

    def test_tool_result_config_recovers_resolved_invalid_thresholds(
        self,
        monkeypatch,
    ):
        """局部覆盖与 Agent 配置冲突时应修正阈值，避免请求入口失败。"""
        from swe.app.source_system_config import runtime

        warning = MagicMock()
        monkeypatch.setattr(runtime.logger, "warning", warning)
        base = ToolResultCompactConfig(
            old_max_bytes=5000,
            recent_max_bytes=10000,
        )
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                DEFAULT_EXPECTED_SOURCE_CONFIG,
            ),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "tool_result_compact": {
                        "recent_max_bytes": 3000,
                    },
                },
            ),
            version=3,
        )

        result = resolve_tool_result_compact_config(base, effective)

        assert result == ToolResultCompactConfig(
            old_max_bytes=5000,
            recent_max_bytes=5000,
        )
        warning.assert_called_once()
        assert warning.call_args.args[1] == "portal"

    def test_cron_unread_auto_pause_runtime_uses_source_config(self):
        """运行时应读取当前 source 的未读自动暂停开关和条数。"""
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "cron_unread_auto_pause": {
                        "enabled": False,
                        "threshold": 12,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "cron_unread_auto_pause": {
                        "enabled": False,
                        "threshold": 12,
                    },
                },
            ),
            version=3,
        )

        result = resolve_cron_unread_auto_pause_config(effective)

        assert result.enabled is False
        assert result.threshold == 12

    def test_cron_task_session_cleanup_runtime_uses_defaults(self):
        result = resolve_cron_task_session_cleanup_config(None)

        assert result.enabled is False
        assert result.retention_days == 30
        assert result.cron == "0 1 * * *"

    def test_cron_task_session_cleanup_runtime_uses_source_config(self):
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "cron_task_session_cleanup": {
                        "enabled": False,
                        "retention_days": 45,
                        "cron": "30 2 * * *",
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "cron_task_session_cleanup": {
                        "enabled": False,
                        "retention_days": 45,
                        "cron": "30 2 * * *",
                    },
                },
            ),
            version=3,
        )

        result = resolve_cron_task_session_cleanup_config(effective)

        assert result.enabled is False
        assert result.retention_days == 45
        assert result.cron == "30 2 * * *"

    def test_archive_maintenance_runtime_uses_defaults(self):
        result = resolve_archive_maintenance_config(None)

        assert result.enabled is True
        assert result.cron == "0 3 * * *"
        assert result.old_orphan_days == 3
        assert result.max_workspaces_per_run == 200
        assert result.max_files_per_workspace == 100
        assert result.max_files_per_run == 5000
        assert result.timeout_seconds == 900

    def test_archive_maintenance_runtime_uses_source_config(self):
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "archive_maintenance": {
                        "enabled": False,
                        "cron": "30 3 * * *",
                        "old_orphan_days": 5,
                        "max_workspaces_per_run": 50,
                        "max_files_per_workspace": 25,
                        "max_files_per_run": 1000,
                        "timeout_seconds": 120,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "archive_maintenance": {
                        "enabled": False,
                        "cron": "30 3 * * *",
                        "old_orphan_days": 5,
                        "max_workspaces_per_run": 50,
                        "max_files_per_workspace": 25,
                        "max_files_per_run": 1000,
                        "timeout_seconds": 120,
                    },
                },
            ),
            version=3,
        )

        result = resolve_archive_maintenance_config(effective)

        assert result.enabled is False
        assert result.cron == "30 3 * * *"
        assert result.old_orphan_days == 5
        assert result.max_workspaces_per_run == 50
        assert result.max_files_per_workspace == 25
        assert result.max_files_per_run == 1000
        assert result.timeout_seconds == 120

    def test_cron_notification_runtime_uses_defaults(self):
        result = resolve_cron_notification_config(None)

        assert result.skip_weekend_zhaohu_enabled is False

    def test_cron_notification_runtime_uses_bound_weekend_switch(self):
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "cron_notifications": {
                        "skip_weekend_zhaohu_enabled": True,
                    },
                },
            ),
            version=3,
        )

        with bind_source_system_config(effective):
            result = resolve_cron_notification_config()

        assert result.skip_weekend_zhaohu_enabled is True

    def test_zhaohu_tool_guard_notification_runtime_uses_defaults(self):
        assert is_zhaohu_tool_guard_notification_enabled(None) is False

    def test_zhaohu_tool_guard_notification_runtime_uses_enabled_source_config(
        self,
    ):
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "approval_notifications": {
                        "zhaohu_tool_guard_enabled": True,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "approval_notifications": {
                        "zhaohu_tool_guard_enabled": True,
                    },
                },
            ),
            version=3,
        )

        result = is_zhaohu_tool_guard_notification_enabled(effective)

        assert result is True

    def test_zhaohu_tool_guard_notification_runtime_uses_disabled_source_config(
        self,
    ):
        effective = EffectiveSourceSystemConfig(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "approval_notifications": {
                        "zhaohu_tool_guard_enabled": False,
                    },
                },
            ).merged_with_defaults(),
            raw_config=SourceSystemConfig.model_validate(
                {
                    "approval_notifications": {
                        "zhaohu_tool_guard_enabled": False,
                    },
                },
            ),
            version=3,
        )

        result = is_zhaohu_tool_guard_notification_enabled(effective)

        assert result is False


class TestSourceSystemConfigMiddleware:
    """验证 HTTP 请求级配置绑定。"""

    def test_middleware_binds_request_state_and_context(self):
        """中间件应在 source 身份解析后绑定 effective config。"""
        record = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal"},
            ),
            version=5,
        )
        service = SourceSystemConfigService(
            _FakeStore(record),
            ttl_seconds=30,
            time_fn=lambda: 100,
        )
        app = FastAPI()

        @app.get("/api/config-check")
        async def config_check(request: Request):
            state_config = request.state.source_system_config
            context_config = get_current_source_system_config()
            return {
                "state_version": state_config.version,
                "context_version": context_config.version,
                "config": context_config.config.as_dict(),
            }

        app.add_middleware(SourceSystemConfigMiddleware, service=service)
        app.add_middleware(TenantIdentityMiddleware, default_tenant_id=None)

        response = TestClient(app).get(
            "/api/config-check",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "state_version": 5,
            "context_version": 5,
            "config": {
                **DEFAULT_EXPECTED_SOURCE_CONFIG,
                "source_name": "Portal",
            },
        }

    def test_middleware_returns_500_when_config_data_is_invalid(self):
        """中间件遇到脏配置时应返回 500，避免误报为存储不可用。"""
        service = SourceSystemConfigService(
            _FakeStore(ValueError("invalid source system config for portal")),
            ttl_seconds=30,
            time_fn=lambda: 100,
        )
        app = FastAPI()

        @app.get("/api/config-check")
        async def config_check():
            return {"ok": True}

        app.add_middleware(SourceSystemConfigMiddleware, service=service)
        app.add_middleware(TenantIdentityMiddleware, default_tenant_id=None)

        response = TestClient(app).get(
            "/api/config-check",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
            },
        )

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Source system config data is invalid",
        }

    def test_middleware_skips_public_static_routes_with_source_header(self):
        """静态文件请求不应因 X-Source-Id 触发 source 配置解析。"""
        service = SimpleNamespace(
            resolve_config=AsyncMock(
                side_effect=AssertionError("source config resolved"),
            ),
        )
        app = FastAPI()

        @app.get("/static/{scope_id}/{agent_id}/{file_name:path}")
        async def static_file():
            assert get_current_source_system_config() is None
            return {"ok": True}

        app.add_middleware(SourceSystemConfigMiddleware, service=service)
        app.add_middleware(TenantIdentityMiddleware, default_tenant_id=None)

        response = TestClient(app, raise_server_exceptions=False).get(
            "/static/default/default/report.html",
            headers={
                "X-Tenant-Id": "default",
                "X-Source-Id": "RMASSIST",
            },
        )

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        service.resolve_config.assert_not_awaited()


class TestSourceSystemConfigApi:
    """验证 source 系统配置 API。"""

    def _build_client(
        self,
        store,
        workspace=None,
        source_scheduler=None,
    ) -> TestClient:
        """创建带真实路由和中间件的测试客户端。"""
        return self._build_client_with_server_exception_mode(
            store,
            True,
            workspace=workspace,
            source_scheduler=source_scheduler,
        )

    def _build_client_with_server_exception_mode(
        self,
        store,
        raise_server_exceptions: bool,
        workspace=None,
        source_scheduler=None,
    ) -> TestClient:
        """创建可选择是否透传服务端异常的测试客户端。"""
        app = FastAPI()
        service = SourceSystemConfigService(
            store,
            ttl_seconds=0,
            time_fn=lambda: 100,
        )
        app.state.source_system_config_service = service
        app.state.source_system_task_scheduler = source_scheduler
        app.include_router(source_config_router, prefix="/api")
        if workspace is not None:

            @app.middleware("http")
            async def attach_workspace(request, call_next):
                request.state.workspace = workspace
                return await call_next(request)

        app.add_middleware(SourceSystemConfigMiddleware)
        app.add_middleware(TenantIdentityMiddleware, default_tenant_id=None)
        return TestClient(
            app,
            raise_server_exceptions=raise_server_exceptions,
        )

    def _build_management_client(
        self,
        store,
        source_scheduler=None,
    ) -> TestClient:
        """创建仅覆盖管理路由的测试客户端。"""
        app = FastAPI()
        app.state.source_system_config_service = SourceSystemConfigService(
            store,
            ttl_seconds=0,
            time_fn=lambda: 100,
        )
        app.state.source_system_task_scheduler = source_scheduler
        app.include_router(source_config_router, prefix="/api")
        return TestClient(app, raise_server_exceptions=False)

    def test_effective_config_returns_defaults_for_missing_source(self):
        """未配置 source 时 effective API 返回默认空配置。"""
        client = self._build_client(_FakeManagementStore())

        response = client.get(
            "/api/source-system-config/effective",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["source_id"] == "portal"
        assert body["is_default"] is True
        assert body["version"] == 0
        assert body["config"] == DEFAULT_EXPECTED_SOURCE_CONFIG

    def test_manager_reads_current_source_default_state(self):
        """manager 读取 current-source 原始配置时，无记录应返回默认态对象。"""
        client = self._build_client(_FakeManagementStore())

        response = client.get(
            "/api/source-system-config/current",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Id": "alice",
                "X-User-Role": "manager",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "source_id": "portal",
            "config": {},
            "version": 0,
            "is_default": True,
            "updated_by": None,
            "updated_at": None,
        }

    def test_manager_updates_current_source_config(self):
        """current-source 写入必须始终落到请求上下文 source 上。"""
        store = _FakeManagementStore()
        client = self._build_client(store)

        response = client.put(
            "/api/source-system-config/current",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Id": "alice",
                "X-User-Role": "manager",
            },
            json={
                "config": {
                    "feature_switches": {
                        "chat_task_progress_enabled": False,
                    },
                    "file_read_truncation": {
                        "enabled": False,
                        "max_bytes": 1,
                    },
                },
            },
        )

        assert response.status_code == 200
        assert response.json()["source_id"] == "portal"
        assert response.json()["config"] == {
            "feature_switches": {
                "chat_task_progress_enabled": False,
            },
        }
        assert list(store.records) == ["portal"]

    def test_manager_update_refreshes_cleanup_source_task(self):
        """保存清理配置后，应按最后修改者身份刷新 source 级系统任务。"""
        store = _FakeManagementStore()
        source_scheduler = SimpleNamespace(
            refresh_task_session_cleanup=AsyncMock(),
            refresh_archive_maintenance=AsyncMock(),
        )
        client = self._build_client(
            store,
            source_scheduler=source_scheduler,
        )

        response = client.put(
            "/api/source-system-config/current",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Id": "alice",
                "X-User-Role": "manager",
            },
            json={
                "config": {
                    "cron_task_session_cleanup": {
                        "enabled": True,
                        "retention_days": 30,
                        "cron": "0 1 * * *",
                    },
                },
            },
        )

        assert response.status_code == 200
        refresh = source_scheduler.refresh_task_session_cleanup
        refresh.assert_awaited_once()
        kwargs = refresh.await_args.kwargs
        identity = kwargs["identity"]
        assert kwargs["source_id"] == "portal"
        assert kwargs["config"] is None
        assert identity.tenant_id == "tenant-a"
        assert identity.from_id == "tenant-a"
        assert identity.updated_by == "alice"
        archive_refresh = source_scheduler.refresh_archive_maintenance
        archive_refresh.assert_awaited_once()
        archive_kwargs = archive_refresh.await_args.kwargs
        archive_identity = archive_kwargs["identity"]
        assert archive_kwargs["source_id"] == "portal"
        assert archive_kwargs["config"] is None
        assert archive_identity.tenant_id == "tenant-a"
        assert archive_identity.from_id == "tenant-a"
        assert archive_identity.updated_by == "alice"

    def test_named_source_update_refreshes_cleanup_source_task(self):
        """管理指定 source 时，刷新目标应来自路径参数而不是请求上下文。"""
        store = _FakeManagementStore()
        source_scheduler = SimpleNamespace(
            refresh_task_session_cleanup=AsyncMock(),
            refresh_archive_maintenance=AsyncMock(),
        )
        client = self._build_management_client(
            store,
            source_scheduler=source_scheduler,
        )

        response = client.put(
            "/api/source-system-config/sources/target-source",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-User-Id": "alice",
                "X-User-Role": "manager",
            },
            json={
                "config": {
                    "cron_task_session_cleanup": {
                        "enabled": True,
                        "retention_days": 30,
                        "cron": "0 1 * * *",
                    },
                },
            },
        )

        assert response.status_code == 200
        refresh = source_scheduler.refresh_task_session_cleanup
        refresh.assert_awaited_once()
        kwargs = refresh.await_args.kwargs
        identity = kwargs["identity"]
        assert kwargs["source_id"] == "target-source"
        assert kwargs["config"] is None
        assert identity.tenant_id == "tenant-a"
        assert identity.from_id == "tenant-a"
        assert identity.updated_by == "alice"
        archive_refresh = source_scheduler.refresh_archive_maintenance
        archive_refresh.assert_awaited_once()
        archive_kwargs = archive_refresh.await_args.kwargs
        archive_identity = archive_kwargs["identity"]
        assert archive_kwargs["source_id"] == "target-source"
        assert archive_kwargs["config"] is None
        assert archive_identity.tenant_id == "tenant-a"
        assert archive_identity.from_id == "tenant-a"
        assert archive_identity.updated_by == "alice"

    def test_source_task_refreshes_are_failure_isolated(self):
        store = _FakeManagementStore()
        source_scheduler = SimpleNamespace(
            refresh_task_session_cleanup=AsyncMock(
                side_effect=RuntimeError("cleanup scheduler down"),
            ),
            refresh_archive_maintenance=AsyncMock(),
        )
        client = self._build_client(
            store,
            source_scheduler=source_scheduler,
        )

        response = client.put(
            "/api/source-system-config/current",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Id": "alice",
                "X-User-Role": "manager",
            },
            json={
                "config": {
                    "archive_maintenance": {
                        "enabled": True,
                        "cron": "0 3 * * *",
                    },
                },
            },
        )

        assert response.status_code == 200
        source_scheduler.refresh_task_session_cleanup.assert_awaited_once()
        source_scheduler.refresh_archive_maintenance.assert_awaited_once()

    def test_current_source_update_rejects_body_source_override(self):
        """current-source 接口不允许请求体携带 source_id 覆盖目标 source。"""
        client = self._build_client(_FakeManagementStore())

        response = client.put(
            "/api/source-system-config/current",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Role": "manager",
            },
            json={
                "source_id": "other-source",
                "config": {
                    "feature_switches": {
                        "chat_task_progress_enabled": False,
                    },
                },
            },
        )

        assert response.status_code == 422

    def test_manager_deletes_current_source_config(self):
        """删除 current-source 配置后，再次读取应回到默认态。"""
        store = _FakeManagementStore()
        store.records["portal"] = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {
                    "feature_switches": {
                        "chat_task_progress_enabled": False,
                    },
                },
            ),
            version=2,
            updated_by="alice",
        )
        client = self._build_client(store)
        headers = {
            "X-Tenant-Id": "tenant-a",
            "X-Source-Id": "portal",
            "X-User-Role": "manager",
        }

        delete_response = client.delete(
            "/api/source-system-config/current",
            headers=headers,
        )
        read_response = client.get(
            "/api/source-system-config/current",
            headers=headers,
        )

        assert delete_response.status_code == 200
        assert delete_response.json()["deleted"] is True
        assert (
            delete_response.json()["scheduler_sync"]["submission_status"]
            == "failed"
        )
        assert read_response.status_code == 200
        assert read_response.json()["config"] == {}
        assert read_response.json()["is_default"] is True

    def test_named_source_delete_refreshes_cleanup_source_task(self):
        """删除指定 source 配置后应按默认配置刷新清理任务。"""
        store = _FakeManagementStore()
        store.records["target-source"] = SourceSystemConfigRecord(
            source_id="target-source",
            config=SourceSystemConfig.model_validate(
                {
                    "cron_task_session_cleanup": {
                        "enabled": True,
                        "retention_days": 30,
                        "cron": "0 1 * * *",
                    },
                },
            ),
            version=2,
            updated_by="alice",
        )
        source_scheduler = SimpleNamespace(
            refresh_task_session_cleanup=AsyncMock(),
            refresh_archive_maintenance=AsyncMock(),
        )
        client = self._build_management_client(
            store,
            source_scheduler=source_scheduler,
        )

        response = client.delete(
            "/api/source-system-config/sources/target-source",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-User-Id": "bob",
                "X-User-Role": "manager",
            },
        )

        assert response.status_code == 200
        refresh = source_scheduler.refresh_task_session_cleanup
        refresh.assert_awaited_once()
        kwargs = refresh.await_args.kwargs
        identity = kwargs["identity"]
        assert kwargs["source_id"] == "target-source"
        assert kwargs["config"] is None
        assert identity.tenant_id == "tenant-a"
        assert identity.from_id == "tenant-a"
        assert identity.updated_by == "bob"
        archive_refresh = source_scheduler.refresh_archive_maintenance
        archive_refresh.assert_awaited_once()
        archive_kwargs = archive_refresh.await_args.kwargs
        archive_identity = archive_kwargs["identity"]
        assert archive_kwargs["source_id"] == "target-source"
        assert archive_kwargs["config"] is None
        assert archive_identity.tenant_id == "tenant-a"
        assert archive_identity.from_id == "tenant-a"
        assert archive_identity.updated_by == "bob"

    def test_effective_config_returns_500_when_persisted_data_is_invalid(
        self,
    ):
        """/effective 读取脏数据时应返回 500，而不是 503。"""
        client = self._build_client_with_server_exception_mode(
            _FakeStore(
                ValueError("invalid source system config for portal"),
            ),
            False,
        )

        response = client.get(
            "/api/source-system-config/effective",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
            },
        )

        assert response.status_code == 500
        assert response.json() == {
            "detail": "Source system config data is invalid",
        }

    def test_non_manager_cannot_update_source_config(self):
        """非 manager 角色不能写入 source 系统配置。"""
        client = self._build_client(_FakeManagementStore())

        response = client.put(
            "/api/source-system-config/sources/portal",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Id": "alice",
            },
            json={"config": {"source_name": "Portal"}},
        )

        assert response.status_code == 403

    def test_non_manager_cannot_access_current_source_config(self):
        """非 manager 角色不能读取或写入 current-source 配置。"""
        client = self._build_client(_FakeManagementStore())
        headers = {
            "X-Tenant-Id": "tenant-a",
            "X-Source-Id": "portal",
            "X-User-Id": "alice",
        }

        read_response = client.get(
            "/api/source-system-config/current",
            headers=headers,
        )
        write_response = client.put(
            "/api/source-system-config/current",
            headers=headers,
            json={"config": {}},
        )
        delete_response = client.delete(
            "/api/source-system-config/current",
            headers=headers,
        )

        assert read_response.status_code == 403
        assert write_response.status_code == 403
        assert delete_response.status_code == 403

    def test_manager_updates_source_config_with_audit_metadata(self):
        """manager 更新配置时应校验、持久化并写入审计人。"""
        store = _FakeManagementStore()
        client = self._build_client(store)

        response = client.put(
            "/api/source-system-config/sources/portal",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Id": "alice",
                "X-User-Role": "manager",
            },
            json={"config": {"source_name": "Portal"}},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["source_id"] == "portal"
        assert body["updated_by"] == "alice"
        assert body["config"] == {"source_name": "Portal"}
        assert store.upserts[0][1].updated_by == "alice"

    def test_invalid_config_is_rejected(self):
        """非 object 配置请求必须被拒绝。"""
        client = self._build_client(_FakeManagementStore())

        response = client.put(
            "/api/source-system-config/sources/portal",
            headers={
                "X-Tenant-Id": "tenant-a",
                "X-Source-Id": "portal",
                "X-User-Role": "manager",
            },
            json={"config": ["not", "object"]},
        )

        assert response.status_code == 422

    def test_manager_lists_and_reads_source_configs(self):
        """manager 可以列表和读取 source 配置记录。"""
        store = _FakeManagementStore()
        store.records["portal"] = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal"},
            ),
            version=2,
            updated_by="alice",
        )
        client = self._build_client(store)
        headers = {
            "X-Tenant-Id": "tenant-a",
            "X-Source-Id": "portal",
            "X-User-Role": "admin",
        }

        list_response = client.get(
            "/api/source-system-config/sources",
            headers=headers,
        )
        read_response = client.get(
            "/api/source-system-config/sources/portal",
            headers=headers,
        )

        assert list_response.status_code == 200
        assert list_response.json()["total"] == 1
        assert read_response.status_code == 200
        assert read_response.json()["version"] == 2
        assert read_response.json()["config"] == {"source_name": "Portal"}

    def test_manager_deletes_source_config(self):
        """manager 可以删除 source 配置，后续 effective 读取回到默认配置。"""
        store = _FakeManagementStore()
        store.records["portal"] = SourceSystemConfigRecord(
            source_id="portal",
            config=SourceSystemConfig.model_validate(
                {"source_name": "Portal"},
            ),
            version=2,
            updated_by="alice",
        )
        client = self._build_client(store)
        headers = {
            "X-Tenant-Id": "tenant-a",
            "X-Source-Id": "portal",
            "X-User-Role": "admin",
        }

        delete_response = client.delete(
            "/api/source-system-config/sources/portal",
            headers=headers,
        )
        effective_response = client.get(
            "/api/source-system-config/effective",
            headers=headers,
        )

        assert delete_response.status_code == 200
        assert delete_response.json()["deleted"] is True
        assert (
            delete_response.json()["scheduler_sync"]["submission_status"]
            == "failed"
        )
        assert store.deleted == ["portal"]
        assert effective_response.status_code == 200
        assert effective_response.json()["is_default"] is True
        assert effective_response.json()["config"] == (
            DEFAULT_EXPECTED_SOURCE_CONFIG
        )

    def test_management_crud_returns_503_when_storage_unavailable(self):
        """DB 不可用时管理端 CRUD 应返回 503，不能返回伪成功。"""
        client = self._build_client(SourceSystemConfigStore(db=None))
        headers = {
            "X-Tenant-Id": "tenant-a",
            "X-Source-Id": "portal",
            "X-User-Role": "manager",
            "X-User-Id": "alice",
        }

        list_response = client.get(
            "/api/source-system-config/sources",
            headers=headers,
        )
        get_response = client.get(
            "/api/source-system-config/sources/portal",
            headers=headers,
        )
        upsert_response = client.put(
            "/api/source-system-config/sources/portal",
            headers=headers,
            json={"config": {"source_name": "Portal"}},
        )
        delete_response = client.delete(
            "/api/source-system-config/sources/portal",
            headers=headers,
        )

        assert list_response.status_code == 503
        assert get_response.status_code == 503
        assert upsert_response.status_code == 503
        assert delete_response.status_code == 503

    def test_management_crud_returns_503_when_sql_execution_fails(self):
        """DB 已连接但 SQL 调用失败时，管理端也应统一返回 503。"""
        headers = {
            "X-User-Role": "manager",
            "X-User-Id": "alice",
        }

        list_db = MagicMock()
        list_db.is_connected = True
        list_db.fetch_all = AsyncMock(side_effect=RuntimeError("db down"))
        list_db.fetch_one = AsyncMock()
        list_db.execute = AsyncMock()
        list_response = self._build_management_client(
            SourceSystemConfigStore(db=list_db),
        ).get("/api/source-system-config/sources", headers=headers)

        get_db = MagicMock()
        get_db.is_connected = True
        get_db.fetch_one = AsyncMock(side_effect=RuntimeError("db down"))
        get_db.fetch_all = AsyncMock()
        get_db.execute = AsyncMock()
        get_response = self._build_management_client(
            SourceSystemConfigStore(db=get_db),
        ).get(
            "/api/source-system-config/sources/portal",
            headers=headers,
        )

        upsert_db = MagicMock()
        upsert_db.is_connected = True
        upsert_db.fetch_one = AsyncMock(side_effect=RuntimeError("db down"))
        upsert_db.fetch_all = AsyncMock()
        upsert_db.execute = AsyncMock()
        upsert_response = self._build_management_client(
            SourceSystemConfigStore(db=upsert_db),
        ).put(
            "/api/source-system-config/sources/portal",
            headers=headers,
            json={"config": {"source_name": "Portal"}},
        )

        delete_db = MagicMock()
        delete_db.is_connected = True
        delete_db.fetch_one = AsyncMock()
        delete_db.fetch_all = AsyncMock()
        delete_db.execute = AsyncMock(side_effect=RuntimeError("db down"))
        delete_response = self._build_management_client(
            SourceSystemConfigStore(db=delete_db),
        ).delete(
            "/api/source-system-config/sources/portal",
            headers=headers,
        )

        assert list_response.status_code == 503
        assert get_response.status_code == 503
        assert upsert_response.status_code == 503
        assert delete_response.status_code == 503

    def test_management_read_returns_500_when_persisted_config_is_invalid(
        self,
    ):
        """脏数据应暴露为服务端数据异常，不能伪装成存储不可用。"""
        db = MagicMock()
        db.is_connected = True
        db.fetch_all = AsyncMock(
            return_value=[
                {
                    "source_id": "portal",
                    "config_text": "{bad-json",
                    "version": 1,
                    "updated_by": "alice",
                    "updated_at": datetime.now(),
                },
            ],
        )
        db.fetch_one = AsyncMock(
            return_value={
                "source_id": "portal",
                "config_text": "{bad-json",
                "version": 1,
                "updated_by": "alice",
                "updated_at": datetime.now(),
            },
        )
        db.execute = AsyncMock()
        client = self._build_management_client(
            SourceSystemConfigStore(db=db),
        )
        headers = {
            "X-User-Role": "manager",
        }

        list_response = client.get(
            "/api/source-system-config/sources",
            headers=headers,
        )
        get_response = client.get(
            "/api/source-system-config/sources/portal",
            headers=headers,
        )

        assert list_response.status_code == 500
        assert list_response.json() == {
            "detail": "Source system config data is invalid",
        }
        assert get_response.status_code == 500
        assert get_response.json() == {
            "detail": "Source system config data is invalid",
        }

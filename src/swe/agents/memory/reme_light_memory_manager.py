# -*- coding: utf-8 -*-
# pylint: disable=too-many-branches
# mypy: ignore-errors
"""ReMeLight-backed memory manager for SWE agents."""

import importlib
import importlib.metadata
import copy
import json
import logging
import os
import platform
import shutil
import sys
import types
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentscope.agent import ReActAgent
from agentscope.message import Msg, TextBlock
from agentscope.tool import Toolkit, ToolResponse

# Pre-import heavy dependencies to avoid first-request latency
from swe.agents.memory.base_memory_manager import BaseMemoryManager
from swe.agents.model_factory import (
    create_model_and_formatter,
    create_model_and_formatter_async,
)
from swe.agents.tools import read_file, write_file, edit_file
from swe.agents.utils import get_swe_token_counter
from swe.agents.utils.tool_output_compaction import (
    compact_tool_result_messages_async,
)
from swe.app.source_system_config import resolve_tool_result_compact_config
from swe.tracing import capture_current_trace_context
from swe.config import load_config
from swe.config.config import load_agent_config
from swe.config.context import (
    set_current_workspace_dir,
    set_current_recent_max_bytes,
)
from swe.constant import EnvVarLoader
from swe.runtime_workers import run_runtime_state_work

if TYPE_CHECKING:
    from reme.memory.file_based.reme_in_memory_memory import ReMeInMemoryMemory

logger = logging.getLogger(__name__)

_EXPECTED_REME_VERSION = "0.3.1.8"


def _clone_unbound_chat_memory(memory: Any) -> Any:
    """Create an empty Chat-local memory when a backend returns a singleton."""
    try:
        clone = copy.copy(memory)
    except (TypeError, copy.Error) as exc:
        raise RuntimeError(
            "ReMe memory backend returned a non-isolatable Chat memory",
        ) from exc

    if hasattr(clone, "content"):
        clone.content = []
    if hasattr(clone, "_compressed_summary"):
        clone._compressed_summary = ""
    # These are instance attributes installed by attach_conversation_archive;
    # removing them lets the class' original methods bind to the clone.
    for name in (
        "add",
        "get_memory",
        "clear_content",
        "clear_compressed_summary",
        "conversation_archive_store",
        "chat_checkpoint_store",
        "_chat_checkpoint_chat_id",
        "_chat_checkpoint_epoch",
        "_checkpoint_original_add",
        "_checkpoint_original_get_memory",
        "_checkpoint_original_clear_content",
        "_checkpoint_original_clear_compressed_summary",
    ):
        try:
            delattr(clone, name)
        except AttributeError:
            pass
    if not hasattr(type(clone), "add"):

        async def add(message: Msg) -> None:
            clone.content.append((message, []))

        clone.add = add
    if not hasattr(type(clone), "get_memory"):

        async def get_memory(*, prepend_summary: bool = True) -> list[Msg]:
            del prepend_summary
            return [message for message, _marks in clone.content]

        clone.get_memory = get_memory
    if not hasattr(type(clone), "clear_content"):

        def clear_content() -> None:
            clone.content.clear()

        clone.clear_content = clear_content
    if not hasattr(type(clone), "clear_compressed_summary"):

        def clear_compressed_summary() -> None:
            clone._compressed_summary = ""

        clone.clear_compressed_summary = clear_compressed_summary
    if not callable(getattr(clone, "add", None)) or not callable(
        getattr(clone, "get_memory", None),
    ):
        raise RuntimeError(
            "ReMe memory clone does not expose Chat memory operations",
        )
    return clone


def _exception_chain_messages(exc: Exception) -> list[str]:
    """Flatten exception/context/cause messages for lightweight matching."""
    messages = [str(exc)]
    current = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__ or current.__context__
        if current is not None:
            messages.append(str(current))
    return messages


def _is_optional_chromadb_import_error(exc: Exception) -> bool:
    """Return True when the import failure is due to optional chromadb."""
    messages = " | ".join(_exception_chain_messages(exc)).lower()
    return "chromadb" in messages or "clientapi" in messages


def _clear_cached_reme_modules() -> None:
    """Clear partially imported reme modules before a retry."""
    for module_name in list(sys.modules):
        if module_name == "reme" or module_name.startswith("reme."):
            del sys.modules[module_name]


def _install_chromadb_compat_shim() -> None:
    """Install a minimal chromadb shim so local ReMe backend can import.

    ReMe imports its Chroma vector store unconditionally. When chromadb is
    absent or broken, the class annotations in that module can fail during
    import even if SWE intends to use the local backend. The shim provides
    only the symbols needed for import-time annotations.
    """
    chromadb_module = types.ModuleType("chromadb")
    chromadb_config_module = types.ModuleType("chromadb.config")

    class ClientAPI:  # pylint: disable=too-few-public-methods
        """Import-time stub for chromadb.ClientAPI."""

    class Collection:  # pylint: disable=too-few-public-methods
        """Import-time stub for chromadb.Collection."""

    class Settings:  # pylint: disable=too-few-public-methods
        """Import-time stub for chromadb.config.Settings."""

    chromadb_module.ClientAPI = ClientAPI
    chromadb_module.Collection = Collection
    chromadb_module.config = chromadb_config_module
    chromadb_config_module.Settings = Settings

    sys.modules["chromadb"] = chromadb_module
    sys.modules["chromadb.config"] = chromadb_config_module


def _install_reme_file_logging_suppression() -> None:
    """Prevent ReMe from creating cwd/logs/<timestamp>.log files."""
    try:
        from loguru import logger as reme_logger
    except Exception as exc:
        logger.debug("Unable to patch ReMe file logging: %s", exc)
        return

    reme_application = sys.modules.get("reme.core.application")
    reme_utils = sys.modules.get("reme.core.utils")
    reme_logger_utils = sys.modules.get("reme.core.utils.logger_utils")
    if not reme_application or not reme_utils or not reme_logger_utils:
        logger.debug("ReMe logger modules are not loaded; skip file log patch")
        return

    def init_logger_without_file_logging(
        log_dir: str = "logs",  # pylint: disable=unused-argument
        level: str = "INFO",
        log_to_console: bool = True,
    ) -> None:
        reme_logger.remove()
        if log_to_console:
            reme_logger.add(
                sink=sys.stdout,
                level=level,
                format=(
                    "{time:YYYY-MM-DD HH:mm:ss} | {level} | "
                    "{file}:{line} | {function} | {message}"
                ),
                colorize=True,
            )

    reme_logger_utils.init_logger = init_logger_without_file_logging
    reme_utils.init_logger = init_logger_without_file_logging
    reme_application.init_logger = init_logger_without_file_logging


def _import_reme_light(memory_backend: str):
    """Import ReMeLight with a local-backend retry for chromadb failures."""
    try:
        reme_light = importlib.import_module("reme.reme_light").ReMeLight
        _install_reme_file_logging_suppression()
        return reme_light
    except Exception as exc:
        if (
            memory_backend == "chroma"
            or not _is_optional_chromadb_import_error(
                exc,
            )
        ):
            raise

        logger.warning(
            "ReMeLight import failed due to optional chromadb dependency. "
            "Retrying with a compatibility shim for local backend. Error: %s",
            exc,
        )
        _clear_cached_reme_modules()
        _install_chromadb_compat_shim()
        reme_light = importlib.import_module("reme.reme_light").ReMeLight
        _install_reme_file_logging_suppression()
        return reme_light


class ReMeLightMemoryManager(BaseMemoryManager):
    """Memory manager that wraps ReMeLight for SWE agents via composition.

    Holds a ``ReMeLight`` instance (``self._reme``) and delegates all
    lifecycle / search / compaction calls to it.

    Capabilities:
    - Conversation compaction via compact_memory()
    - Memory summarization with file tools via summary_memory()
    - Vector and full-text search via memory_search()
    """

    def __init__(
        self,
        working_dir: str,
        agent_id: str,
        tenant_id: str | None = None,
    ):
        """Initialize with ReMeLight.

        Args:
            working_dir: Working directory for memory storage.
            agent_id: Agent ID for config loading.
            tenant_id: Optional tenant identifier for scoped config access.

        Embedding priority: config > env var (EMBEDDING_API_KEY /
        EMBEDDING_BASE_URL / EMBEDDING_MODEL_NAME).
        Backend: MEMORY_STORE_BACKEND env var (auto/local/chroma,
        default auto).
        """
        super().__init__(
            working_dir=working_dir,
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
        self._reme_version_ok: bool = self._check_reme_version()
        self._reme = None

        logger.info(
            f"ReMeLightMemoryManager init: "
            f"agent_id={agent_id}, working_dir={working_dir}",
        )

        backend_env = EnvVarLoader.get_str("MEMORY_STORE_BACKEND", "auto")
        if backend_env == "auto":
            if platform.system() == "Windows":
                memory_backend = "local"
            else:
                try:
                    import chromadb  # noqa: F401 pylint: disable=unused-import

                    memory_backend = "chroma"
                except Exception as e:
                    logger.warning(
                        f"""
chromadb import failed, falling back to `local` backend.
This is often caused by an outdated system SQLite (requires >= 3.35).
Please upgrade your system SQLite to >= 3.35.
See: https://docs.trychroma.com/docs/overview/troubleshooting#sqlite
| Error: {e}
                        """,
                    )
                    memory_backend = "local"
        else:
            memory_backend = backend_env

        emb_config = self.get_embedding_config()
        vector_enabled = bool(emb_config["base_url"]) and bool(
            emb_config["model_name"],
        )

        log_cfg = {
            **emb_config,
            "api_key": self._mask_key(emb_config["api_key"]),
        }
        logger.info(
            f"Embedding config: {log_cfg}, vector_enabled={vector_enabled}",
        )

        fts_enabled = EnvVarLoader.get_bool("FTS_ENABLED", True)

        agent_config = self._load_agent_config()
        rebuild_on_start = (
            agent_config.running.memory_summary.rebuild_memory_index_on_start
        )

        reme_light_cls = _import_reme_light(memory_backend)

        self._reme = reme_light_cls(
            working_dir=working_dir,
            default_embedding_model_config=emb_config,
            default_file_store_config={
                "backend": memory_backend,
                "store_name": "swe",
                "vector_enabled": vector_enabled,
                "fts_enabled": fts_enabled,
            },
            default_file_watcher_config={
                "rebuild_index_on_start": rebuild_on_start,
            },
        )

        self.summary_toolkit = Toolkit()
        self.summary_toolkit.register_tool_function(read_file)
        self.summary_toolkit.register_tool_function(write_file)
        self.summary_toolkit.register_tool_function(edit_file)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mask_key(key: str) -> str:
        """Mask API key, showing first 5 chars only."""
        return key[:5] + "*" * (len(key) - 5) if len(key) > 5 else key

    @staticmethod
    def _check_reme_version() -> bool:
        """Return False (and warn) when installed reme-ai version
        mismatches."""
        try:
            installed = importlib.metadata.version("reme-ai")
        except importlib.metadata.PackageNotFoundError:
            return True
        if installed != _EXPECTED_REME_VERSION:
            logger.warning(
                f"reme-ai version mismatch: installed={installed}, "
                f"expected={_EXPECTED_REME_VERSION}. "
                f"Run `pip install reme-ai=={_EXPECTED_REME_VERSION}`"
                " to align.",
            )
            return False
        return True

    def _warn_if_version_mismatch(self) -> None:
        """Warn once per call if the cached version check failed."""
        if not self._reme_version_ok:
            logger.warning(
                "reme-ai version mismatch, "
                f"expected={_EXPECTED_REME_VERSION}. "
                f"Run `pip install reme-ai=={_EXPECTED_REME_VERSION}`"
                " to align.",
            )

    def _load_agent_config(self, tenant_id: str | None = None):
        resolved_tenant_id = (
            tenant_id if tenant_id is not None else self.tenant_id
        )
        return load_agent_config(
            self.agent_id,
            tenant_id=resolved_tenant_id,
        )

    def _create_execution_model_formatter(
        self,
        trace_context: dict | None = None,
    ):
        """为独立执行流程创建专用模型，避免复用共享 chat_model。"""
        self._warn_if_version_mismatch()
        return create_model_and_formatter(
            self.agent_id,
            trace_context=trace_context,
        )

    async def _create_execution_model_formatter_async(
        self,
        trace_context: dict | None = None,
    ):
        self._warn_if_version_mismatch()
        return await create_model_and_formatter_async(
            self.agent_id,
            trace_context=trace_context,
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_embedding_config(self) -> dict:
        """Return embedding config with priority:
        config > env var > default."""
        self._warn_if_version_mismatch()
        cfg = self._load_agent_config().running.embedding_config
        return {
            "backend": cfg.backend,
            "api_key": cfg.api_key
            or EnvVarLoader.get_str("EMBEDDING_API_KEY"),
            "base_url": cfg.base_url
            or EnvVarLoader.get_str("EMBEDDING_BASE_URL"),
            "model_name": cfg.model_name
            or EnvVarLoader.get_str("EMBEDDING_MODEL_NAME"),
            "dimensions": cfg.dimensions,
            "enable_cache": cfg.enable_cache,
            "use_dimensions": cfg.use_dimensions,
            "max_cache_size": cfg.max_cache_size,
            "max_input_length": cfg.max_input_length,
            "max_batch_size": cfg.max_batch_size,
        }

    async def restart_embedding_model(self):
        """Restart the embedding model with current config."""
        self._warn_if_version_mismatch()
        if self._reme is None:
            return
        await self._reme.restart(
            restart_config={
                "embedding_models": {"default": self.get_embedding_config()},
            },
        )

    # ------------------------------------------------------------------
    # BaseMemoryManager interface
    # ------------------------------------------------------------------

    async def start(self):
        """Start the ReMeLight lifecycle."""
        self._warn_if_version_mismatch()
        if self._reme is None:
            return None
        return await self._reme.start()

    async def close(self) -> bool:
        """Drain summary work before closing its shared backend."""
        async with self._summary_close_lock:
            if self._summary_backend_closed:
                return True
            if not await self._close_summary_tasks():
                return False
            self._warn_if_version_mismatch()
            result = True if self._reme is None else await self._reme.close()
            if result:
                self._summary_backend_closed = True
            return result

    async def compact_tool_result(
        self,
        *,
        messages: list[Msg],
        old_max_bytes: int,
        recent_max_bytes: int,
        recent_n: int,
        retention_days: int,
    ) -> list[Msg]:
        """Compact recoverable tool-result text in native message history."""
        self._warn_if_version_mismatch()
        workspace_dir = Path(self.working_dir)
        artifact_dir = workspace_dir / "tool_result"
        return await compact_tool_result_messages_async(
            messages,
            old_max_bytes=old_max_bytes,
            recent_max_bytes=recent_max_bytes,
            recent_n=recent_n,
            artifact_dir=artifact_dir,
            workspace_dir=workspace_dir,
            retention_days=retention_days,
        )

    async def check_context(self, **kwargs):
        """Check context size and determine if compaction is needed."""
        self._warn_if_version_mismatch()
        if self._reme is None:
            return None
        return await self._reme.check_context(**kwargs)

    async def compact_memory(
        self,
        messages: list[Msg],
        previous_summary: str = "",
        **kwargs,
    ) -> str:
        """Compact messages into a condensed summary.

        Returns the compacted string, or empty string on failure.
        """
        bound_chat_model = kwargs.pop("_bound_chat_model", None)
        bound_formatter = kwargs.pop("_bound_formatter", None)
        if bound_chat_model is None or bound_formatter is None:
            execution_trace_context = capture_current_trace_context()
            execution_model, execution_formatter = (
                await self._create_execution_model_formatter_async(
                    trace_context=execution_trace_context,
                )
            )
            if bound_chat_model is None:
                bound_chat_model = execution_model
            if bound_formatter is None:
                bound_formatter = execution_formatter

        agent_config = await run_runtime_state_work(self._load_agent_config)
        cc = agent_config.running.context_compact

        result = await self._reme.compact_memory(
            messages=messages,
            as_llm=bound_chat_model,
            as_llm_formatter=bound_formatter,
            as_token_counter=get_swe_token_counter(agent_config),
            language=agent_config.language,
            max_input_length=agent_config.running.max_input_length,
            compact_ratio=cc.memory_compact_ratio,
            previous_summary=previous_summary,
            return_dict=True,
            add_thinking_block=cc.compact_with_thinking_block,
        )

        if isinstance(result, str):
            logger.error(
                "compact_memory returned str instead of dict, "
                f"result: {result[:200]}... "
                "Please install the latest reme package.",
            )
            return result

        if not result.get("is_valid", True):
            unique_id = uuid.uuid4().hex[:8]
            filepath = os.path.join(
                agent_config.workspace_dir,
                f"compact_invalid_{unique_id}.json",
            )
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                logger.error(
                    f"Invalid compact result saved to {filepath}. "
                    f"user_msg: {result.get('user_message', '')[:200]}..., "
                    "history_compact: "
                    f"{result.get('history_compact', '')[:200]}...",
                )
                logger.error(
                    "Please upload the log: "
                    "https://github.com/agentscope-ai/SWE/issues",
                )
            except Exception as _e:
                logger.error(f"Failed to save invalid compact result: {_e}")
            return ""

        return result.get("history_compact", "")

    async def summary_memory(self, messages: list[Msg], **kwargs) -> str:
        """Generate a comprehensive summary of the given messages."""
        bound_chat_model = kwargs.pop("_bound_chat_model", None)
        bound_formatter = kwargs.pop("_bound_formatter", None)
        execution_trace_context = kwargs.pop("_summary_trace_context", None)
        if bound_chat_model is None or bound_formatter is None:
            if execution_trace_context is None:
                execution_trace_context = capture_current_trace_context()
            execution_model, execution_formatter = (
                await self._create_execution_model_formatter_async(
                    trace_context=execution_trace_context,
                )
            )
            if bound_chat_model is None:
                bound_chat_model = execution_model
            if bound_formatter is None:
                bound_formatter = execution_formatter

        agent_config = await run_runtime_state_work(self._load_agent_config)
        cc = agent_config.running.context_compact

        set_current_workspace_dir(Path(self.working_dir))
        tool_result_compact = resolve_tool_result_compact_config(
            agent_config.running.tool_result_compact,
        )
        set_current_recent_max_bytes(tool_result_compact.recent_max_bytes)

        return await self._reme.summary_memory(
            messages=messages,
            as_llm=bound_chat_model,
            as_llm_formatter=bound_formatter,
            as_token_counter=get_swe_token_counter(agent_config),
            toolkit=self.summary_toolkit,
            language=agent_config.language,
            max_input_length=agent_config.running.max_input_length,
            compact_ratio=cc.memory_compact_ratio,
            timezone=(await run_runtime_state_work(load_config)).user_timezone
            or None,
            add_thinking_block=cc.compact_with_thinking_block,
        )

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ) -> ToolResponse:
        """Search stored memories for relevant content."""
        self._warn_if_version_mismatch()
        if self._reme is None or not getattr(self._reme, "_started", False):
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text="ReMe is not started, report github issue!",
                    ),
                ],
            )
        return await self._reme.memory_search(
            query=query,
            max_results=max_results,
            min_score=min_score,
        )

    async def prepare_request_memory(
        self,
        chat_id: str,
    ) -> "ReMeInMemoryMemory | None":
        """Prepare isolated memory and its checkpoint lease off the loop."""
        return await run_runtime_state_work(
            self.create_request_memory,
            chat_id,
        )

    def create_request_memory(
        self,
        chat_id: str,
    ) -> "ReMeInMemoryMemory | None":
        """Create one isolated online memory owned by the current request."""
        self._warn_if_version_mismatch()
        if self._reme is None:
            return None
        agent_config = self._load_agent_config()
        memory = self._reme.get_in_memory_memory(
            as_token_counter=get_swe_token_counter(agent_config),
        )
        memory = _clone_unbound_chat_memory(memory)

        from .conversation_archive import attach_conversation_archive

        return attach_conversation_archive(
            memory,
            Path(self.working_dir) / "dialog",
            chat_id,
        )

    def get_in_memory_memory(
        self,
        chat_id: str | None = None,
        **_kwargs,
    ) -> "ReMeInMemoryMemory | None":
        """Compatibility wrapper; Chat memories are still request-owned."""
        if chat_id:
            return self.create_request_memory(chat_id)
        self._warn_if_version_mismatch()
        if self._reme is None:
            return None
        agent_config = self._load_agent_config()
        return _clone_unbound_chat_memory(
            self._reme.get_in_memory_memory(
                as_token_counter=get_swe_token_counter(agent_config),
            ),
        )

    async def archive_checkpoint_messages(
        self,
        *,
        chat_id: str,
        messages: list[Msg],
        candidate_id: str,
        memory: "ReMeInMemoryMemory",
    ) -> Any:
        """Archive source messages and install the selected Chat checkpoint."""
        return await memory.archive_checkpoint_messages(messages, candidate_id)

    async def schedule_precompaction(
        self,
        *,
        chat_id: str,
        watermark: int,
        messages: list[Msg],
        memory: "ReMeInMemoryMemory",
        **kwargs,
    ) -> bool:
        """Persist a revision-bound candidate without changing live memory."""
        del watermark, kwargs
        from .chat_checkpoint import EvidenceItem, PrecompactionCandidate

        source_message_ids = tuple(message.id for message in messages)
        online_message_ids = tuple(
            message.id for message, _marks in getattr(memory, "content", ())
        )
        if (
            not source_message_ids
            or len(source_message_ids) != len(set(source_message_ids))
            or len(online_message_ids) != len(set(online_message_ids))
        ):
            return False
        state = await memory.chat_checkpoint_store.read_checkpoint_prefix(
            chat_id,
            len(messages),
        )
        if len(state.events) < len(messages):
            return False
        selected_events = list(state.events[: len(messages)])
        for offset, (message, event) in enumerate(
            zip(messages, selected_events),
            start=1,
        ):
            if (
                event.sequence != state.record.applied_event_sequence + offset
                or event.type != "message_added"
                or dict(event.facts)
                != {"message_id": message.id, "role": message.role}
                or event.source_refs != (f"message:{message.id}",)
            ):
                return False
        applied_event_sequence = selected_events[-1].sequence
        event_context = tuple(
            EvidenceItem(
                text=(
                    f"{event.type}: "
                    + json.dumps(
                        event.to_dict()["facts"],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                ),
                evidence_refs=event.source_refs,
            )
            for event in selected_events
        )
        candidate_record = replace(
            state.record,
            revision=state.record.revision + 1,
            source_revision=state.record.revision,
            applied_event_sequence=applied_event_sequence,
            critical_context=(
                *state.record.critical_context,
                *event_context,
            ),
        )
        candidate = PrecompactionCandidate.new(
            record=candidate_record,
            base_revision=state.record.revision,
            applied_event_sequence=applied_event_sequence,
            source_message_ids=source_message_ids,
        )
        await memory.chat_checkpoint_store.write_pending_candidate(
            chat_id,
            candidate,
        )
        return True

    async def install_ready_precompaction(
        self,
        *,
        chat_id: str,
        messages: list[Msg] | None = None,
        memory: "ReMeInMemoryMemory",
    ) -> bool:
        """Install a still-valid candidate and refresh its Markdown projection."""
        if not messages:
            # Installing a cursor-advancing candidate without atomically
            # archiving its source prefix can hide journal evidence.
            return False
        return await memory.commit_ready_precompaction(messages)

    async def install_degraded_checkpoint(
        self,
        *,
        chat_id: str,
        messages: list[Msg],
        memory: "ReMeInMemoryMemory",
    ) -> bool:
        """Install a reference-only record before one emergency retry."""
        await memory.install_degraded_checkpoint(messages)
        return True

    async def recover_evidence(
        self,
        *,
        chat_id: str,
        epoch: int,
        **kwargs,
    ) -> Any:
        """Recover durable Chat evidence without acquiring online memory."""
        from .conversation_archive import ConversationArchiveStore

        archive_store = ConversationArchiveStore(
            Path(self.working_dir) / "dialog",
        )
        refs = kwargs.pop("refs", ())
        return await archive_store.recover_evidence(
            chat_id=chat_id,
            epoch=epoch,
            refs=refs,
            **kwargs,
        )

    async def reset_context_epoch(
        self,
        *,
        chat_id: str,
        reason: str,
        memory: "ReMeInMemoryMemory",
    ) -> Any:
        """Reset the Chat epoch while retaining physical archive evidence."""
        return await memory.reset_context_epoch(reason=reason)

    # ------------------------------------------------------------------
    # Dream-based memory optimization
    # ------------------------------------------------------------------

    # pylint: disable=too-many-statements
    async def dream_memory(
        self,
        tenant_id: str | None = None,
        trigger: str = "cron",
        **kwargs,
    ) -> None:
        """Run one dream-based memory optimization task.

        This method executes a dream agent that reads today's logs and
        existing MEMORY.md, then optimizes and consolidates the memory.

        Args:
            tenant_id: Optional tenant ID for tenant-scoped config lookup.
            trigger: Trigger type, "cron" or "manual".
            **kwargs: Additional keyword arguments (unused).
        """
        import time

        logger.info(
            "Running dream-based memory optimization (trigger=%s)",
            trigger,
        )
        start_time = time.time()

        execution_trace_context = capture_current_trace_context()
        dream_model, dream_formatter = (
            await self._create_execution_model_formatter_async(
                trace_context=execution_trace_context,
            )
        )

        # Load agent config with tenant_id for proper scoping
        agent_config = await run_runtime_state_work(
            load_agent_config,
            self.agent_id,
            tenant_id=tenant_id,
        )

        set_current_workspace_dir(Path(self.working_dir))
        tool_result_compact = resolve_tool_result_compact_config(
            agent_config.running.tool_result_compact,
        )
        set_current_recent_max_bytes(tool_result_compact.recent_max_bytes)

        # Determine language based on agent config
        language = getattr(agent_config, "language", "zh")

        # Get current date in YYYY-MM-DD format
        current_date = datetime.now().strftime("%Y-%m-%d")

        # Build the dream prompt
        query_text = self._get_dream_prompt(language, current_date)

        if not query_text.strip():
            logger.debug("Dream optimization skipped: empty query")
            return

        # Create backup directory
        backup_path = Path(self.working_dir).absolute() / "backup"
        backup_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        record_id = f"dream_{timestamp}"

        # Backup all context files before optimization
        context_files = [
            "MEMORY.md",
            "SOUL.md",
            "AGENTS.md",
            "PROFILE.md",
        ]

        backup_files: dict[str, Path] = {}
        for filename in context_files:
            file_path = Path(self.working_dir) / filename
            if file_path.exists():
                backup_filename = f"{filename.lower().replace('.md', '')}_backup_{timestamp}.md"
                backup_file = backup_path / backup_filename
                try:
                    shutil.copyfile(file_path, backup_file)
                    backup_files[filename] = backup_file
                    logger.info(f"Created {filename} backup: {backup_file}")
                except Exception as e:
                    logger.error(f"Failed to create {filename} backup: {e}")
            else:
                logger.debug(f"No existing {filename} file to backup")

        # Create a minimal ReActAgent for dream functionality
        dream_agent = ReActAgent(
            name="DreamOptimizer",
            model=dream_model,
            sys_prompt=(
                "You are a Dream Optimization Assistant. Your task is to organize "
                "and optimize memory files, NOT to redefine the Agent's identity.\n\n"
                "CRITICAL RULES:\n"
                "1. SOUL.md, AGENTS.md, PROFILE.md define the REAL Agent's identity "
                "   and behavior. Preserve their original core content.\n"
                "2. Only perform: remove redundancy, merge duplicates, delete "
                "outdated content.\n"
                "3. NEVER rewrite these files to describe yourself (DreamOptimizer).\n"
                "4. Keep the original Agent's persona, values, and capabilities intact.\n\n"
                "Your goal is memory optimization, not identity replacement."
            ),
            toolkit=self.summary_toolkit,
            formatter=dream_formatter,
        )

        # Build request message
        user_msg = Msg(
            name="dream",
            role="user",
            content=[TextBlock(type="text", text=query_text)],
        )

        # Get model name for logging
        model_used = getattr(dream_model, "model_name", "unknown")

        # Track token usage before
        input_tokens_before = getattr(dream_agent, "_total_input_tokens", 0)
        output_tokens_before = getattr(dream_agent, "_total_output_tokens", 0)

        try:
            response = await dream_agent.reply(user_msg)
            response_text = response.get_text_content()
            logger.debug(f"Dream agent response: {response_text}")

            # Calculate duration
            duration_ms = int((time.time() - start_time) * 1000)

            # Calculate file stats after optimization
            file_stats: dict[str, dict] = {}
            for filename, backup_file in backup_files.items():
                file_path = Path(self.working_dir) / filename
                if file_path.exists() and backup_file.exists():
                    stats = self._get_file_stats(file_path, backup_file)
                    # Record all files with backup info (needed for backup file listing)
                    # Files with no changes will have size_saved=0 and lines_removed=0
                    file_stats[filename] = stats

            # Get token usage after
            input_tokens = (
                getattr(dream_agent, "_total_input_tokens", 0)
                - input_tokens_before
            )
            output_tokens = (
                getattr(dream_agent, "_total_output_tokens", 0)
                - output_tokens_before
            )

            # Extract full summary from response
            summary = response_text if response_text else ""

            # Log success result
            self._log_dream_result(
                record_id=record_id,
                trigger=trigger,
                status="success",
                file_stats=file_stats,
                duration_ms=duration_ms,
                model_used=model_used,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                summary=summary,
            )

        except Exception as e:
            # Calculate duration for failure
            duration_ms = int((time.time() - start_time) * 1000)

            # Log failure result
            self._log_dream_result(
                record_id=record_id,
                trigger=trigger,
                status="failed",
                file_stats={},  # No stats on failure
                duration_ms=duration_ms,
                model_used=model_used,
                input_tokens=0,
                output_tokens=0,
                summary="",
                error=str(e),
            )

            logger.error("Dream-based memory optimization failed: %s", repr(e))
            raise

    def _get_dream_prompt(
        self,
        language: str = "zh",
        current_date: str = "",
    ) -> str:
        """Get the dream prompt based on language setting.

        Args:
            language: Language code ("zh" or "en").
            current_date: Current date string in YYYY-MM-DD format.

        Returns:
            Dream optimization prompt string.
        """
        zh_prompt = (
            "# 重要说明\n"
            "你是梦境优化助手，任务是对记忆文件进行整理优化。你**不是**要把自己定义"
            "成文件中的 Agent。SOUL.md、AGENTS.md、PROFILE.md 定义的是**真实 Agent**"
            "的身份与行为准则，请保留原有内容的核心定义，只做以下优化：\n"
            "- 精简冗余描述\n"
            "- 合并重复条目\n"
            "- 剔除过期内容\n"
            "**严禁**将这些文件改写成关于「梦境优化助手」或「DreamOptimizer」的内容。\n\n"
            "---\n\n"
            "现在进入梦境状态，对长期记忆和系统上下文进行全面优化整理。\n\n"
            f"当前日期: {current_date}\n\n"
            "【梦境优化原则】\n"
            "1. 极简去冗：严禁记录流水账、Bug修复细节或单次任务。"
            "仅保留「核心业务决策」、「确认的用户偏好」与「高价值可复用经验」。\n"
            "2. 状态覆写：若发现状态变更（如技术栈更改、配置更新），"
            "必须用新状态替换旧状态，严禁新旧矛盾信息并存。\n"
            "3. 归纳整合：主动将零碎的相似规则提炼、合并为通用性强的独立条目。\n"
            "4. 废弃剔除：主动删除已被证伪的假设或不再适用的陈旧条目。\n\n"
            "【梦境执行步骤】\n"
            "步骤 1 [加载]：调用 `read` 工具，依次读取以下文件：\n"
            f"  - `MEMORY.md`（长期记忆）\n"
            f"  - `memory/{current_date}.md`（今日日志）\n"
            "  - `SOUL.md`（Agent 人设与核心理念）\n"
            "  - `AGENTS.md`（系统提示与行为规则）\n"
            "  - `PROFILE.md`（Agent 档案信息）\n"
            "步骤 2 [梦境提纯]：对每个文件严格按照【梦境优化原则】进行优化：\n"
            "  - MEMORY.md：提炼高价值增量信息，去重合并，保持精简\n"
            "  - SOUL.md：精简冗余描述，保持原有 Agent 人设定义不变\n"
            "  - AGENTS.md：合并重复规则，保持原有行为准则不变\n"
            "  - PROFILE.md：精简档案描述，保持原有身份信息不变\n"
            "步骤 3 [落盘]：调用 `write` 或 `edit` 工具，将优化后的内容写入对应文件，"
            "保持清晰的层级与列表结构。\n"
            "步骤 4 [苏醒汇报]：从梦境中苏醒后，简短汇报：\n"
            "  1) 各文件新增/沉淀了哪些核心内容\n"
            "  2) 修正/删除了哪些过期内容\n"
        )

        en_prompt = (
            "# IMPORTANT NOTICE\n"
            "You are a Dream Optimization Assistant tasked with organizing memory "
            "files. You are **NOT** defining yourself as the Agent in these files. "
            "SOUL.md, AGENTS.md, and PROFILE.md define the **real Agent's** identity "
            "and behavior rules. Please preserve the original core definitions and "
            "only perform these optimizations:\n"
            "- Remove redundant descriptions\n"
            "- Merge duplicate entries\n"
            "- Delete outdated content\n"
            "**DO NOT** rewrite these files to describe 'Dream Optimizer' or "
            "'DreamOptimizer'.\n\n"
            "---\n\n"
            "Enter dream state for comprehensive optimization of long-term "
            "memory and system context files.\n\n"
            f"Current date: {current_date}\n\n"
            "[Dream Optimization Principles]\n"
            "1. Extreme Minimalism: Strictly forbid recording daily routines, "
            "specific bug-fix details, or one-off tasks. Retain ONLY 'core "
            "business decisions', 'confirmed user preferences', and "
            "'high-value reusable experiences'.\n"
            "2. State Overwrite: If a state change is detected (e.g., "
            "tech stack changes, config updates), you MUST replace the old "
            "state with the new one. Contradictory old and new information "
            "must not coexist.\n"
            "3. Inductive Consolidation: Proactively distill and merge "
            "fragmented, similar rules into highly universal, independent "
            "entries.\n"
            "4. Deprecation: Proactively delete hypotheses that have been "
            "proven false or outdated entries that no longer apply.\n\n"
            "[Dream Execution Steps]\n"
            "Step 1 [Load]: Invoke the `read` tool to read the following files:\n"
            f"  - `MEMORY.md` (long-term memory)\n"
            f"  - `memory/{current_date}.md` (today's log)\n"
            "  - `SOUL.md` (Agent persona and core values)\n"
            "  - `AGENTS.md` (system prompts and behavior rules)\n"
            "  - `PROFILE.md` (Agent profile information)\n"
            "Step 2 [Dream Purification]: Optimize each file following the "
            "[Dream Optimization Principles]:\n"
            "  - MEMORY.md: Extract high-value insights, deduplicate and merge\n"
            "  - SOUL.md: Simplify redundant descriptions, keep original Agent persona\n"
            "  - AGENTS.md: Merge duplicate rules, keep original behavior guidelines\n"
            "  - PROFILE.md: Simplify profile, keep original identity information\n"
            "Step 3 [Save]: Invoke `write` or `edit` tools to write optimized "
            "content to each file, maintaining clear hierarchy and structure.\n"
            "Step 4 [Awake Report]: After waking, briefly report:\n"
            "  1) What core content was added/consolidated in each file\n"
            "  2) What outdated content was corrected/deleted\n"
        )

        prompts = {
            "zh": zh_prompt,
            "en": en_prompt,
        }
        return prompts.get(language, prompts["en"])

    # ------------------------------------------------------------------
    # Dream logs storage
    # ------------------------------------------------------------------

    DREAM_LOGS_FILE = "dream_logs.json"
    MAX_DREAM_LOGS_RECORDS = 100

    def _get_file_stats(
        self,
        file_path: Path,
        backup_path: Path,
    ) -> dict:
        """Calculate file statistics before and after optimization.

        Args:
            file_path: Current file path (after optimization).
            backup_path: Backup file path (before optimization).

        Returns:
            Dict with size_before, size_after, size_saved, lines_before,
            lines_after, lines_removed, backup_path.
        """
        size_before = backup_path.stat().st_size if backup_path.exists() else 0
        size_after = file_path.stat().st_size if file_path.exists() else 0

        lines_before = 0
        lines_after = 0
        try:
            if backup_path.exists():
                lines_before = len(
                    backup_path.read_text(encoding="utf-8").splitlines(),
                )
            if file_path.exists():
                lines_after = len(
                    file_path.read_text(encoding="utf-8").splitlines(),
                )
        except Exception:
            pass

        return {
            "size_before": size_before,
            "size_after": size_after,
            "size_saved": max(0, size_before - size_after),
            "lines_before": lines_before,
            "lines_after": lines_after,
            "lines_removed": max(0, lines_before - lines_after),
            "backup_path": str(
                backup_path.relative_to(Path(self.working_dir)),
            ),
        }

    def _load_dream_logs(self) -> dict:
        """Load dream_logs.json from workspace directory.

        Returns:
            Dict with 'records' list and 'stats' summary.
        """
        log_path = Path(self.working_dir) / self.DREAM_LOGS_FILE
        if not log_path.exists():
            return {"records": [], "stats": self._get_empty_stats()}
        try:
            with open(log_path, encoding="utf-8") as f:
                data = json.load(f)
            if "records" not in data:
                data["records"] = []
            if "stats" not in data:
                data["stats"] = self._get_empty_stats()
            return data
        except Exception:
            return {"records": [], "stats": self._get_empty_stats()}

    def _get_empty_stats(self) -> dict:
        """Return empty stats structure."""
        return {
            "total_executions": 0,
            "success_count": 0,
            "failed_count": 0,
            "total_size_saved": 0,
            "total_lines_removed": 0,
            "total_files_changed": 0,
            "last_execution": None,
        }

    def _save_dream_logs(self, data: dict) -> None:
        """Save dream_logs.json to workspace directory.

        Args:
            data: Dict with 'records' list and 'stats' summary.
        """
        log_path = Path(self.working_dir) / self.DREAM_LOGS_FILE
        # Keep only last MAX_DREAM_LOGS_RECORDS
        if len(data["records"]) > self.MAX_DREAM_LOGS_RECORDS:
            data["records"] = data["records"][-self.MAX_DREAM_LOGS_RECORDS :]
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save dream logs: {e}")

    def _log_dream_result(
        self,
        record_id: str,
        trigger: str,
        status: str,
        file_stats: dict,
        duration_ms: int,
        model_used: str,
        input_tokens: int,
        output_tokens: int,
        summary: str = "",
        error: str | None = None,
    ) -> None:
        """Append dream optimization record to dream_logs.json.

        Args:
            record_id: Unique identifier for this optimization.
            trigger: "cron" or "manual".
            status: "success", "failed", or "cancelled".
            file_stats: Dict mapping filename to stats dict.
            duration_ms: Execution duration in milliseconds.
            model_used: Model name used for optimization.
            input_tokens: Input token count.
            output_tokens: Output token count.
            summary: Optimization summary text.
            error: Error message if failed.
        """
        data = self._load_dream_logs()

        # Calculate totals
        total_size_saved = sum(
            fs.get("size_saved", 0) for fs in file_stats.values()
        )
        total_lines_removed = sum(
            fs.get("lines_removed", 0) for fs in file_stats.values()
        )
        # Only count files with actual changes
        changed_files = [
            fname
            for fname, fs in file_stats.items()
            if fs.get("size_saved", 0) > 0 or fs.get("lines_removed", 0) > 0
        ]
        total_files_changed = len(changed_files)

        record = {
            "id": record_id,
            "timestamp": datetime.now().isoformat(),
            "trigger": trigger,
            "status": status,
            "files_optimized": changed_files,
            "file_stats": file_stats,
            "total_size_saved": total_size_saved,
            "total_files_changed": total_files_changed,
            "total_lines_removed": total_lines_removed,
            "duration_ms": duration_ms,
            "model_used": model_used,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "summary": summary,
            "error": error,
        }

        data["records"].append(record)

        # Update stats
        stats = data["stats"]
        stats["total_executions"] += 1
        if status == "success":
            stats["success_count"] += 1
        elif status == "failed":
            stats["failed_count"] += 1
        stats["total_size_saved"] += total_size_saved
        stats["total_lines_removed"] += total_lines_removed
        stats["total_files_changed"] += total_files_changed
        stats["total_duration_ms"] = (
            stats.get("total_duration_ms", 0) + duration_ms
        )
        stats["last_execution"] = record["timestamp"]

        self._save_dream_logs(data)
        logger.info(f"Saved dream log record: {record_id}")

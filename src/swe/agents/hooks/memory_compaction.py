# -*- coding: utf-8 -*-
"""Memory compaction hook for managing context window.

This hook monitors token usage and automatically compacts older messages
when the context window approaches its limit, preserving recent messages
and the system prompt.
"""

import asyncio
import logging
from dataclasses import dataclass
from math import floor
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from agentscope.agent import ReActAgent
from agentscope.message import Msg, TextBlock
from swe.constant import MEMORY_COMPACT_KEEP_RECENT

from ...app.source_system_config import resolve_tool_result_compact_config
from ..utils import (
    check_valid_messages,
    get_swe_token_counter,
)
from ...config.config import load_agent_config

if TYPE_CHECKING:
    from ..memory import BaseMemoryManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextBudgetDecision:
    """One deterministic decision for the configured context budget stages."""

    projected_tokens: int
    ratio: float
    stage: Literal["normal", "governance", "active", "emergency"]
    precompaction_watermark: int | None


def decide_context_budget(
    projected_tokens: int,
    max_input_length: int,
    config: Any,
) -> ContextBudgetDecision:
    """Classify use into the confirmed 65/5/80/90 staged policy."""
    if max_input_length <= 0:
        raise ValueError("max_input_length must be positive")
    ratio = max(projected_tokens, 0) / max_input_length
    if ratio >= config.emergency_compact_ratio:
        return ContextBudgetDecision(
            projected_tokens,
            ratio,
            "emergency",
            None,
        )
    if ratio >= config.memory_compact_ratio:
        return ContextBudgetDecision(projected_tokens, ratio, "active", None)
    if ratio >= config.lightweight_governance_ratio:
        watermark = floor(
            (ratio - config.lightweight_governance_ratio)
            / config.precompaction_step_ratio
            + 1e-9,
        )
        return ContextBudgetDecision(
            projected_tokens,
            ratio,
            "governance",
            watermark,
        )
    return ContextBudgetDecision(projected_tokens, ratio, "normal", None)


class MemoryCompactionHook:
    """Hook for automatic memory compaction when context is full.

    This hook monitors the token count of messages and triggers compaction
    when it exceeds the threshold. It preserves the system prompt and recent
    messages while summarizing older conversation history.
    """

    def __init__(self, memory_manager: "BaseMemoryManager"):
        """Initialize memory compaction hook.

        Args:
            memory_manager: Memory manager instance for compaction
        """
        self.memory_manager = memory_manager
        self._precompaction_watermarks: dict[tuple[str, int], int] = {}
        self._precompaction_tasks: dict[tuple[str, int], asyncio.Task] = {}

    async def _apply_checkpoint_budget_stage(
        self,
        agent: ReActAgent,
        running_config: Any,
        messages: list[Msg],
        projected_tokens: int,
        remeasure_projected_tokens: Callable[[], Awaitable[int]] | None = None,
    ) -> bool:
        """Apply staged checkpoint policy; return whether legacy flow stops."""
        context = self._checkpoint_budget_context(
            agent,
            running_config,
            projected_tokens,
        )
        if context is None:
            return False
        chat_id, compact_config, decision, watermark_key = context
        if decision.stage == "normal":
            return True
        if decision.stage == "governance":
            self._schedule_governance_precompaction(
                agent,
                messages,
                chat_id,
                decision,
                watermark_key,
            )
            return True
        return await self._install_checkpoint_stage(
            agent,
            running_config,
            messages,
            chat_id,
            decision,
            remeasure_projected_tokens,
        )

    @staticmethod
    def _checkpoint_budget_context(
        agent: ReActAgent,
        running_config: Any,
        projected_tokens: int,
    ) -> (
        tuple[
            str,
            Any,
            ContextBudgetDecision,
            tuple[str, int],
        ]
        | None
    ):
        """Return the stable checkpoint routing inputs for this request."""
        compact_config = getattr(running_config, "context_compact", None)
        required = (
            "lightweight_governance_ratio",
            "precompaction_step_ratio",
            "memory_compact_ratio",
            "emergency_compact_ratio",
        )
        if compact_config is None or not all(
            hasattr(compact_config, name) for name in required
        ):
            return None
        chat_id = str(
            getattr(agent, "_request_context", {}).get("chat_id") or "",
        )
        if not chat_id:
            return None
        decision = decide_context_budget(
            projected_tokens,
            running_config.max_input_length,
            compact_config,
        )
        epoch = int(
            getattr(
                getattr(agent, "memory", None),
                "_chat_checkpoint_epoch",
                1,
            ),
        )
        return chat_id, compact_config, decision, (chat_id, epoch)

    def _schedule_governance_precompaction(
        self,
        agent: ReActAgent,
        messages: list[Msg],
        chat_id: str,
        decision: ContextBudgetDecision,
        watermark_key: tuple[str, int],
    ) -> None:
        """Start one new non-blocking precompaction task for a watermark."""
        watermark = decision.precompaction_watermark
        if watermark is None:
            return
        if watermark <= self._precompaction_watermarks.get(
            watermark_key,
            -1,
        ):
            return
        task = self._precompaction_tasks.get(watermark_key)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(
            self.memory_manager.schedule_precompaction(
                chat_id=chat_id,
                watermark=watermark,
                messages=messages,
                memory=agent.memory,
                chat_model=agent.model,
                formatter=agent.formatter,
            ),
        )
        self._precompaction_tasks[watermark_key] = task
        self._precompaction_watermarks[watermark_key] = watermark
        task.add_done_callback(
            lambda completed: self._record_precompaction_result(
                completed,
                watermark_key,
                watermark,
            ),
        )

    def _record_precompaction_result(
        self,
        completed: asyncio.Task,
        watermark_key: tuple[str, int],
        watermark: int,
    ) -> None:
        """Clear a failed watermark while leaving newer schedules intact."""
        if completed.cancelled():
            return
        try:
            if completed.result():
                return
        except Exception:
            logger.exception("Checkpoint precompaction failed")
        if self._precompaction_watermarks.get(watermark_key) == watermark:
            self._precompaction_watermarks.pop(watermark_key, None)

    async def _install_checkpoint_stage(
        self,
        agent: ReActAgent,
        running_config: Any,
        messages: list[Msg],
        chat_id: str,
        decision: ContextBudgetDecision,
        remeasure_projected_tokens: Callable[[], Awaitable[int]] | None,
    ) -> bool:
        """Install ready or degraded state and decide whether legacy flow stops."""
        installed = await self.memory_manager.install_ready_precompaction(
            chat_id=chat_id,
            messages=messages,
            memory=agent.memory,
        )
        if installed:
            return await self._is_legacy_fallback_avoided(
                running_config,
                remeasure_projected_tokens,
            )
        if decision.stage != "emergency":
            return False
        install_degraded = getattr(
            self.memory_manager,
            "install_degraded_checkpoint",
            None,
        )
        if install_degraded is None:
            return False
        await install_degraded(
            chat_id=chat_id,
            messages=messages,
            memory=agent.memory,
        )
        return await self._is_legacy_fallback_avoided(
            running_config,
            remeasure_projected_tokens,
            default_result=False,
        )

    @staticmethod
    async def _is_legacy_fallback_avoided(
        running_config: Any,
        remeasure_projected_tokens: Callable[[], Awaitable[int]] | None,
        *,
        default_result: bool = True,
    ) -> bool:
        """Return whether a checkpoint install brought usage below fallback."""
        if remeasure_projected_tokens is None:
            return default_result
        remeasured = await remeasure_projected_tokens()
        compact_config = running_config.context_compact
        return decide_context_budget(
            remeasured,
            running_config.max_input_length,
            compact_config,
        ).stage in ("normal", "governance")

    @staticmethod
    async def _print_status_message(
        agent: ReActAgent,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Print a status message to the agent's output.

        Args:
            agent: The agent instance to print the message for.
            text: The text content of the status message.
        """
        msg = Msg(
            name=agent.name,
            role="assistant",
            content=[TextBlock(type="text", text=text)],
            metadata=metadata or {},
        )
        await agent.print(msg)

    async def _get_left_compact_threshold(
        self,
        agent: ReActAgent,
        running_config: Any,
        token_counter: Any,
    ) -> int | None:
        memory = agent.memory
        str_token_count = await token_counter.count(
            messages=[],
            text=(agent.sys_prompt or "")
            + (memory.get_compressed_summary() or ""),
        )
        left_compact_threshold = (
            running_config.memory_compact_threshold - str_token_count
        )
        if left_compact_threshold > 0:
            return left_compact_threshold

        logger.warning(
            "The memory_compact_threshold is set too low; "
            "the combined token length of system_prompt and "
            "compressed_summary exceeds the configured threshold. "
            "Alternatively, you could use /clear to reset the context "
            "and compressed_summary, ensuring the total remains "
            "below the threshold.",
        )
        return None

    @staticmethod
    async def _count_projected_tokens(
        token_counter: Any,
        messages: list[Msg],
        fixed_text: str,
    ) -> int:
        """Count online messages and fixed prompt context independently."""
        message_tokens = await token_counter.count(
            messages=[message.to_dict() for message in messages],
        )
        fixed_text_tokens = await token_counter.count(
            messages=[],
            text=fixed_text,
        )
        return message_tokens + fixed_text_tokens

    async def _compact_tool_results_if_enabled(
        self,
        messages: list[Msg],
        running_config: Any,
    ) -> None:
        # source 显式覆盖只影响本请求，缺失字段继续继承 Agent 配置。
        trc = resolve_tool_result_compact_config(
            running_config.tool_result_compact,
        )
        if not trc.enabled:
            return

        await self.memory_manager.compact_tool_result(
            messages=messages,
            recent_n=trc.recent_n,
            old_max_bytes=trc.old_max_bytes,
            recent_max_bytes=trc.recent_max_bytes,
            retention_days=trc.retention_days,
        )

    @staticmethod
    def _compactable_messages_for_invalid_context(
        messages: list[Msg],
    ) -> list[Msg]:
        keep_length: int = MEMORY_COMPACT_KEEP_RECENT
        messages_length = len(messages)
        while keep_length > 0 and not check_valid_messages(
            messages[max(messages_length - keep_length, 0) :],
        ):
            keep_length -= 1

        if keep_length <= 0:
            return messages

        return messages[: max(messages_length - keep_length, 0)]

    async def _get_messages_to_compact(
        self,
        messages: list[Msg],
        running_config: Any,
        token_counter: Any,
        left_compact_threshold: int,
    ) -> list[Msg]:
        (
            messages_to_compact,
            _,
            is_valid,
        ) = await self.memory_manager.check_context(
            messages=messages,
            memory_compact_threshold=left_compact_threshold,
            memory_compact_reserve=running_config.memory_compact_reserve,
            as_token_counter=token_counter,
        )

        if is_valid or not messages_to_compact:
            return messages_to_compact

        logger.warning(
            "Please include the output of the /history command when "
            "reporting the bug to the community. Invalid messages=%s",
            messages,
        )
        return self._compactable_messages_for_invalid_context(messages)

    @staticmethod
    def _get_scope_id(agent: ReActAgent) -> str | None:
        scope_id = str(
            getattr(agent, "_request_context", {}).get(
                "session_id",
                "",
            )
            or "",
        )
        return scope_id or None

    def _add_summary_task_if_enabled(
        self,
        agent: ReActAgent,
        running_config: Any,
        messages_to_compact: list[Msg],
    ) -> None:
        if not running_config.memory_summary.memory_summary_enabled:
            return

        self.memory_manager.add_async_summary_task(
            messages=messages_to_compact,
            chat_model=agent.model,
            formatter=agent.formatter,
            scope_id=self._get_scope_id(agent),
        )

    async def _run_context_compaction(
        self,
        agent: ReActAgent,
        messages_to_compact: list[Msg],
        running_config: Any,
    ) -> str:
        if not running_config.context_compact.context_compact_enabled:
            return ""

        compact_content = await self.memory_manager.compact_memory(
            messages=messages_to_compact,
            previous_summary=agent.memory.get_compressed_summary(),
            _bound_chat_model=agent.model,
            _bound_formatter=agent.formatter,
        )
        if not compact_content:
            await self._print_status_message(
                agent,
                "⚠️ Context compaction failed.",
            )
        return compact_content

    async def _persist_compaction_result(
        self,
        memory: Any,
        messages_to_compact: list[Msg],
        compact_content: str,
    ) -> Any | None:
        archive_messages = getattr(memory, "archive_compacted_messages", None)
        if archive_messages is not None:
            boundary = await archive_messages(messages_to_compact)
            updated_count = len(messages_to_compact)
        else:
            boundary = None
            updated_count = await memory.mark_messages_compressed(
                messages_to_compact,
            )
        logger.info("Marked %s messages as compacted", updated_count)
        await memory.update_compressed_summary(compact_content)
        from ...token_diagnostics.runtime import notify_compaction

        notify_compaction()
        return boundary

    async def __call__(
        self,
        agent: ReActAgent,
        kwargs: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Pre-reasoning hook to check and compact memory if needed.

        This hook extracts system prompt messages and recent messages,
        builds an estimated full context prompt, and triggers compaction
        when the total estimated token count exceeds the threshold.

        Memory structure:
            [System Prompt (preserved)] + [Compactable (counted)] +
            [Recent (preserved)]

        Args:
            agent: The agent instance
            kwargs: Input arguments to the _reasoning method

        Returns:
            None (hook doesn't modify kwargs)
        """
        try:
            # Get hot-reloaded agent config
            agent_config = load_agent_config(
                self.memory_manager.agent_id,
                tenant_id=getattr(self.memory_manager, "tenant_id", None),
            )
            running_config = agent_config.running
            token_counter = get_swe_token_counter(agent_config)
            memory = agent.memory

            left_compact_threshold = await self._get_left_compact_threshold(
                agent,
                running_config,
                token_counter,
            )
            if left_compact_threshold is None:
                return None

            messages = await memory.get_memory(prepend_summary=False)
            await self._compact_tool_results_if_enabled(
                messages,
                running_config,
            )
            if not running_config.context_compact.context_compact_enabled:
                return None

            messages_to_compact = await self._get_messages_to_compact(
                messages,
                running_config,
                token_counter,
                left_compact_threshold,
            )
            candidate_messages = (
                messages_to_compact
                or messages[
                    : max(len(messages) - MEMORY_COMPACT_KEEP_RECENT, 0)
                ]
            )
            while candidate_messages and not check_valid_messages(
                candidate_messages,
            ):
                # Preserve the oldest complete interaction prefix; never split
                # a tool_use/tool_result transaction at the candidate edge.
                candidate_messages = candidate_messages[:-1]

            projected_tokens = await self._count_projected_tokens(
                token_counter,
                messages,
                (agent.sys_prompt or "")
                + (memory.get_compressed_summary() or ""),
            )

            async def remeasure_projected_tokens() -> int:
                refreshed_messages = await memory.get_memory(
                    prepend_summary=False,
                )
                return await self._count_projected_tokens(
                    token_counter,
                    refreshed_messages,
                    (agent.sys_prompt or "")
                    + (memory.get_compressed_summary() or ""),
                )

            if await self._apply_checkpoint_budget_stage(
                agent,
                running_config,
                candidate_messages,
                projected_tokens,
                remeasure_projected_tokens,
            ):
                return None
            # A candidate or emergency record may have changed online memory
            # during the re-measurement. Derive the one permitted legacy ReMe
            # fallback from that current snapshot, never a stale prefix.
            messages = await memory.get_memory(prepend_summary=False)
            messages_to_compact = await self._get_messages_to_compact(
                messages,
                running_config,
                token_counter,
                left_compact_threshold,
            )
            if not messages_to_compact:
                return None

            self._add_summary_task_if_enabled(
                agent,
                running_config,
                messages_to_compact,
            )
            compact_content = await self._run_context_compaction(
                agent,
                messages_to_compact,
                running_config,
            )
            if not compact_content:
                return None

            boundary = await self._persist_compaction_result(
                memory,
                messages_to_compact,
                compact_content,
            )
            if boundary is not None:
                await self._print_status_message(
                    agent,
                    "",
                    metadata={
                        "conversation_compaction_boundary": boundary.to_dict(),
                    },
                )

        except Exception as e:
            logger.exception(
                "Failed to compact memory in pre_reasoning hook: %s",
                e,
                exc_info=True,
            )

        return None

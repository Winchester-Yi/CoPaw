# -*- coding: utf-8 -*-
"""Bound tool-result output before it reaches AgentScope print and memory."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from agentscope.message import Msg

from swe.app.source_system_config.runtime import (
    resolve_tool_result_compact_config,
)
from swe.constant import WORKING_DIR

from .utils.tool_output_compaction import compact_tool_result_messages_async
from .tools.shell_output_spool import bind_shell_output_spool


class ToolOutputBudgetMixin:
    """Apply the recoverable tool-output budget at AgentScope's boundary."""

    async def _acting(self, tool_call) -> dict | None:
        """Execute a tool call with its displayed and stored output bounded."""
        tool_res_msg = Msg(
            "system",
            [
                {
                    "type": "tool_result",
                    "id": tool_call["id"],
                    "name": tool_call["name"],
                    "output": [],
                },
            ],
            "system",
        )
        try:
            tool_result_compact = resolve_tool_result_compact_config(
                self._agent_config.running.tool_result_compact,
            )
            workspace_dir = Path(self._workspace_dir or WORKING_DIR)

            with (
                (
                    bind_shell_output_spool(
                        workspace_dir,
                        tool_result_compact.recent_max_bytes,
                    )
                    if tool_result_compact.enabled
                    else nullcontext()
                ),
            ):
                tool_res = await self.toolkit.call_tool_function(tool_call)
                async for chunk in tool_res:
                    output: Any = chunk.content
                    # Cancellation during compaction must retain this chunk.
                    tool_res_msg.content[0]["output"] = output
                    if tool_result_compact.enabled:
                        compacted_chunk_msg = Msg(
                            "system",
                            [
                                {
                                    "type": "tool_result",
                                    "id": tool_call["id"],
                                    "name": tool_call["name"],
                                    "output": output,
                                },
                            ],
                            "system",
                        )
                        await compact_tool_result_messages_async(
                            [compacted_chunk_msg],
                            old_max_bytes=tool_result_compact.recent_max_bytes,
                            recent_max_bytes=tool_result_compact.recent_max_bytes,
                            recent_n=1,
                            artifact_dir=workspace_dir / "tool_result",
                            workspace_dir=workspace_dir,
                        )
                        output = compacted_chunk_msg.content[0]["output"]

                    tool_res_msg.content[0]["output"] = output
                    await self.print(tool_res_msg, chunk.is_last)

                    if chunk.is_interrupted:
                        raise asyncio.CancelledError()

                    if (
                        tool_call["name"] == self.finish_function_name
                        and chunk.metadata
                        and chunk.metadata.get("success", False)
                    ):
                        return chunk.metadata.get("structured_output")

            return None
        finally:
            await self.memory.add(tool_res_msg)

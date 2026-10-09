"""Result contract shared by Cron execution engines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ExecutionResult:
    trace_id: str = ""
    output_preview: str = ""
    input_snapshot: Optional[Dict[str, Any]] = None
    executor_leader: str = ""
    execution_meta: Optional[Dict[str, Any]] = None
    status: str = "success"

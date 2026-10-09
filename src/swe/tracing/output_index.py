"""Index rendered Cron output for trace detail views."""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def index_trace_output(trace_id: str, output: str) -> None:
    """Best-effort update of the existing trace output projection."""
    monitor_url = os.environ.get(
        "SWE_MONITOR_API_URL",
        "http://127.0.0.1:9090",
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{monitor_url}/monitor/tracing/model-output",
                json={"trace_id": trace_id, "model_output": output},
            )
        if response.status_code == 200:
            result = response.json()
            if result.get("status") == "success":
                logger.info("Trace output indexed: trace_id=%s", trace_id)
            else:
                logger.info(
                    "Trace output write skipped: trace_id=%s reason=%s",
                    trace_id,
                    result.get("reason", "unknown"),
                )
        else:
            logger.warning(
                "Trace output index returned status=%s trace_id=%s",
                response.status_code,
                trace_id,
            )
    except Exception:
        logger.warning(
            "Trace output indexing failed: trace_id=%s",
            trace_id,
            exc_info=True,
        )

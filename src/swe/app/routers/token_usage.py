# -*- coding: utf-8 -*-
"""Token usage API for console and skill tool."""

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query

from ...token_usage import get_token_usage_manager, TokenUsageSummary
from ...tracing import has_trace_manager, get_trace_manager

router = APIRouter(prefix="/token-usage", tags=["token-usage"])


def _parse_date(s: str | None) -> date | None:
    """Parse YYYY-MM-DD string to date."""
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _to_datetime(d: date) -> datetime:
    """Convert date to datetime at start of day."""
    return datetime.combine(d, datetime.min.time())


@router.get(
    "",
    summary="Get token usage summary",
    description="Return token usage aggregated by date, model, and provider",
)
async def get_token_usage(
    start_date: str
    | None = Query(
        None,
        description="Start date YYYY-MM-DD (inclusive). Default: 30 days ago",
    ),
    end_date: str
    | None = Query(
        None,
        description="End date YYYY-MM-DD (inclusive). Default: today",
    ),
    model: str
    | None = Query(
        None,
        description="Filter by model name",
    ),
    provider: str
    | None = Query(
        None,
        description="Filter by provider ID",
    ),
) -> TokenUsageSummary:
    """Return token usage summary for the given date range.

    When tracing is enabled, data is queried from the tracing system.
    Otherwise, falls back to the legacy token_usage.json file.
    """
    end_d = _parse_date(end_date) or date.today()
    start_d = _parse_date(start_date) or (end_d - timedelta(days=30))
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    # Try to use tracing data first
    if has_trace_manager():
        try:
            trace_mgr = get_trace_manager()
            if trace_mgr.enabled:
                return await trace_mgr.store.get_token_summary(
                    start_date=_to_datetime(start_d),
                    end_date=_to_datetime(end_d)
                    + timedelta(days=1),  # Include end date
                    model_name=model,
                    provider_id=provider,
                )
        except RuntimeError:
            pass  # Fall through to legacy

    # Fallback to legacy token_usage.json
    return await get_token_usage_manager().get_summary(
        start_date=start_d,
        end_date=end_d,
        model_name=model,
        provider_id=provider,
    )

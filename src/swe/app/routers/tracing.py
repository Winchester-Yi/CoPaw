# -*- coding: utf-8 -*-
# pylint: disable=too-many-branches too-many-statements too-many-locals
"""Tracing API router.

Provides REST API endpoints for tracing analytics.
"""
from datetime import datetime, timedelta
from typing import Optional
import io
import csv
import logging

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse

from ...tracing import get_trace_manager, has_trace_manager
from ...tracing.models import (
    OverviewStats,
    UserStats,
    TraceDetail,
    SessionStats,
)

router = APIRouter(prefix="/tracing", tags=["tracing"])
logger = logging.getLogger(__name__)


def _parse_date(
    date_str: Optional[str],
    field_name: str,
    add_day: bool = False,
) -> Optional[datetime]:
    """Parse a date string to datetime.

    Args:
        date_str: Date string in YYYY-MM-DD format
        field_name: Field name for error message
        add_day: Whether to add one day to include the end date

    Returns:
        Parsed datetime or None

    Raises:
        HTTPException: If date format is invalid
    """
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if add_day:
            dt = dt + timedelta(days=1)
        return dt
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name} format",
        ) from exc


def _check_tracing_available() -> None:
    """Check if tracing is available, raise HTTPException if not."""
    if not has_trace_manager():
        raise HTTPException(
            status_code=503,
            detail="Tracing not available. Enable tracing in configuration.",
        )


@router.get("/overview", response_model=OverviewStats)
async def get_overview(
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> OverviewStats:
    """Get overview statistics for the dashboard.

    Args:
        start_date: Optional start date filter
        end_date: Optional end date filter

    Returns:
        Overview statistics including user counts, token usage,
        model distribution.
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        # Tracing not initialized, return empty stats
        return OverviewStats()

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    return await manager.store.get_overview_stats(start, end)


@router.get("/users", response_model=dict)
async def get_users(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    user_id: Optional[str] = Query(
        None,
        description="Filter by user ID (partial match)",
    ),
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> dict:
    """Get list of users with their statistics.

    Args:
        page: Page number
        page_size: Page size
        user_id: Filter by user ID
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Paginated list of users with stats
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    users, total = await manager.store.get_users(
        page,
        page_size,
        user_id,
        start,
        end,
    )
    return {
        "items": [u.model_dump() for u in users],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/users/{user_id}", response_model=UserStats)
async def get_user_stats(
    user_id: str,
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> UserStats:
    """Get statistics for a specific user.

    Args:
        user_id: User identifier
        start_date: Optional start date filter
        end_date: Optional end date filter

    Returns:
        User statistics
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return UserStats(user_id=user_id)

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    return await manager.store.get_user_stats(user_id, start, end)


@router.get("/traces", response_model=dict)
async def get_traces(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    session_id: Optional[str] = Query(
        None,
        description="Filter by session ID",
    ),
    status: Optional[str] = Query(None, description="Filter by status"),
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> dict:
    """Get list of traces.

    Args:
        page: Page number
        page_size: Page size
        user_id: Filter by user ID
        session_id: Filter by session ID
        status: Filter by status (running, completed, error, cancelled)
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Paginated list of traces
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    traces, total = await manager.store.get_traces(
        page=page,
        page_size=page_size,
        user_id=user_id,
        session_id=session_id,
        status=status,
        start_date=start,
        end_date=end,
    )
    return {
        "items": [t.model_dump() for t in traces],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/traces/{trace_id}", response_model=TraceDetail)
async def get_trace_detail(trace_id: str) -> TraceDetail:
    """Get detailed trace with spans.

    Args:
        trace_id: Trace identifier

    Returns:
        Trace detail with all spans

    Raises:
        HTTPException: If trace not found
    """
    try:
        manager = get_trace_manager()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Tracing not available",
        ) from exc

    detail = await manager.store.get_trace_detail(trace_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    return detail


@router.get("/models", response_model=dict)
async def get_model_usage(
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> dict:
    """Get model usage statistics.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Model usage statistics
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"models": []}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    stats = await manager.store.get_overview_stats(start, end)
    return {"models": [m.model_dump() for m in stats.model_distribution]}


@router.get("/tools", response_model=dict)
async def get_tool_usage(
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> dict:
    """Get tool usage statistics.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Tool usage statistics
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"tools": []}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    stats = await manager.store.get_overview_stats(start, end)
    return {"tools": [t.model_dump() for t in stats.top_tools]}


@router.get("/skills", response_model=dict)
async def get_skill_usage(
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> dict:
    """Get skill usage statistics.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Skill usage statistics
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"skills": []}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    stats = await manager.store.get_overview_stats(start, end)
    return {"skills": [s.model_dump() for s in stats.top_skills]}


@router.get("/mcp", response_model=dict)
async def get_mcp_usage(
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> dict:
    """Get MCP tool and server usage statistics.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        MCP usage statistics
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"mcp_tools": [], "mcp_servers": []}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    stats = await manager.store.get_overview_stats(start, end)
    return {
        "mcp_tools": [t.model_dump() for t in stats.top_mcp_tools],
        "mcp_servers": [s.model_dump() for s in stats.mcp_servers],
    }


@router.get("/sessions", response_model=dict)
async def get_sessions(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    session_id: Optional[str] = Query(
        None,
        description="Filter by session ID (partial match)",
    ),
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> dict:
    """Get list of sessions with their statistics.

    Args:
        page: Page number
        page_size: Page size
        user_id: Filter by user ID
        session_id: Filter by session ID (partial match)
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Paginated list of sessions with stats
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    sessions, total = await manager.store.get_sessions(
        page=page,
        page_size=page_size,
        user_id=user_id,
        session_id=session_id,
        start_date=start,
        end_date=end,
    )
    return {
        "items": [s.model_dump() for s in sessions],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/sessions/{session_id:path}", response_model=SessionStats)
async def get_session_stats(
    session_id: str,
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
) -> SessionStats:
    """Get statistics for a specific session.

    Args:
        session_id: Session identifier
        start_date: Optional start date filter
        end_date: Optional end date filter

    Returns:
        Session statistics
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return SessionStats(session_id=session_id, user_id="", channel="")

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    return await manager.store.get_session_stats(session_id, start, end)


@router.get("/user-messages", response_model=dict)
async def get_user_messages(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Page size"),
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    session_id: Optional[str] = Query(
        None,
        description="Filter by session ID",
    ),
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    query: Optional[str] = Query(
        None,
        description="Search in user message content",
    ),
) -> dict:
    """Get user messages with token info for cost analysis.

    Args:
        page: Page number
        page_size: Page size
        user_id: Filter by user ID
        session_id: Filter by session ID
        start_date: Start date filter
        end_date: End date filter
        query: Search in user message content (partial match)

    Returns:
        Paginated list of user messages with token usage
    """
    try:
        manager = get_trace_manager()
    except RuntimeError:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    messages, total = await manager.store.get_user_messages(
        page=page,
        page_size=page_size,
        user_id=user_id,
        session_id=session_id,
        start_date=start,
        end_date=end,
        query=query,
        export=False,
    )
    return {
        "items": [m.model_dump() for m in messages],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/user-messages/export")
async def export_user_messages(
    user_id: Optional[str] = Query(None, description="Filter by user ID"),
    session_id: Optional[str] = Query(
        None,
        description="Filter by session ID",
    ),
    start_date: Optional[str] = Query(
        None,
        description="Start date (YYYY-MM-DD)",
    ),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    query: Optional[str] = Query(
        None,
        description="Search in user message content",
    ),
    export_format: str = Query(
        "csv",
        description="Export format: csv, json or xlsx",
        alias="format",
    ),
) -> StreamingResponse:
    """Export user messages with token info for cost analysis.

    Args:
        user_id: Filter by user ID
        session_id: Filter by session ID
        start_date: Start date filter
        end_date: End date filter
        query: Search in user message content (partial match)
        export_format: Export format (csv, json or xlsx)

    Returns:
        StreamingResponse with exported data
    """
    try:
        manager = get_trace_manager()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="Tracing not available",
        ) from exc

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date", add_day=True)

    messages, _ = await manager.store.get_user_messages(
        page=1,
        page_size=1,  # Ignored in export mode
        user_id=user_id,
        session_id=session_id,
        start_date=start,
        end_date=end,
        query=query,
        export=True,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if export_format == "json":
        import json

        data = [m.model_dump() for m in messages]
        # Convert datetime to string for JSON serialization
        for item in data:
            if item.get("start_time"):
                item["start_time"] = item["start_time"].isoformat()
        content = json.dumps(data, ensure_ascii=False, indent=2)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=user_messages_{timestamp}.json"
                ),
            },
        )
    elif export_format == "xlsx":
        # Excel format
        try:
            from openpyxl import Workbook
            from openpyxl.styles import (
                Font,
                Alignment,
                Border,
                Side,
                PatternFill,
            )
            from openpyxl.utils import get_column_letter
        except ImportError as exc:
            raise HTTPException(
                status_code=400,
                detail="openpyxl not installed. Use csv or json format.",
            ) from exc

        wb = Workbook()
        ws = wb.active
        ws.title = "User Messages"

        # Header style
        header_fill = PatternFill(
            start_color="4472C4",
            end_color="4472C4",
            fill_type="solid",
        )
        header_font = Font(bold=True, color="FFFFFF")
        header_alignment = Alignment(horizontal="center", vertical="center")
        thin_border = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )

        # Write header
        headers = [
            "Trace ID",
            "User ID",
            "Session ID",
            "Channel",
            "User Message",
            "Input Tokens",
            "Output Tokens",
            "Model Name",
            "Start Time",
            "Duration (ms)",
        ]
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = thin_border

        # Write data
        for row, m in enumerate(messages, 2):
            ws.cell(row=row, column=1, value=m.trace_id).border = thin_border
            ws.cell(row=row, column=2, value=m.user_id).border = thin_border
            ws.cell(row=row, column=3, value=m.session_id).border = thin_border
            ws.cell(row=row, column=4, value=m.channel).border = thin_border
            # Wrap text for user message
            msg_cell = ws.cell(row=row, column=5, value=m.user_message or "")
            msg_cell.border = thin_border
            msg_cell.alignment = Alignment(wrap_text=True, vertical="top")
            ws.cell(
                row=row,
                column=6,
                value=m.input_tokens,
            ).border = thin_border
            ws.cell(
                row=row,
                column=7,
                value=m.output_tokens,
            ).border = thin_border
            ws.cell(
                row=row,
                column=8,
                value=m.model_name or "",
            ).border = thin_border
            ws.cell(
                row=row,
                column=9,
                value=m.start_time.isoformat() if m.start_time else "",
            ).border = thin_border
            ws.cell(
                row=row,
                column=10,
                value=m.duration_ms or "",
            ).border = thin_border

        # Auto-adjust column widths
        column_widths = [36, 20, 36, 15, 60, 12, 12, 25, 22, 12]
        for col, width in enumerate(column_widths, 1):
            ws.column_dimensions[get_column_letter(col)].width = width

        # Save to bytes
        excel_buffer = io.BytesIO()
        wb.save(excel_buffer)
        excel_buffer.seek(0)

        return StreamingResponse(
            iter([excel_buffer.getvalue()]),
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": (
                    f"attachment; filename=user_messages_{timestamp}.xlsx"
                ),
            },
        )
    else:
        # CSV format
        output = io.StringIO()
        writer = csv.writer(output)
        # Write header
        writer.writerow(
            [
                "trace_id",
                "user_id",
                "session_id",
                "channel",
                "user_message",
                "input_tokens",
                "output_tokens",
                "model_name",
                "start_time",
                "duration_ms",
            ],
        )
        # Write data
        for m in messages:
            writer.writerow(
                [
                    m.trace_id,
                    m.user_id,
                    m.session_id,
                    m.channel,
                    m.user_message or "",
                    m.input_tokens,
                    m.output_tokens,
                    m.model_name or "",
                    m.start_time.isoformat() if m.start_time else "",
                    m.duration_ms or "",
                ],
            )
        content = output.getvalue()
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=user_messages_{timestamp}.csv"
                ),
            },
        )

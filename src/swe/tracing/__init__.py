# -*- coding: utf-8 -*-
"""SWE Tracing Module - Link tracing and analytics.

This module provides tracing and analytics capabilities for SWE,
including event collection, storage, and aggregation.
"""

from .config import TracingConfig
from .models import EventType, Span, Trace, TraceStatus
from .manager import (
    TraceManager,
    TraceContext,
    get_trace_manager,
    init_trace_manager,
    close_trace_manager,
    get_current_trace,
    set_current_trace,
    has_trace_manager,
)
from .store import TraceStore
from .model_wrapper import TracingModelWrapper

# Re-export from database module for convenience
from ..database import (
    DatabaseConfig,
    DatabaseConnection,
    TDSQLConfig,
    TDSQLConnection,
)

__all__ = [
    # Config
    "TracingConfig",
    # Database (re-exported)
    "DatabaseConfig",
    "TDSQLConfig",
    "DatabaseConnection",
    "TDSQLConnection",
    # Models
    "EventType",
    "Span",
    "Trace",
    "TraceStatus",
    # Manager
    "TraceManager",
    "TraceContext",
    "get_trace_manager",
    "init_trace_manager",
    "close_trace_manager",
    "get_current_trace",
    "set_current_trace",
    "has_trace_manager",
    # Store
    "TraceStore",
    # Model wrapper
    "TracingModelWrapper",
]

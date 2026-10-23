# -*- coding: utf-8 -*-
"""CoPaw Database Module - Shared database configuration and connection.

This module provides database configuration and async connection pool
management for MySQL-compatible databases.
"""

from .config import DatabaseConfig, TDSQLConfig, get_database_config
from .connection import DatabaseConnection, TDSQLConnection
from .session import DatabaseSession, ExecutionResult
from .prepared import PreparedInsert

__all__ = [
    # Config
    "DatabaseConfig",
    "get_database_config",
    "TDSQLConfig",  # Backward compatibility alias
    # Connection
    "DatabaseConnection",
    "DatabaseSession",
    "ExecutionResult",
    "PreparedInsert",
    "TDSQLConnection",  # Backward compatibility alias
]

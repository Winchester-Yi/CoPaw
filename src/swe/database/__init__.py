# -*- coding: utf-8 -*-
"""CoPaw Database Module - Shared database configuration and connection.

This module provides database configuration and async connection pool
management for MySQL-compatible databases.
"""

from .config import DatabaseConfig, TDSQLConfig
from .connection import DatabaseConnection, TDSQLConnection

__all__ = [
    # Config
    "DatabaseConfig",
    "TDSQLConfig",  # Backward compatibility alias
    # Connection
    "DatabaseConnection",
    "TDSQLConnection",  # Backward compatibility alias
]

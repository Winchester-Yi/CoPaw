# -*- coding: utf-8 -*-
"""Database configuration module.

Defines configuration classes for database connections.
"""

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """Database connection configuration.

    Supports MySQL-compatible databases including TDSQL (Tencent Cloud).
    """

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=3306, description="Database port")
    user: str = Field(default="root", description="Database user")
    password: str = Field(default="", description="Database password")
    database: str = Field(default="copaw", description="Database name")
    min_connections: int = Field(
        default=2,
        description="Minimum connection pool size",
    )
    max_connections: int = Field(
        default=10,
        description="Maximum connection pool size",
    )
    charset: str = Field(default="utf8mb4", description="Character set")


# Backward compatibility alias
TDSQLConfig = DatabaseConfig

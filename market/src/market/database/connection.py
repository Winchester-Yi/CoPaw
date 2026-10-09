# -*- coding: utf-8 -*-
"""异步 MySQL 连接池."""

import logging
from contextvars import ContextVar
from contextlib import asynccontextmanager
from hashlib import sha256
from typing import Any, Optional

from .config import DatabaseConfig

logger = logging.getLogger(__name__)
_CURRENT_CONNECTION: ContextVar[Any | None] = ContextVar(
    "market_db_connection",
    default=None,
)


def _named_lock_name(name: str) -> str:
    """生成不超过 MySQL GET_LOCK 限制的稳定锁名。"""
    return f"swe:{sha256(name.encode()).hexdigest()[:60]}"


try:
    import aiomysql

    AIOMYSQL_AVAILABLE = True
except ImportError:
    AIOMYSQL_AVAILABLE = False
    logger.debug(
        "aiomysql not installed, database features will be unavailable",
    )


class DatabaseConnection:
    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._pool: Optional[Any] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._pool is not None

    async def connect(self) -> None:
        if not AIOMYSQL_AVAILABLE:
            raise RuntimeError("aiomysql is not installed")
        if self._pool is not None:
            return
        try:
            self._pool = await aiomysql.create_pool(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                db=self.config.database,
                charset=self.config.charset,
                minsize=self.config.min_connections,
                maxsize=self.config.max_connections,
                autocommit=True,
            )
            self._connected = True
            logger.info(
                "DB pool created: %s:%s/%s",
                self.config.host,
                self.config.port,
                self.config.database,
            )
        except Exception as e:
            logger.error("Failed to create DB pool: %s", e)
            self._connected = False
            raise

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
            self._connected = False

    @asynccontextmanager
    async def acquire(self):
        if self._pool is None:
            raise RuntimeError("Database not connected")
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self):
        """在同一连接上执行可嵌套事务。"""
        current = _CURRENT_CONNECTION.get()
        if current is not None:
            yield current
            return

        async with self.acquire() as conn:
            token = _CURRENT_CONNECTION.set(conn)
            try:
                await conn.begin()
                yield conn
            except Exception:
                await conn.rollback()
                raise
            else:
                await conn.commit()
            finally:
                _CURRENT_CONNECTION.reset(token)

    @asynccontextmanager
    async def transaction_with_named_lock(
        self,
        name: str,
        timeout: int = 10,
    ):
        """在同一连接上持锁执行事务，并在提交后释放锁。"""
        if _CURRENT_CONNECTION.get() is not None:
            raise RuntimeError("Named transaction cannot be nested")

        lock_name = _named_lock_name(name)
        async with self.acquire() as conn:
            token = _CURRENT_CONNECTION.set(conn)
            acquired = False
            try:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT GET_LOCK(%s, %s) AS acquired",
                        (lock_name, timeout),
                    )
                    row = await cur.fetchone()
                if not row or int(row.get("acquired") or 0) != 1:
                    raise TimeoutError(
                        f"Failed to acquire database lock: {name}",
                    )
                acquired = True
                await conn.begin()
                try:
                    yield conn
                except Exception:
                    await conn.rollback()
                    raise
                await conn.commit()
            finally:
                if acquired:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT RELEASE_LOCK(%s)",
                            (lock_name,),
                        )
                _CURRENT_CONNECTION.reset(token)

    @asynccontextmanager
    async def named_lock(self, name: str, timeout: int = 10):
        """持有 MySQL 命名锁，跨实例串行化同一业务键。"""
        lock_name = _named_lock_name(name)
        conn = _CURRENT_CONNECTION.get()
        if conn is None:
            async with self.acquire() as conn:
                token = _CURRENT_CONNECTION.set(conn)
                try:
                    async with self.named_lock(name, timeout):
                        yield
                finally:
                    _CURRENT_CONNECTION.reset(token)
            return

        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT GET_LOCK(%s, %s) AS acquired",
                (lock_name, timeout),
            )
            row = await cur.fetchone()
        if not row or int(row.get("acquired") or 0) != 1:
            raise TimeoutError(f"Failed to acquire database lock: {name}")
        try:
            yield
        finally:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT RELEASE_LOCK(%s)",
                    (lock_name,),
                )

    async def execute(self, query: str, params: Optional[tuple] = None) -> int:
        conn = _CURRENT_CONNECTION.get()
        if conn is not None:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                return cur.rowcount
        async with self.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                return cur.rowcount

    async def execute_many(self, query: str, params_list: list[tuple]) -> int:
        if not params_list:
            return 0
        conn = _CURRENT_CONNECTION.get()
        if conn is not None:
            async with conn.cursor() as cur:
                await cur.executemany(query, params_list)
                return cur.rowcount
        async with self.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(query, params_list)
                return cur.rowcount

    async def fetch_one(
        self,
        query: str,
        params: Optional[tuple] = None,
    ) -> Optional[dict]:
        conn = _CURRENT_CONNECTION.get()
        if conn is not None:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
                return dict(row) if row else None
        async with self.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
                return dict(row) if row else None

    async def fetch_all(
        self,
        query: str,
        params: Optional[tuple] = None,
    ) -> list[dict]:
        conn = _CURRENT_CONNECTION.get()
        if conn is not None:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                return [dict(row) for row in rows] if rows else []
        async with self.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                return [dict(row) for row in rows] if rows else []

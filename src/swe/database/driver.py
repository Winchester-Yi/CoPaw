# -*- coding: utf-8 -*-
"""Small public-API adapter for strict prepared handle ownership."""

from mysql.connector.aio import MySQLConnection
from mysql.connector.errors import Error


class ManagedMySQLConnection(MySQLConnection):
    """Expose failed statement disposal to the pool instead of hiding it.

    Connector/Python 26.7 prepared cursor.reset catches driver Error from
    cmd_stmt_close. Our LRU must discard the connection after such a failure,
    so reclassify it at the public command boundary, retaining the cause.
    """

    async def cmd_stmt_close(self, statement_id, **kwargs):
        try:
            return await super().cmd_stmt_close(statement_id, **kwargs)
        except Error as error:
            raise RuntimeError(
                "Database prepared statement close failed",
            ) from error
